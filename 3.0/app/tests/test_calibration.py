import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tracking3.calibration import analyze
from tools.generate_calibration_demo import generate
from tools.prepare_time_calibration import prepare


def points(differences=(-1,-1,-1), bound=.02):
    return [dict(event_id=f'E{i}',reference_second=float(i*2+1),
                 target_second=float(i*2+1)-offset, reference_tolerance=bound,
                 target_tolerance=bound,evidence='受控相同事件') for i,offset in enumerate(differences)]


class CalibrationMath(unittest.TestCase):
    def test_constant_offset_interval_and_no_invented_registered_uncertainty(self):
        result=analyze(points())
        self.assertEqual(result['status'],'consistent_at_observed_points')
        self.assertEqual(result['median_offset_to_reference_s'],-1)
        self.assertEqual(result['candidate_offset_to_reference_s'],-1)
        self.assertEqual(result['feasible_offset_interval_s'],[-1.04,-.96])
        self.assertTrue(result['compatible_at_observed_points'])
        self.assertIsNone(result['uncertainty_to_register'])
        self.assertFalse(result['time_metadata_changed'])

    def test_candidate_stays_in_feasible_interval_with_unequal_bounds(self):
        rows=points((-1,-1,-.95),bound=.1)
        rows[-1].update(reference_tolerance=.01,target_tolerance=.01)
        result=analyze(rows)
        lower,upper=result['feasible_offset_interval_s']
        self.assertLess(result['median_offset_to_reference_s'],lower)
        self.assertLessEqual(lower,result['candidate_offset_to_reference_s'])
        self.assertLessEqual(result['candidate_offset_to_reference_s'],upper)

    def test_drifting_offsets_do_not_pass_by_fitting_one_median(self):
        result=analyze(points((-1,-1.2,-1.6)))
        self.assertEqual(result['status'],'fixed_offset_inconsistent')
        self.assertEqual(result['feasible_offset_interval_s'],[])
        self.assertFalse(result['compatible_at_observed_points'])

    def test_unknown_bounds_are_not_zero_and_one_point_does_not_prove_stability(self):
        result=analyze(points(bound=None))
        self.assertEqual(result['status'],'annotation_bounds_unknown')
        self.assertIsNone(result['feasible_offset_interval_s'])
        self.assertIsNone(result['compatible_at_observed_points'])
        self.assertEqual(analyze(points()[:1])['status'],'insufficient_points')

    def test_event_order_conflict_and_unordered_input(self):
        rows=points();rows.reverse()
        self.assertEqual(analyze(rows)['status'],'consistent_at_observed_points')
        rows=points();rows[1]['target_second']=.5
        self.assertEqual(analyze(rows)['status'],'event_order_conflict')

    def test_bad_ids_seconds_bounds_and_missing_evidence(self):
        for key,value in [('event_id',''),('reference_second',None),('target_second',float('nan')),
                          ('reference_tolerance',-1),('target_tolerance',False),('evidence','')]:
            rows=points();rows[0][key]=value
            with self.subTest(key=key), self.assertRaises(ValueError): analyze(rows)
        for key in ('event_id','reference_second','target_second'):
            rows=points();rows[1][key]=rows[0][key]
            with self.subTest(duplicate=key), self.assertRaises(ValueError): analyze(rows)


class CalibrationReview(unittest.TestCase):
    def snapshot(self, root):
        con=sqlite3.connect((root/'data/tracking.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)
        try: return con.execute('select * from documents order by kind,id').fetchall(),con.execute('select * from audit order by id').fetchall()
        finally:con.close()

    def test_real_decoded_shared_events_hash_binding_and_database_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root=generate(Path(directory)/'source');before=self.snapshot(root)
            for name,status in [('constant','consistent_at_observed_points'),('drift','fixed_offset_inconsistent'),('unknown','annotation_bounds_unknown')]:
                result=prepare(root/'data',root/f'{name}.json',root/f'review-{name}')
                self.assertEqual(result['status'],status)
                self.assertTrue(result['frames_match_requested_times'])
                self.assertFalse(result['event_identity_checked_by_tool'])
                self.assertEqual(len(result['decoded_frames']),3)
                for row in result['decoded_frames']:
                    for role in ('reference','target'):
                        self.assertTrue((root/f'review-{name}'/row[role]['image']).is_file())
            self.assertEqual(self.snapshot(root),before)

    def test_non_frame_time_requires_review_and_out_of_range_has_no_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root=generate(Path(directory)/'source');data=json.loads((root/'constant.json').read_text())
            data['points'][0]['reference_second']=1.01
            (root/'off-frame.json').write_text(json.dumps(data))
            result=prepare(root/'data',root/'off-frame.json',root/'off-frame')
            self.assertEqual(result['status'],'frame_selection_needs_review')
            self.assertIsNone(result['compatible_at_observed_points'])
            self.assertIsNone(result['candidate_offset_to_reference_s'])
            self.assertAlmostEqual(result['decoded_frames'][0]['reference']['actual_media_s'],1.1)
            data['points'][-1]['target_second']=100
            (root/'out-of-range.json').write_text(json.dumps(data))
            with self.assertRaises(ValueError):prepare(root/'data',root/'out-of-range.json',root/'invalid')
            self.assertFalse((root/'invalid').exists())

    def test_wrong_input_binding_changed_source_and_existing_output_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=generate(Path(directory)/'source');data=json.loads((root/'constant.json').read_text());before=self.snapshot(root)
            data['reference_fingerprint']='wrong';(root/'wrong.json').write_text(json.dumps(data))
            with self.assertRaises(ValueError):prepare(root/'data',root/'wrong.json',root/'wrong-review')
            self.assertFalse((root/'wrong-review').exists())
            (root/'existing').mkdir()
            with self.assertRaises(ValueError):prepare(root/'data',root/'constant.json',root/'existing')
            with (root/'data/uploads/fixed.mp4').open('ab') as handle:handle.write(b'changed')
            with self.assertRaises(ValueError):prepare(root/'data',root/'constant.json',root/'changed-review')
            self.assertFalse((root/'changed-review').exists())
            self.assertEqual(self.snapshot(root),before)
