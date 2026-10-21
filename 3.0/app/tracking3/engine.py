import hashlib
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image

from .media import crop
from .store import fingerprint
from .timing import fields as time_fields
from .topology import scope, before_anchor
from .conditions import match


class Encoder:
    """Versioned appearance backends; baseline stays explicit and opt-in compatible."""

    def __init__(self, onnx=None, *, osnet=None, device='cpu'):
        self.session = None
        self.reid = None
        if onnx and osnet:
            raise ValueError('不能同时指定ONNX和OSNet编码器')
        if osnet:
            from .reid import OSNetEncoder
            self.reid = OSNetEncoder(osnet, device)
            self.version = self.reid.version
        elif onnx:
            import onnxruntime as ort
            self.session = ort.InferenceSession(str(onnx), providers=['CPUExecutionProvider'])
            model_hash = hashlib.sha256(Path(onnx).read_bytes()).hexdigest()
            self.version = 'onnx:' + model_hash
        else:
            self.version = 'hsv-body-v1:experimental'

    def extract(self, rgb):
        if self.reid:
            return self.reid.extract(rgb)
        if self.session:
            image = np.asarray(Image.fromarray(rgb).resize((128, 256)), dtype=np.float32) / 255
            image = (image - np.array([.485, .456, .406])) / np.array([.229, .224, .225])
            batch = image.transpose(2, 0, 1)[None].astype(np.float32)
            vector = self.session.run(None, {self.session.get_inputs()[0].name: batch})[0].reshape(-1)
        else:
            # Upper/lower body appearance; cheap workflow baseline, weak under lighting/view changes.
            hsv = np.asarray(Image.fromarray(rgb).convert('HSV'))
            parts = np.array_split(hsv, 2, axis=0)
            vector = np.concatenate([np.histogramdd(p.reshape(-1, 3), bins=(16, 4, 4),
                         range=((0, 256), (0, 256), (0, 256)))[0].flatten() for p in parts])
            vector = np.sqrt(vector / max(vector.sum(), 1))
        norm = np.linalg.norm(vector)
        if not norm or not np.isfinite(vector).all():
            raise ValueError('无法提取有效外观特征')
        return (vector / norm).astype(float).tolist()


