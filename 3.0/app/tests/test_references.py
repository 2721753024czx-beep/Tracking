import tempfile
import unittest
from pathlib import Path

from tracking3.engine import Encoder
from tracking3.references import configure
from tracking3.store import Store, fingerprint


class References(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        source = self.store.root / 'source.mp4'
        source.write_bytes(b'reference source')
        self.store.put('media', dict(id='m', path='source.mp4', camera_id='A', location='A',
                                     fingerprint=fingerprint(source), status='complete'))
        self.store.put('task', dict(id='t', version=1, status='active', encoder=Encoder().version,
                                    feature=[1.0, 0.0], reference_observations=[],
                                    reference_review_required=False))
        for ident, feature in [('a', [1.0, 0.0]), ('b', [0.0, 1.0])]:
            self.store.put('observation', dict(id=ident, media_id='m', feature=feature,
                                               encoder=Encoder().version, start=0, end=1))
            self.store.decide('t', ident, 'confirmed')

    def tearDown(self):
        self.tmp.cleanup()

    def test_configure_averages_confirmed_references_and_requires_refresh(self):
        task = configure(self.store, {'task_id': 't', 'expected_task_version': 3,
                                      'observation_ids': ['a', 'b']})
        self.assertEqual(task['reference_observations'], ['a', 'b'])
        self.assertAlmostEqual(task['feature'][0], 2 ** -0.5, places=5)
        self.assertEqual(task['version'], 4)
        self.store.decide('t', 'a', 'pending', expected_task_version=4)
        self.assertTrue(self.store.get('task', 't')['reference_review_required'])

    def test_pending_observation_cannot_become_reference(self):
        self.store.decide('t', 'b', 'pending', expected_task_version=3)
        with self.assertRaisesRegex(ValueError, '必须先确认'):
            configure(self.store, {'task_id': 't', 'expected_task_version': 4,
                                   'observation_ids': ['a', 'b']})

    def test_incompatible_encoder_cannot_pollute_query_even_with_same_dimension(self):
        for encoder in ('other-model', None):
            observation = self.store.get('observation', 'b')
            observation['encoder'] = encoder
            self.store.put('observation', observation)
            before = self.store.get('task', 't')
            audit_count = self.store.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0]
            with self.assertRaisesRegex(ValueError, '编码器'):
                configure(self.store, {'task_id': 't', 'expected_task_version': 3,
                                       'observation_ids': ['a', 'b']})
            self.assertEqual(self.store.get('task', 't'), before)
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0], audit_count)

    def test_wrong_feature_dimension_cannot_replace_task_query(self):
        observation = self.store.get('observation', 'b')
        observation['feature'] = [1., 0., 0.]
        self.store.put('observation', observation)
        before = self.store.get('task', 't')
        with self.assertRaisesRegex(ValueError, '维度'):
            configure(self.store, {'task_id': 't', 'expected_task_version': 3,
                                   'observation_ids': ['b']})
        self.assertEqual(self.store.get('task', 't'), before)


if __name__ == '__main__':
    unittest.main()
