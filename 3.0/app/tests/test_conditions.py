import tempfile
import unittest

from tracking3.conditions import values, match, update
from tracking3.coverage import build
from tracking3.store import Store
from tracking3.topology import set_scope, scope


class Conditions(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name)
        self.s.put('task',dict(id='t',version=1,status='active',cameras=[],anchor=None))
    def tearDown(self):
        self.s.db.close();self.tmp.cleanup()
    def media(self,ident,start,duration=5,error=0,status='complete',camera='A'):
        m=dict(id=ident,camera_id=camera,name=ident,status=status,reference_group='r',relative_start=start,
            offset=0,uncertainty=error,start_epoch=None,duration=duration,processed_until=2,sample_fps=3)
        self.s.put('media',m);return m
    def test_relative_bounds_include_uncertainty_and_unknown_basis(self):
        task=values(dict(time_filter='relative',query_group='r',relative_from=10,relative_to=20))
        o=dict(start=0,end=2)
        self.assertEqual(match(task,self.media('a',0),o),'outside')
        self.assertEqual(match(task,self.media('b',8,error=2),o),'included')
        self.assertEqual(match(task,self.media('c',0,error=None),o),'unknown')
        m=self.media('d',0);m['reference_group']='other';self.assertEqual(match(task,m,o),'unknown')
        m=self.media('e',10);m['start_epoch']=1000
        self.assertEqual(match(task,m,o),'included') # Relative group still usable despite wall clock.
    def test_editing_preserves_evidence_and_stale_edit_is_rejected(self):
        self.media('a',0);self.s.put('observation',dict(id='o',media_id='a'))
        self.s.decide('t','o','confirmed');v=self.s.get('task','t')['version']
        update(self.s,dict(task_id='t',expected_task_version=v,cameras=['A','B'],time_filter='any'))
        self.assertEqual(self.s.get('decision','t:o')['status'],'confirmed')
        with self.assertRaises(ValueError):update(self.s,dict(task_id='t',expected_task_version=v))
    def test_nominal_gap_overlap_and_unprocessed_tail_are_separate(self):
        self.media('a',0);self.media('b',4);self.media('c',12,status='paused')
        task=self.s.get('task','t');task.update(values(dict(time_filter='relative',query_group='r',relative_from=0,relative_to=20)))
        g=build(self.s,task)['groups'][0]
        self.assertEqual(g['available'],[[0,9],[12,17]])
        self.assertEqual(g['nominal_gaps'],[[9,12],[17,20]])
        self.assertEqual(g['nominal_overlaps'],[[4,5]])
        self.assertEqual(g['files'][-1]['unprocessed_tail'],[2,5])
        self.assertIsNone(g['files'][0]['unprocessed_tail'])
    def test_unknown_clock_not_certified_and_missing_camera_visible(self):
        self.media('a',0,error=None)
        task=self.s.get('task','t');task.update(values(dict(cameras=['A','missing'],time_filter='relative',query_group='r',relative_from=10,relative_to=20)))
        data=build(self.s,task)
        self.assertEqual(data['missing_cameras'],['missing'])
        self.assertFalse(data['groups'][0]['timing_certain'])
        self.assertTrue(data['groups'][0]['files'][0]['query_time_unknown'])
    def test_invalid_bounds_and_explicit_clear_of_monitor_anchor(self):
        for d in [dict(time_filter='relative',query_group='r',relative_from=20,relative_to=10),
                  dict(time_filter='relative'),dict(cameras='A'),dict(relative_from='nan',time_filter='relative',query_group='r')]:
            with self.assertRaises(ValueError):values(d)
        m=self.media('a',0);self.s.put('observation',dict(id='o',media_id=m['id']))
        t=self.s.get('task','t');t['anchor']='o';self.s.put('task',t);self.s.decide('t','o','confirmed')
        t=self.s.get('task','t');t=set_scope(self.s,dict(task_id='t',expected_task_version=t['version'],mode='all',anchor_id=None))
        self.assertIsNone(scope(self.s,t)['anchor_id'])

if __name__=='__main__': unittest.main()
