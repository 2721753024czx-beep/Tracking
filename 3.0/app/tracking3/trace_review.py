"""Read-only, task-bound inspection of actual detection samples and source frames."""
import base64
from fractions import Fraction
import hashlib
import io
import math

import av
from PIL import Image

from .index_trace import snapshot
from .integrity import require as require_source
from .store import Store


def _read(store, task_id, observation_id):
    # A separate read transaction also isolates external WAL writers.
    reader = Store.open_readonly(store.root)
    try:
        reader.db.execute('BEGIN')
        task = reader.get('task', task_id)
        observation = reader.get('observation', observation_id)
        if reader.observation_owner(observation) not in (None, task_id):
            raise ValueError('片段不属于此任务')
        media = reader.get('media', observation['media_id'])
        frames = snapshot(reader, {media['id']})['frames']
        samples = []
        for row in frames:
            for detection in row['detections']:
                if detection['observation_id'] != observation_id:
                    continue
                box = detection['box']
                if (row['camera_id'] != observation['camera_id'] or
                        row['encoder'] != observation['encoder'] or
                        str(detection['local_track']) != str(observation['local_track']) or
                        len(box) != 4 or not all(math.isfinite(v) for v in box) or
                        box[0] >= box[2] or box[1] >= box[3]):
                    raise ValueError('采样框与片段记录不一致')
                samples.append({k: row[k] for k in (
                    'id', 'media_id', 'camera_id', 'source_sha256', 'run_no',
                    'frame_index', 'raw_pts', 'time_base', 'origin_pts_s', 'media_s',
                    'width', 'height', 'sample_fps') } | {'detection': detection})
        samples.sort(key=lambda row: (row['media_s'], row['run_no'], row['frame_index']))
        return task, observation, media, samples
    finally:
        reader.db.rollback()
        reader.db.close()


def inspect(store, task_id, observation_id):
    task, observation, media, samples = _read(store, task_id, observation_id)
    integrity = require_source(store, media)
    return dict(task_id=task_id, task_version=task['version'],
        observation={k:v for k,v in observation.items() if k!='feature'},
        source_integrity=integrity, samples=samples, sample_count=len(samples),
        status='recorded_samples_only' if samples else
            ('manual_without_automatic_trace' if observation.get('source')=='manual'
             else 'not_recorded_legacy_or_unindexed'),
        superseded=observation_id in task.get('superseded_observations', []),
        unsampled_frames_inferred=False, trajectory_identity_certified=False)


def preview(store, task_id, observation_id, sample_id):
    task, observation, media, samples = _read(store, task_id, observation_id)
    sample = next((row for row in samples if row['id']==sample_id), None)
    if sample is None:
        raise ValueError('采样点不属于此片段或没有保存自动轨迹')
    require_source(store, media)
    rgb = None
    origin = None
    with av.open(str(store.asset(media['path']))) as container:
        for index, frame in enumerate(container.decode(container.streams.video[0])):
            if frame.pts is None:
                raise ValueError('源录像缺少帧时间，不能核对采样画面')
            raw_time = float(frame.pts * frame.time_base)
            if origin is None:
                origin = raw_time
            if index != sample['frame_index']:
                continue
            if (frame.pts != sample['raw_pts'] or
                    Fraction(frame.time_base) != Fraction(sample['time_base']) or
                    abs(origin-sample['origin_pts_s'])>1e-6 or
                    abs(raw_time-origin-sample['media_s'])>1e-6 or
                    frame.width!=sample['width'] or frame.height!=sample['height']):
                raise ValueError('源帧与保存的采样位置或时间不一致')
            rgb = frame.to_ndarray(format='rgb24')
            break
    if rgb is None:
        raise ValueError('源录像没有保存采样点对应的帧')
    require_source(store, media)
    buffer = io.BytesIO()
    Image.fromarray(rgb).save(buffer, 'PNG')
    image = buffer.getvalue()
    return dict(task_id=task_id, task_version=task['version'],
        observation_id=observation['id'], sample=sample,
        actual_media_s=sample['media_s'], actual_frame_index=sample['frame_index'],
        image_sha256=hashlib.sha256(image).hexdigest(),
        image_data_url='data:image/png;base64,'+base64.b64encode(image).decode(),
        trajectory_identity_certified=False)
