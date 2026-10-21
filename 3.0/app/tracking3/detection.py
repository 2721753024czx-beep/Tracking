"""Keep raw detection evidence that is absent from the SDK's tracking output."""
import importlib.metadata
import math
from pathlib import Path

from .store import fingerprint

POLICY = 'tracked-and-isolated-detections-v1'
RETAIN_CONFIDENCE = .25


def combine(raw, returned, selected):
    """Use SDK detection indices, not smoothed box overlap, to avoid duplicates."""
    if (len(selected) != len(returned) or len(set(selected)) != len(selected) or
            any(type(i) is not int or not 0 <= i < len(raw) for i in selected)):
        raise ValueError('跟踪返回的检测索引无法核验，停止生成候选')
    rows = []
    for (box, confidence, ident), index in zip(returned, selected):
        rows.append((box, confidence, ident, dict(raw_detection_index=index,
            detection_kind='tracked' if ident is not None else 'single_frame',
            retention_reason='tracker_returned' if ident is not None else 'returned_without_track_id')))
    for index, detection in enumerate(raw):
        if index in selected or detection['confidence'] < RETAIN_CONFIDENCE:
            continue
        if detection['class_id'] != 0:
            continue
        rows.append((detection['box'], detection['confidence'], None,
                     dict(raw_detection_index=index, detection_kind='single_frame', retention_reason='tracker_filtered')))
    return rows


class TrackedDetector:
    def __init__(self, weights, device):
        from ultralytics import YOLO
        import ultralytics
        from ultralytics.trackers.track import register_tracker, on_predict_postprocess_end
        self.model, self.device = YOLO(str(weights)), device
        installed = Path(ultralytics.__file__).parent
        self.policy = dict(name=POLICY, retain_confidence=RETAIN_CONFIDENCE,
                           weights_sha256=fingerprint(Path(weights)),
                           sdk_version=importlib.metadata.version('ultralytics'),
                           sdk_sha256={name: fingerprint(installed / name) for name in (
                               'engine/model.py', 'trackers/track.py', 'trackers/byte_tracker.py', 'cfg/trackers/bytetrack.yaml')})
        self.sample = None
        register_tracker(self.model, persist=True)
        callbacks = self.model.callbacks['on_predict_postprocess_end']
        position = next(i for i, cb in enumerate(callbacks) if getattr(cb, 'func', None) is on_predict_postprocess_end)
        callbacks.insert(position, self._capture_raw)
        self._callback_order = tuple(getattr(cb, 'func', cb) for cb in callbacks)

    def _capture_raw(self, predictor):
        if len(predictor.results) != 1:
            raise ValueError('检测批次不符合单帧索引要求')
        boxes = predictor.results[0].boxes
        self.raw = [dict(box=box, confidence=float(score), class_id=int(cls)) for box, score, cls in
                    zip(boxes.xyxy.cpu().tolist(), boxes.conf.cpu().tolist(), boxes.cls.cpu().tolist())]
        tracker = predictor.trackers[0]
        if not getattr(tracker, '_tracking3_capture_installed', False):
            original_update = tracker.update

            def update(*args, **kwargs):
                rows = original_update(*args, **kwargs)
                self.selected = []
                for row in rows:
                    value = float(row[-1])
                    if not math.isfinite(value) or value != int(value):
                        raise ValueError('跟踪检测索引无效')
                    self.selected.append(int(value))
                return rows

            tracker.update = update
            tracker._tracking3_capture_installed = True

    def __call__(self, rgb):
        self.raw, self.selected, self.sample = None, None, None
        result = self.model.track(rgb[:, :, ::-1].copy(), persist=True, classes=[0],
                                  tracker='bytetrack.yaml', device=self.device, verbose=False)[0]
        # SDK registrations must remain in place; changed callback semantics must
        # fail explicitly rather than silently stop preserving raw evidence.
        if (tuple(getattr(cb, 'func', cb) for cb in self.model.callbacks['on_predict_postprocess_end']) != self._callback_order or
                self.raw is None or self.selected is None):
            raise ValueError('检测跟踪回调已变化，请核对SDK后重新索引')
        boxes = result.boxes
        coords, scores = boxes.xyxy.cpu().tolist(), boxes.conf.cpu().tolist()
        ids = boxes.id.cpu().tolist() if boxes.id is not None else [None] * len(coords)
        if boxes.id is None and len(coords) == len(self.raw):
            selected = list(range(len(self.raw)))
        else:
            selected = self.selected
        returned = list(zip(coords, scores, ids))
        rows = combine(self.raw, returned, selected)
        self.sample = dict(raw_detections=self.raw, returned_detection_indices=selected,
                           tracker_returned_count=len(returned), retained_filtered_count=len(rows)-len(returned),
                           retention_confidence=RETAIN_CONFIDENCE, policy=POLICY)
        return rows
