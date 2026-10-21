"""Local RTSP/HTTP capture for offline replay, with no raw URL persisted."""
import math
import json
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from .media import finite, probe, read_frame
from .store import fingerprint, uid
from .timing import metadata as time_metadata
from .venue import camera as venue_camera


SCHEMES = {'rtsp', 'rtmp', 'http', 'https'}
VIDEO_TIME_BASES = {
    'source': '原流时间戳或解复用器推定；不保证对应实际拍摄时间',
    'receiver': '接收端墙上时钟生成的VFR帧时间；受网络缓冲、时钟调整和输入时基量化影响，不是摄像头拍摄时间',
}


def redact_url(value):
    parsed = urlparse(str(value).strip())
    if parsed.username is None and parsed.password is None:
        return urlunparse(parsed)
    host = parsed.hostname or ''
    if parsed.port:
        host += f':{parsed.port}'
    return urlunparse((parsed.scheme, f'***:***@{host}', parsed.path,
                       parsed.params, parsed.query, parsed.fragment))


def validate(data):
    url = str(data.get('url', '')).strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() not in SCHEMES or not parsed.hostname:
        raise ValueError('取流地址须为带主机名的 RTSP/RTMP/HTTP/HTTPS 地址')
    camera_id = str(data.get('camera_id', '')).strip()
    if not camera_id or len(camera_id) > 100:
        raise ValueError('请输入不超过100字符的摄像头编号')
    location = str(data.get('location', '')).strip()
    if len(location) > 200:
        raise ValueError('位置过长')
    duration = finite(data.get('duration', 60), '录制时长', 1)
    if duration > 86400:
        raise ValueError('单次录制不能超过24小时')
    codec = str(data.get('codec', 'copy')).strip().lower()
    if codec not in ('copy', 'h264'):
        raise ValueError('编码模式只能是 copy 或 h264')
    timestamp_mode = str(data.get('timestamp_mode', 'source')).strip().lower()
    if timestamp_mode not in VIDEO_TIME_BASES:
        raise ValueError('帧时间模式只能是 source 或 receiver')
    if timestamp_mode == 'receiver' and (parsed.scheme.lower() not in ('http', 'https') or codec != 'h264'):
        raise ValueError('接收帧时间模式须使用 HTTP/HTTPS 连续流和 H.264 转码')
    return dict(url=url, camera_id=camera_id, location=location,
                duration=duration, codec=codec, timestamp_mode=timestamp_mode,
                url_redacted=redact_url(url))


def command(config, output):
    args = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y']
    if urlparse(config['url']).scheme.lower() == 'rtsp':
        args += ['-rtsp_transport', 'tcp']
    if config.get('timestamp_mode', 'source') == 'receiver':
        args += ['-use_wallclock_as_timestamps', '1']
    args += ['-i', config['url'], '-t', f"{config['duration']:.3f}", '-map', '0:v:0', '-an']
    args += ['-c:v', 'copy'] if config['codec'] == 'copy' else ['-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p']
    if config.get('timestamp_mode', 'source') == 'receiver':
        args += ['-fps_mode', 'vfr', '-enc_time_base', '1:1000000']
    return args + ['-movflags', '+faststart', str(output)]


def sidecar_payload(job):
    """Return capture metadata without raw credentials or subprocess arguments."""
    return {
        'format': 'tracking3-capture-sidecar-v1',
        'capture_id': job.get('id'), 'status': job.get('status'),
        'path': job.get('path'), 'camera_id': job.get('camera_id'),
        'location': job.get('location'), 'duration': job.get('duration'),
        'codec': job.get('codec'), 'url_redacted': job.get('url_redacted'),
        'timestamp_mode': job.get('timestamp_mode', 'source'),
        'video_timestamp_basis': VIDEO_TIME_BASES[job.get('timestamp_mode', 'source')],
        'started_at': job.get('started_at'), 'finished_at': job.get('finished_at'),
        'media_id': job.get('media_id'), 'error': job.get('error'),
        'stop_requested_at': job.get('stop_requested_at'),
        'finish_reason': job.get('finish_reason'),
        'recorded_duration': job.get('recorded_duration'),
        'time_basis': '接收端墙上时钟；不是摄像头帧时间，须另行校时',
    }


class CaptureManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.processes = {}
        self.threads = {}

    def list(self, store):
        return store.list('capture')

    def start(self, store, data):
        config = validate(data)
        with self.lock:
            if any(job.get('status') in ('recording', 'stopping') and
                   job.get('camera_id') == config['camera_id'] for job in store.list('capture')):
                raise ValueError('该摄像头已有录制任务，请先停止或等待完成')
            ident = uid()
            relative = f'captures/{ident}.mp4'
            job = dict(id=ident, status='recording', path=relative,
                       camera_id=config['camera_id'], location=config['location'],
                       duration=config['duration'], codec=config['codec'],
                       timestamp_mode=config['timestamp_mode'],
                       url_redacted=config['url_redacted'], started_at=time.time(),
                       media_id=None, error=None, sidecar=f'captures/{ident}.json')
            store.put('capture', job)
            self._write_sidecar(store, job)
            thread = threading.Thread(target=self._run, args=(store, job, config), daemon=True)
            self.threads[ident] = thread
            thread.start()
            return job

    def stop(self, store, ident):
        with self.lock:
            job = store.get('capture', ident)
            if job.get('status') != 'recording':
                return job
            job['status'] = 'stopping'
            job['stop_requested_at'] = time.time()
            store.put('capture', job)
            self._write_sidecar(store, job)
            process = self.processes.get(ident)
            if process:
                process.terminate()
            return job

    def stop_all(self):
        with self.lock:
            for process in self.processes.values():
                if process.poll() is None:
                    process.terminate()

    def _run(self, store, job, config):
        ident = job['id']
        output = store.root / job['path']
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            process = subprocess.Popen(command(config, output), stdout=subprocess.DEVNULL,
                                       stderr=subprocess.PIPE, text=True)
            with self.lock:
                self.processes[ident] = process
                # A stop can arrive before Popen has been registered.
                if store.get('capture', ident).get('status') == 'stopping':
                    process.terminate()
            try:
                stderr = process.communicate(timeout=config['duration'] + 120)[1]
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise ValueError('录制进程超时，已终止')
            current = store.get('capture', ident)
            stopped = current.get('stop_requested_at') is not None
            job.update(stop_requested_at=current.get('stop_requested_at'),
                       finish_reason='user_stopped' if stopped else 'duration_or_source_end')
            # ffmpeg may exit 255 after SIGTERM while finalizing a valid MP4.
            # Accept only an explicit stop and known signal exits; probe must
            # still succeed before any media is registered.
            if process.returncode != 0 and not (stopped and process.returncode in (255, -15)):
                message = (stderr or 'ffmpeg录制失败').replace(config['url'], config['url_redacted'])
                raise ValueError(message.strip()[-1000:])
            info = probe(output)
            if info['duration'] <= 0:
                raise ValueError('录制没有可回看的视频时长')
            if stopped:
                read_frame(output, 0)  # Metadata alone is insufficient for a partial recording.
            job['recorded_duration'] = info['duration']
            fp = fingerprint(output)
            profile = venue_camera(store, job['camera_id']) or {}
            existing = next((m for m in store.list('media')
                             if m.get('camera_id') == job['camera_id'] and m.get('fingerprint') == fp), None)
            if existing:
                media_id = existing['id']
            else:
                media = dict(id=uid(), path=job['path'], name=output.name,
                             camera_id=job['camera_id'],
                             location=job['location'] or profile.get('area') or profile.get('label', ''),
                             venue_camera=profile or None, fingerprint=fp, status='unprocessed',
                             processed_until=-1, time_version=1,
                             capture_id=job['id'], capture_sidecar=job['sidecar'],
                             capture_wallclock_started_at=job['started_at'],
                             capture_wallclock_finished_at=time.time(),
                             capture_time_basis='接收端墙上时钟；不是摄像头帧时间，须另行校时',
                             capture_video_timestamp_mode=config.get('timestamp_mode', 'source'),
                             capture_video_timestamp_basis=VIDEO_TIME_BASES[config.get('timestamp_mode', 'source')],
                             **time_metadata({}), **info)
                store.put('media', media)
                media_id = media['id']
            job.update(status='complete', media_id=media_id, finished_at=time.time())
        except Exception as exc:
            job.update(status='failed', error=f'{type(exc).__name__}: {str(exc)[:1000]}', finished_at=time.time())
            output.unlink(missing_ok=True)
        finally:
            self._write_sidecar(store, job)
            with self.lock:
                self.processes.pop(ident, None)
                self.threads.pop(ident, None)
            store.put('capture', job)

    @staticmethod
    def _write_sidecar(store, job):
        relative = job.get('sidecar')
        if not relative:
            return
        path = store.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sidecar_payload(job), ensure_ascii=False, indent=2), encoding='utf-8')
