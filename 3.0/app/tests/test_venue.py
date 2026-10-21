import tempfile
import unittest
from pathlib import Path

from tracking3.store import Store
from tracking3.venue import camera, configuration, update


class VenueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_update_is_versioned_and_camera_metadata_is_lookupable(self):
        saved = update(self.store, {
            'expected_version': 0,
            'name': '宿舍模拟场景',
            'cameras': [{'id': 'cam_a', 'label': '入口机位', 'floor': '3F',
                         'area': '北门', 'orientation': '朝东', 'notes': '固定支架'}],
        })
        self.assertEqual(saved['version'], 1)
        self.assertEqual(camera(self.store, 'cam_a')['area'], '北门')
        with self.assertRaisesRegex(ValueError, '已变化'):
            update(self.store, {'expected_version': 0, 'name': '旧', 'cameras': []})

    def test_duplicate_and_invalid_camera_ids_rejected(self):
        with self.assertRaisesRegex(ValueError, '不能重复'):
            update(self.store, {'expected_version': 0, 'cameras': [{'id': 'A'}, {'id': 'A'}]})
        with self.assertRaisesRegex(ValueError, '不能为空'):
            update(self.store, {'expected_version': 0, 'cameras': [{'id': '  '}]})

    def test_default_configuration_is_empty_and_unknown_camera_is_none(self):
        self.assertEqual(configuration(self.store)['version'], 0)
        self.assertIsNone(camera(self.store, 'missing'))
