from pathlib import Path
from math import sqrt
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.generate_demo_dataset import write_video
from tracking3.engine import Indexer, search
from tracking3.store import Store, fingerprint
from tracking3.track_rerank import GALLERY_POLICY, POLICY, _features, compare


class FixtureEncoder:
    version = 'fixture-multiframe'

    def extract(self, rgb):
        return {20: [.6, .8], 30: [1., 0.], 40: [0., 1.]}[rgb.shape[1]]


class TrackRerank(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        self.addCleanup(self.store.db.close)
        self.encoder = FixtureEncoder()
        video = self.store.root / 'uploads/camera.mp4'
        write_video(video, 0, 1.0)
        self.store.put('media', dict(id='media', camera_id='b', path='uploads/camera.mp4',
                                    location='fixture', fingerprint=fingerprint(video),
                                    status='unprocessed', processed_until=-1))
        samples = 0

        def detector(rgb):
            nonlocal samples
            samples += 1
            target = ([0, 10, 40, 80], .98, 1) if samples == 1 else ([10, 10, 40, 80], .75, 1)
            other = ([70, 10, 90, 80], .85, 2)
            return [target, other]

        Indexer(self.store, self.encoder, 'missing.pt').process('media', 5, detector=detector)
        self.target = next(o for o in self.store.list('observation') if o['local_track'] == '1')
        self.store.put('observation', dict(self.target, id='legacy', feature=[.8, .6]))
        self.store.put('observation', dict(self.target, id='manual', source='manual',
                                           feature=[.2, .979795897]))
        self.store.put('task', dict(id='task', version=1, status='active', entry='photo',
                                    cameras=['b'], encoder=self.encoder.version, feature=[1., 0.]))

    def test_multiframe_improves_clipped_representative_with_same_candidates(self):
        baseline = search(self.store, self.encoder, 'task')
        before = [tuple(row) for row in self.store.db.execute('SELECT * FROM documents ORDER BY kind,id')]
        with patch('tracking3.track_rerank._features', wraps=_features) as extract:
            report = compare(self.store.root, self.encoder, 'task')
        extract.assert_called_once()

        rows = report['candidates']
        self.assertEqual([(c['observation_id'], c['baseline_score']) for c in rows],
                         [(c['id'], c['score']) for c in baseline])
        self.assertEqual(sorted(c['multiframe_rank'] for c in rows), [1, 2, 3, 4])
        by_id = {c['observation_id']: c for c in rows}
        target = by_id[self.target['id']]
        self.assertEqual((target['baseline_rank'], target['baseline_score']), (4, 0.0))
        self.assertEqual((target['multiframe_rank'], target['multiframe_score']), (1, 1.0))
        self.assertEqual(target['representative_s'], 0.)
        self.assertEqual(target['policy'], POLICY)
        self.assertEqual([r['frame_index'] for r in target['selected_frames']], [2, 4, 6, 8])
        self.assertFalse(any(r['touches_boundary'] for r in target['selected_frames']))
        for ident, reason in [('legacy', 'insufficient_recorded_samples'), ('manual', 'manual_or_single_frame')]:
            result = by_id[ident]
            self.assertEqual(result['selection'], reason)
            self.assertEqual(result['baseline_score'], result['multiframe_score'])
        after = [tuple(row) for row in self.store.db.execute('SELECT * FROM documents ORDER BY kind,id')]
        self.assertEqual(after, before)

    def test_inconsistent_source_frame_provenance_aborts_comparison(self):
        row = self.store.list('index_frame')[1]
        row['width'] += 1
        self.store.put('index_frame', row)
        with self.assertRaisesRegex(ValueError, '源帧与实际采样记录不同'):
            compare(self.store.root, self.encoder, 'task')

    def test_gallery_is_opt_in_and_uses_two_best_cosines_on_same_frames(self):
        target_vectors = {2: [1., 0.], 4: [.8, .6]}

        def fixture_features(store, encoder, media, samples):
            vectors = [target_vectors.get(sample['frame_index'], [0., 1.])
                       if sample['detection']['local_track'] == '1' else [.9, sqrt(.19)]
                       for sample in samples]
            return vectors, []

        with patch('tracking3.track_rerank._features', side_effect=fixture_features) as extract:
            report = compare(self.store.root, self.encoder, 'task', compare_gallery=True)
            extract.assert_called_once()
            default = compare(self.store.root, self.encoder, 'task')

        self.assertEqual(report['gallery_policy'], GALLERY_POLICY)
        self.assertEqual(report['gallery_top_matches'], 2)
        by_id = {c['observation_id']: c for c in report['candidates']}
        target = by_id[self.target['id']]
        self.assertEqual(target['gallery_score'], .9)
        self.assertEqual(target['gallery_policy'], GALLERY_POLICY)
        self.assertNotEqual(target['gallery_score'], target['multiframe_score'])
        self.assertEqual(target['gallery_rank'], 2)  # Tied score favors the earlier baseline rank.
        for ident in ('legacy', 'manual'):
            result = by_id[ident]
            self.assertEqual(result['gallery_policy'], 'baseline')
            self.assertEqual(result['gallery_score'], result['baseline_score'])

        self.assertNotIn('gallery_policy', default)
        self.assertNotIn('gallery_top_matches', default)
        unchanged = [{k: v for k, v in c.items() if not k.startswith('gallery_')}
                     for c in report['candidates']]
        self.assertEqual(default['candidates'], unchanged)


if __name__ == '__main__':
    unittest.main()