class Indexer:
    def __init__(self, store, encoder, weights, device='cpu'):
        self.store, self.encoder = store, encoder
        self.weights, self.device = Path(weights), device
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None

    def start(self, media_id, sample_fps=3):
        if not self.lock.acquire(blocking=False):
            raise ValueError('当前正在处理另一份录像，请等待或暂停')
        self.stop.clear()
        self.thread = threading.Thread(target=self._run, args=(media_id, sample_fps), daemon=True)
        self.thread.start()

    def _run(self, media_id, sample_fps):
        try:
            self.process(media_id, sample_fps)
        except Exception as exc:
            media = self.store.get('media', media_id)
            media.update(status='failed', error=f'{type(exc).__name__}: {str(exc)[:500]}')
            self.store.put('media', media)
        finally:
            self.lock.release()

    def process(self, media_id, sample_fps=3, detector=None):
        import av
        media = self.store.get('media', media_id)
        if fingerprint(self.store.asset(media['path'])) != media['fingerprint']:
            raise ValueError('源文件已改变，停止沿用旧证据，请重新导入')
        if media['status'] == 'complete':
            raise ValueError('该文件已处理完成；无需重复处理')
        if media.get('encoder') and media['encoder'] != self.encoder.version:
            raise ValueError('编码器已改变，须另建素材版本，不能混合已有索引')
        if media.get('sample_fps') and media['sample_fps'] != sample_fps:
            raise ValueError('恢复时须使用相同采样帧率')
        resume_after = media.get('processed_until', -1)
        run_no = media.get('run_no', 0) + 1
        if detector is None:
            if not self.weights.is_file():
                raise ValueError('检测权重不存在，请设置 --weights')
            from .detection import TrackedDetector
            detector = TrackedDetector(self.weights, self.device)
            if resume_after >= 0 and media.get('index_policy') != detector.policy:
                raise ValueError('索引检测策略或模型已变化，请新增素材版本，不能混合恢复旧索引')
            media['index_policy'] = detector.policy

        media.update(status='processing', run_no=run_no, error=None,
                     sample_fps=sample_fps, encoder=self.encoder.version,
                     index_trace_version=1,
                     detector='yolov8n+bytetrack' if self.weights.is_file() else 'test-detector')
        self.store.put('media', media)
        seen = {}
        origin, next_sample, last_second = None, 0., 0.
        with av.open(str(self.store.asset(media['path']))) as container:
            stream = container.streams.video[0]
            for frame_index, frame in enumerate(container.decode(stream)):
                if self.stop.is_set():
                    break
                if frame.pts is None:
                    raise ValueError('录像缺少媒体时间，不能建立可信定位')
                pts = float(frame.pts * frame.time_base)
                if origin is None:
                    origin = pts
                second = pts - origin
                if second < last_second - .001:
                    raise ValueError('录像时间戳倒退，请修复素材时间后重试')
                last_second = second
                if second <= resume_after + 1e-6:
                    continue
                if second + 1e-6 < next_sample:
                    continue
                next_sample = second + 1 / sample_fps
                rgb = frame.to_ndarray(format='rgb24')
                frame_observations = []
                detections = []
                for i, detection in enumerate(detector(rgb)):
                    box, confidence, local_id = detection[:3]
                    evidence = detection[3] if len(detection) == 4 else {}
                    if evidence.get('detection_kind') == 'single_frame':
                        local_id = None
                    key = str(int(local_id)) if local_id is not None else f'frame:{frame.pts}:{i}'
                    # Even local IDs are segmented after a substantial gap.
                    old = seen.get(key)
                    if old and second - old['end'] > 2:
                        old = None
                    ident = old['id'] if old else hashlib.sha256(
                        f"{media_id}:{run_no}:{key}:{frame.pts}".encode()).hexdigest()[:24]
                    patch = crop(rgb, box)
                    vector = self.encoder.extract(patch)
                    relative = f'crops/{ident}.jpg'
                    if old is None or confidence > old['confidence']:
                        Image.fromarray(patch).save(self.store.root / relative)
                        feature, representative, representative_box = vector, second, box
                        representative_evidence = evidence
                    else:
                        feature, representative, representative_box = old['feature'], old['representative'], old['box']
                        representative_evidence = {k: old[k] for k in evidence if k in old}
                    observation = dict(id=ident, media_id=media_id, camera_id=media['camera_id'],
                        start=old['start'] if old else second, end=second, representative=representative,
                        box=representative_box, crop=relative, feature=feature, encoder=self.encoder.version,
                        confidence=max(confidence, old['confidence'] if old else 0), local_track=key,
                        source='detected', sampled=True)
                    observation.update(representative_evidence)
                    frame_observations.append(observation)
                    detections.append(dict(observation_id=ident, local_track=key,
                                           box=list(map(float, box)), confidence=float(confidence), **evidence))
                    seen[key] = observation
                media['processed_until'] = second
                trace = dict(id=hashlib.sha256(f'index:{media_id}:{run_no}:{frame_index}'.encode()).hexdigest()[:32],
                    version=1, media_id=media_id, camera_id=media['camera_id'], run_no=run_no,
                    source_sha256=media['fingerprint'], frame_index=frame_index, frame_index_base=0,
                    raw_pts=frame.pts, time_base=str(frame.time_base), origin_pts_s=origin,
                    media_s=second, width=frame.width, height=frame.height, sample_fps=sample_fps,
                    encoder=self.encoder.version, detector=media['detector'], detections=detections)
                if getattr(detector, 'sample', None) is not None:
                    trace['detector_sample'] = detector.sample
                # Empty detections are still an executed model sample, never GT absence.
                # Source frame, trajectory update and progress commit together.
                self.store.batch([('observation', o) for o in frame_observations] +
                                 [('index_frame', trace), ('media', media)])
        media['status'] = 'paused' if self.stop.is_set() else 'complete'
        media['error'] = '已暂停；再次处理可恢复，跨暂停边界的轨迹需重新核对。' if self.stop.is_set() else None
        self.store.put('media', media)


def timestamp(media, second):
    if media.get('start_epoch') is None:
        return None
    return media['start_epoch'] + media.get('offset', 0) + second


def search(store, encoder, task_id, limit=30):
    task = store.get('task', task_id)
    if task['encoder'] != encoder.version:
        raise ValueError('任务编码器版本不同，请用同一编码器启动或新建任务')
    if task.get('reference_review_required'):
        raise ValueError('多参考片段已发生身份变化，请重新配置参考片段')
    decisions = {d['observation_id']: d['status'] for d in store.decisions(task_id)}
    context = scope(store, task)
    result = []
    from .integrity import check
    integrity = {m['id']: check(store,m) for m in store.list('media')}
    for observation in store.list('observation'):
        if store.observation_owner(observation) not in (None, task_id) or observation['id'] in task.get('superseded_observations', []):
            continue
        if observation['encoder'] != encoder.version:
            continue
        media = store.get('media', observation['media_id'])
        direction = task.get('search_direction', 'after')
        before = bool(context['anchor'] and before_anchor(store, context['anchor'], observation))
        after = bool(context['anchor'] and before_anchor(store, observation, context['anchor']))
        if direction == 'after' and before:
            continue
        if direction == 'before' and after:
            continue
        if media['camera_id'] not in context['selected_cameras']:
            continue
        at = timestamp(media, observation['start'])
        time_match = match(task,media,observation)
        if time_match=='outside':
            continue
        score = float(np.dot(task['feature'], observation['feature']))
        direction_status = 'before' if before else 'after' if after else 'unknown'
        result.append({**observation, 'feature': None, 'score': round(score, 4),
                       'source_integrity': integrity[media['id']],
                       'status': decisions.get(observation['id'], 'pending'), 'query_time_status':time_match,
                       'direction_status': direction_status,
                       'location': media['location'], 'absolute_start': at,
                       'absolute_end': timestamp(media, observation['end']),
                       **time_fields(media, observation), 'media_path': media['path']})
    result.sort(key=lambda x: x['score'], reverse=True)
    return result[:limit]
