import json
import math
import subprocess
from datetime import datetime


def finite(value, label, minimum=None):
    number = float(value)
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise ValueError(f'{label}数值无效')
    return number


def absolute_start(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('录像起始时刻须包含时区，如 2026-10-09T15:00:00+08:00')
    return parsed.timestamp()


def probe(path):
    p = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                        '-of', 'json', str(path)], capture_output=True, timeout=30)
    if p.returncode:
        raise ValueError('无法读取视频，请检查文件或 ffprobe。')
    result = json.loads(p.stdout)
    video = next((s for s in result['streams'] if s['codec_type'] == 'video'), None)
    if not video:
        raise ValueError('文件中没有视频轨道')
    duration = finite(video.get('duration', result.get('format', {}).get('duration', 0)), '时长', 0)
    return dict(duration=duration, width=video['width'], height=video['height'],
                codec=video.get('codec_name'), fps=video.get('avg_frame_rate'),
                stream_start=video.get('start_time'))


def read_frame(path, seconds):
    import av
    # Exact decode from the beginning: slower, but does not mistake keyframe time for requested time.
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        origin = None
        last = None
        for frame in container.decode(stream):
            if frame.pts is None:
                continue
            pts = float(frame.pts * frame.time_base)
            if origin is None:
                origin = pts
            last = (frame.to_ndarray(format='rgb24'), pts - origin)
            if pts - origin + 1e-6 >= seconds:
                return last
        if last is None or seconds > last[1] + 0.5:
            raise ValueError('该时刻没有可读取画面')
        return last


def crop(image, box=None):
    h, w = image.shape[:2]
    if box is None:
        return image
    if len(box) != 4:
        raise ValueError('框须为 x1,y1,x2,y2')
    coords = [finite(v, '目标框') for v in box]
    x1, y1, x2, y2 = map(int, coords)
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        raise ValueError('目标框为空，请重新框选')
    return image[y1:y2, x1:x2]
