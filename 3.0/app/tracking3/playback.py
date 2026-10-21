"""Read-only target playback from recorded index samples and verified source frames.

The MP4 contains no overlays. Every displayed frame is one saved index sample;
the UI draws only that sample's detections, keyed by observation ID.
"""
from contextlib import contextmanager
from fractions import Fraction
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

import av

from .index_trace import snapshot
from .integrity import require as require_source
from .store import Store, fingerprint


EXPORT_VERSION = 'target-playback-sampled-vfr-h264-v1'
TIME_BASE = Fraction(1, 1_000_000)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _identifier(value, label):
    if not isinstance(value, str) or not value or len(value) > 200 or any(
            c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-:' for c in value):
        raise ValueError(f'{label}无效')
    return value


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _read(store, task_id, observation_id):
    _identifier(task_id, '任务标识'); _identifier(observation_id, '片段标识')
    reader = Store.open_readonly(store.root)
    try:
        reader.db.execute('BEGIN')
        task = reader.get('task', task_id)
        observation = reader.get('observation', observation_id)
        if reader.observation_owner(observation) not in (None, task_id):
            raise ValueError('片段不属于此任务')
        media = reader.get('media', observation['media_id'])
        _identifier(media['id'], '素材标识')
        if (not task.get('encoder') or observation.get('encoder') != task['encoder'] or
                media.get('encoder') != task['encoder']):
            raise ValueError('任务、片段和素材编码器不一致')
        if (observation.get('source') != 'detected' or
                observation.get('detection_kind', 'tracked') != 'tracked' or
                observation.get('local_track') is None):
            raise ValueError('此片段没有可回放的自动跟踪记录（人工或单帧线索不适用）')
        # Query/reference sources remain accessible even when search is limited
        # to another camera. Other out-of-scope camera observations are refused.
        references = set(task.get('reference_observations') or [])
        references.update(x for x in (task.get('anchor'), task.get('query_anchor')) if x)
        source_media = set()
        for ident in references:
            reference = reader.get('observation', ident)
            if (reader.observation_owner(reference) in (None, task_id) and
                    reference.get('encoder') == task['encoder']):
                source_media.add(reference['media_id'])
        allowed = task.get('cameras') or []
        if allowed and media['camera_id'] not in allowed and media['id'] not in source_media:
            raise ValueError('片段不在此任务可访问的摄像头或参考片段中')
        if observation['camera_id'] != media['camera_id']:
            raise ValueError('片段与素材摄像头不一致')
        if media.get('status') == 'processing':
            raise ValueError('请暂停素材处理后回放已保存的采样')
        rows = snapshot(reader, {media['id']})['frames']
        if not rows:
            raise ValueError('素材没有保存实际采样记录，不能生成目标回放')
        rows.sort(key=lambda row: (row['media_s'], row['frame_index'], row['run_no']))
        seen_frames, seen_times, target_count = set(), set(), 0
        observations = {o['id']: o for o in reader.list('observation') if o.get('media_id') == media['id']}
        for row in rows:
            if (row['camera_id'] != media['camera_id'] or row['encoder'] != task['encoder'] or
                    type(row['width']) is not int or row['width'] <= 0 or
                    type(row['height']) is not int or row['height'] <= 0 or
                    type(row['raw_pts']) is not int or not _number(row['sample_fps']) or row['sample_fps'] <= 0 or
                    row['frame_index'] in seen_frames or row['media_s'] in seen_times or row['media_s'] < 0):
                raise ValueError('采样帧来源、尺寸或时间重复/无效')
            seen_frames.add(row['frame_index']); seen_times.add(row['media_s'])
            if row['width'] != rows[0]['width'] or row['height'] != rows[0]['height']:
                raise ValueError('采样画面尺寸变化，不能共用此回放')
            detection_ids = set()
            for detection in row['detections']:
                linked = observations.get(detection['observation_id'])
                box, confidence = detection.get('box'), detection.get('confidence')
                if (linked is None or linked.get('encoder') != task['encoder'] or
                        linked['camera_id'] != media['camera_id'] or
                        str(detection.get('local_track')) != str(linked.get('local_track')) or
                        detection['observation_id'] in detection_ids or
                        not isinstance(box, list) or len(box) != 4 or not all(_number(v) for v in box) or
                        not (0 <= box[0] < box[2] <= row['width'] and 0 <= box[1] < box[3] <= row['height']) or
                        not _number(confidence) or not 0 <= confidence <= 1):
                    raise ValueError('采样框与片段记录不一致或目标框无效')
                detection_ids.add(detection['observation_id'])
                if detection['observation_id'] == observation_id:
                    if detection.get('detection_kind', 'tracked') != 'tracked':
                        raise ValueError('所选片段包含非跟踪采样，不能沿用为目标轨迹')
                    target_count += 1
        if not target_count:
            raise ValueError('此片段没有自己的实际跟踪采样，不能借用相同本地编号的其他片段')
        decisions = {d['observation_id']: d['status'] for d in reader.decisions(task_id)}
        status = decisions.get(observation_id, 'pending')
        return task, observation, media, rows, status
    finally:
        reader.db.rollback(); reader.db.close()


def _binding(media, rows):
    return dict(export_version=EXPORT_VERSION, media_id=media['id'],
        source_sha256=media['fingerprint'], samples_sha256=_digest(rows))


def _paths(store, media):
    directory = store.root / 'playback-previews' / media['id']
    if not directory.resolve().is_relative_to(store.root):
        raise ValueError('预览缓存路径无效')
    return directory, directory / 'samples.mp4', directory / 'manifest.json'


def _cached(store, media, rows):
    _, video, manifest = _paths(store, media)
    try:
        data = json.loads(manifest.read_text())
        if (data.get('binding') != _binding(media, rows) or data.get('sample_count') != len(rows) or
                not video.is_file() or fingerprint(video) != data.get('sha256')):
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def _payload(store, state, cached):
    task, observation, media, rows, status = state
    fps = sorted({row['sample_fps'] for row in rows})
    _, video, _ = _paths(store, media)
    preview = dict(path=str(video.relative_to(store.root)) if cached else None,
        sample_count=len(rows), sample_fps=fps[0] if len(fps) == 1 else None)
    if cached:
        preview['sha256'] = cached['sha256']
    return dict(ready=bool(cached), task={k: task.get(k) for k in ('id', 'name', 'version')},
        observation={k: observation.get(k) for k in ('id', 'camera_id', 'local_track', 'start', 'end', 'representative', 'crop')} | {'status': status},
        media={k: media.get(k) for k in ('id', 'name', 'camera_id', 'duration')} |
              {k: rows[0][k] for k in ('width', 'height')},
        preview=preview, samples=[{k: row[k] for k in ('media_s', 'frame_index', 'width', 'height', 'detections')} for row in rows],
        explanation='按已保存的实际采样帧回放；采样间保持上一画面，未推断中间帧。框仅属于当前采样中的片段ID；未检出不证明无人，断段或重新检出不自动认定同一人。')


def inspect(store, task_id, observation_id):
    """GET is read-only, including when no preview cache exists."""
    state = _read(store, task_id, observation_id)
    require_source(store, state[2])
    cached = _cached(store, state[2], state[3])
    require_source(store, state[2])
    return _payload(store, state, cached)


@contextmanager
def _cache_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.prepare.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _encode(store, media, rows, output):
    width = min(1280, rows[0]['width'])
    height = round(rows[0]['height'] * width / rows[0]['width'])
    width, height = max(2, width // 2 * 2), max(2, height // 2 * 2)
    by_index = {row['frame_index']: row for row in rows}
    timestamps = [round(row['media_s'] / TIME_BASE) for row in rows]
    if any(b <= a for a, b in zip(timestamps, timestamps[1:])):
        raise ValueError('采样时间过于接近，不能保持实际帧顺序')
    end = media.get('duration')
    tail = end - rows[-1]['media_s'] if _number(end) and end > rows[-1]['media_s'] else 1 / rows[-1]['sample_fps']
    durations = {at: (timestamps[i+1] - at if i+1 < len(rows) else max(1, round(tail / TIME_BASE)))
                 for i, at in enumerate(timestamps)}
    encoded, origin = 0, None
    with av.open(str(output), 'w', options={'movflags': '+faststart'}) as out:
        stream = out.add_stream('libx264', rate=30)
        stream.width, stream.height, stream.pix_fmt = width, height, 'yuv420p'
        stream.options = {'preset': 'veryfast', 'crf': '23', 'bf': '0', 'tune': 'zerolatency'}
        stream.time_base = TIME_BASE
        stream.codec_context.time_base = TIME_BASE

        def mux(packets):
            for packet in packets:
                packet.duration = durations[packet.pts]
                out.mux(packet)

        with av.open(str(store.asset(media['path']))) as source:
            for index, frame in enumerate(source.decode(video=0)):
                if frame.pts is None:
                    raise ValueError('源录像缺少帧时间，不能核对采样画面')
                raw_time = float(frame.pts * frame.time_base)
                if origin is None:
                    origin = raw_time
                row = by_index.pop(index, None)
                if row is None:
                    continue
                if (frame.pts != row['raw_pts'] or Fraction(frame.time_base) != Fraction(row['time_base']) or
                        abs(origin - row['origin_pts_s']) > 1e-6 or
                        abs(raw_time - origin - row['media_s']) > 1e-6 or
                        frame.width != row['width'] or frame.height != row['height']):
                    raise ValueError('源帧与保存的采样位置、时间或尺寸不一致')
                video = frame.reformat(width=width, height=height, format='yuv420p')
                video.pts = timestamps[encoded]; video.time_base = TIME_BASE
                mux(stream.encode(video)); encoded += 1
                if not by_index:
                    break
        if by_index or encoded != len(rows):
            raise ValueError('源录像没有保存采样点对应的帧')
        mux(stream.encode())
    # Verify actual muxed timestamps, rather than trusting encoder configuration.
    verified = 0
    with av.open(str(output)) as preview:
        for verified, frame in enumerate(preview.decode(video=0), 1):
            if (verified > len(rows) or frame.pts is None or
                    abs(float(frame.pts * frame.time_base) - rows[verified-1]['media_s']) > 1e-6):
                raise ValueError('预览编码未保留实际采样时间，未发布此预览')
    if verified != len(rows):
        raise ValueError('预览编码未保留实际采样时间，未发布此预览')
    return width, height


def prepare(store, task_id, observation_id):
    state = _read(store, task_id, observation_id)
    directory, video, manifest = _paths(store, state[2])
    require_source(store, state[2])
    with _cache_lock(directory):
        state = _read(store, task_id, observation_id)
        media, rows = state[2], state[3]
        require_source(store, media)
        cached = _cached(store, media, rows)
        if cached:
            return _payload(store, state, cached)
        handle, temporary = tempfile.mkstemp(prefix='.samples-', suffix='.mp4', dir=directory)
        os.close(handle)
        temporary = Path(temporary)
        manifest_tmp = temporary.with_suffix('.json')
        try:
            width, height = _encode(store, media, rows, temporary)
            require_source(store, media)
            current = _read(store, task_id, observation_id)
            if _binding(current[2], current[3]) != _binding(media, rows):
                raise ValueError('采样记录已变化，请重新准备目标回放')
            require_source(store, current[2])
            cached = dict(binding=_binding(media, rows), sample_count=len(rows),
                sha256=fingerprint(temporary), width=width, height=height)
            manifest_tmp.write_text(json.dumps(cached, ensure_ascii=False, sort_keys=True) + '\n')
            os.replace(temporary, video)
            os.replace(manifest_tmp, manifest)
            return _payload(store, current, cached)
        finally:
            temporary.unlink(missing_ok=True); manifest_tmp.unlink(missing_ok=True)
