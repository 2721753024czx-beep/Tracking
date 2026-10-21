import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tracking3.detection import combine
from tracking3.engine import search
from tracking3.evaluation_export import export
from tracking3.server import report
from tracking3.trace_review import inspect, preview
from tracking3.index_trace import snapshot
import test_index_trace as fixture


class DetectionSelection(unittest.TestCase):
    def test_indices_avoid_duplicate_smoothed_boxes_and_preserve_untracked_high_person(self):
        raw = [dict(box=[0,0,20,40], confidence=.9, class_id=0),
               dict(box=[30,0,50,40], confidence=.8, class_id=0),
               dict(box=[60,0,80,40], confidence=.24, class_id=0)]
        result = combine(raw, [([2,1,22,41], .9, 7)], [0])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0][0], [2,1,22,41])
        self.assertEqual(result[1][:3], ([30,0,50,40], .8, None))
        self.assertEqual(result[1][3]['retention_reason'], 'tracker_filtered')

    def test_empty_tracker_keeps_high_raw_and_no_track_output_does_not_duplicate(self):
        raw = [dict(box=[0,0,20,40], confidence=.25, class_id=0)]
        self.assertEqual(len(combine(raw, [], [])), 1)
        result = combine(raw, [([0,0,20,40], .25, None)], [0])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][3]['retention_reason'], 'returned_without_track_id')
        self.assertEqual(combine([dict(raw[0], class_id=1)], [], []), [])

    def test_invalid_or_duplicate_sdk_index_refuses_evidence(self):
        raw = [dict(box=[0,0,20,40], confidence=.9, class_id=0)]
        for selected, returned in [([9], [([0,0,20,40],.9,1)]),
                                   ([0,0], [([0,0,20,40],.9,1)] * 2),
                                   ([], [([0,0,20,40],.9,1)]),
                                   ([True], [([0,0,20,40],.9,1)])]:
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                combine(raw, returned, selected)


class SingleFrameWorkflow(unittest.TestCase):
    setUp = fixture.IndexTrace.setUp
    tearDown = fixture.IndexTrace.tearDown
    make_task = fixture.IndexTrace.make_task

    def test_resume_old_detection_policy_refuses_mixed_evidence(self):
        self.indexer.weights = self.store.root / 'weights.pt'
        self.indexer.weights.write_bytes(b'fixture')
        self.media.update(status='paused', processed_until=.2)
        self.store.put('media', self.media)
        detector = lambda rgb: []
        detector.policy = dict(name='new-policy')
        with patch('tracking3.detection.TrackedDetector', return_value=detector):
            with self.assertRaisesRegex(ValueError, '检测策略'):
                self.indexer.process('m')
        self.assertEqual(self.store.get('media','m'), self.media)
        self.assertEqual(self.store.list('index_frame'), [])

    def test_raw_binding_and_duplicate_preserved_indices_refuse_snapshot(self):
        class Detector:
            def __call__(self, rgb):
                raw = [dict(box=[10,10,40,80],confidence=.8,class_id=0)]
                self.sample = dict(raw_detections=raw,returned_detection_indices=[],tracker_returned_count=0,
                                   retained_filtered_count=1,retention_confidence=.25,policy='tracked-and-isolated-detections-v1')
                return combine(raw,[],[])
        self.indexer.process('m',5,detector=Detector())
        frames = snapshot(self.store, {'m'})['frames']
        self.assertEqual(len(frames),5)
        original = frames[0]
        changed = json.loads(json.dumps(original))
        changed['detections'][0]['box'][0] += 1
        self.store.put('index_frame',changed)
        with self.assertRaisesRegex(ValueError,'原始检测'):
            snapshot(self.store, {'m'})
        changed = json.loads(json.dumps(original))
        changed['detections'].append(changed['detections'][0])
        changed['detector_sample']['retained_filtered_count'] = 2
        self.store.put('index_frame',changed)
        with self.assertRaisesRegex(ValueError,'缺失或重复'):
            snapshot(self.store, {'m'})

    def test_adjacent_detections_stay_separate_pending_with_exact_preview_and_export(self):
        def detector(rgb):
            return [([10,10,40,80],.9,5),
                    ([45,10,70,80],.8,99,dict(detection_kind='single_frame',retention_reason='tracker_filtered',raw_detection_index=1))]
        self.indexer.process('m', 5, detector=detector)
        self.make_task()
        task = self.store.get('task', 't')
        task.update(name='single-frame fixture', entry='photo')
        self.store.put('task', task)
        observations = self.store.list('observation')
        isolated = [o for o in observations if o.get('detection_kind') == 'single_frame']
        self.assertEqual(len(isolated), 5)
        self.assertEqual(len({o['id'] for o in isolated}), 5)
        self.assertTrue(all(o['start'] == o['end'] == o['representative'] for o in isolated))
        self.assertTrue(all(o['local_track'].startswith('frame:') for o in isolated))
        self.assertEqual(len([o for o in observations if o.get('detection_kind') != 'single_frame']), 1)
        candidates = search(self.store, self.encoder, 't', limit=100)
        self.assertTrue(all(o['status'] == 'pending' for o in candidates))
        observation = isolated[0]
        trace = inspect(self.store, 't', observation['id'])
        self.assertEqual(trace['sample_count'], 1)
        image = preview(self.store, 't', observation['id'], trace['samples'][0]['id'])
        self.assertEqual(image['actual_media_s'], observation['representative'])
        self.assertEqual(image['sample']['detection']['detection_kind'], 'single_frame')
        self.assertFalse(image['trajectory_identity_certified'])
        folder = Path(self.tmp.name) / 'single-frame-export'
        export(self.store.root, 't', folder)
        exported = json.loads((folder / 'candidates.json').read_text())
        self.assertEqual(len([o for o in exported if o.get('detection_kind') == 'single_frame']), 5)
        text = self.store.asset(report(self.store, 't')).read_text()
        self.assertIn('单帧检测线索（无连续轨迹', text)
        self.assertEqual(self.store.decisions('t'), [])


if __name__ == '__main__':
    unittest.main()
