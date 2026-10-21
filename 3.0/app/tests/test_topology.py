import tempfile
import unittest

from tracking3.engine import Encoder, search
from tracking3.store import Store, fingerprint
from tracking3.topology import configuration, update, scope, set_scope, before_anchor


class Topology(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.s=Store(self.tmp.name);self.encoder=Encoder()
        (self.s.root/'x.mp4').write_bytes(b'topology fixture source')
        self.s.put('task',dict(id='t',version=1,status='active',cameras=[],anchor=None,
            encoder=self.encoder.version,feature=[1,0]))
        for i, c in enumerate('ABC'):
            self.s.put('media',dict(id=c,camera_id=c,location=c,path='x.mp4',status='complete',
                fingerprint=fingerprint(self.s.root/'x.mp4'),
                relative_start=i*10,reference_group='r',uncertainty=0,offset=0,start_epoch=None))
            self.s.put('observation',dict(id=c,media_id=c,camera_id=c,start=0,end=2,
                encoder=self.encoder.version,feature=[1,0]))
            self.s.decide('t',c,'confirmed')

    def tearDown(self):
        self.s.db.close();self.tmp.cleanup()

    def choose(self,mode,anchor='A'):
        return set_scope(self.s,dict(task_id='t',expected_task_version=self.s.get('task','t')['version'],
            mode=mode,anchor_id=anchor))

    def test_neighbor_scope_and_expansion_finds_unconfigured_camera(self):
        update(self.s,dict(expected_version=0,edges=[dict(**{'from':'A','to':'B'},evidence='test')]))
        task=self.choose('neighbors')
        self.assertEqual(scope(self.s,task)['remaining_cameras'],['C'])
        self.assertEqual({o['camera_id'] for o in search(self.s,self.encoder,'t')},{'A','B'})
        self.choose('all')
        self.assertEqual({o['camera_id'] for o in search(self.s,self.encoder,'t')},{'A','B','C'})
        self.assertEqual(len(self.s.decisions('t')),3)

    def test_missing_configuration_falls_back_and_missing_video_is_visible(self):
        task=self.choose('neighbors')
        self.assertTrue(scope(self.s,task)['fallback'])
        self.assertEqual(scope(self.s,task)['selected_cameras'],['A','B','C'])
        update(self.s,dict(expected_version=0,edges=[dict(**{'from':'A','to':'missing'},evidence='test')]))
        ctx=scope(self.s,self.s.get('task','t'))
        self.assertEqual(ctx['unavailable_neighbors'],['missing'])
        self.assertEqual(ctx['selected_cameras'],['A'])
        self.assertEqual(ctx['remaining_cameras'],['B','C'])

    def test_stale_config_scope_and_revoked_anchor_are_rejected(self):
        task=self.choose('neighbors')
        update(self.s,dict(expected_version=0,edges=[]))
        with self.assertRaises(ValueError):
            set_scope(self.s,dict(task_id='t',expected_task_version=task['version'],mode='all'))
        with self.assertRaises(ValueError):update(self.s,dict(expected_version=0,edges=[]))
        self.s.decide('t','A','pending')
        with self.assertRaises(ValueError):search(self.s,self.encoder,'t')
        self.choose('all','B')
        self.assertEqual({o['camera_id'] for o in search(self.s,self.encoder,'t')},{'B','C'})

    def test_unknown_clock_and_overlap_retained_task_boundary_preserved(self):
        task=self.s.get('task','t');task['cameras']=['A','B'];self.s.put('task',task)
        self.choose('all','B')
        a=self.s.get('media','A');a['uncertainty']=None;self.s.put('media',a)
        self.assertEqual({o['camera_id'] for o in search(self.s,self.encoder,'t')},{'A','B'})
        ctx=scope(self.s,self.s.get('task','t'));self.assertEqual(ctx['outside_task_cameras'],['C'])
        a['uncertainty']=10;self.s.put('media',a)
        self.assertFalse(before_anchor(self.s,self.s.get('observation','B'),self.s.get('observation','A')))

    def test_forward_direction_keeps_before_candidates_and_excludes_after(self):
        task=self.choose('all','B')
        task['search_direction']='before';self.s.put('task',task)
        result=search(self.s,self.encoder,'t')
        self.assertEqual({o['camera_id'] for o in result},{'A','B'})
        self.assertTrue(all(o['direction_status'] in ('before','unknown') for o in result))


if __name__=='__main__': unittest.main()
