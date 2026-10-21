import tempfile,unittest
from unittest.mock import patch
from pathlib import Path
from PIL import Image
import numpy as np
from tracking3.engine import Encoder,search
from tracking3.store import Store,fingerprint
from tracking3.observations import change
from tracking3.connections import change as link

class ObservationRevisions(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name);self.e=Encoder()
  (self.s.root/'uploads/a').write_bytes(b'fixture')
  self.rgb=np.full((80,60,3),120,dtype=np.uint8);self.f=self.e.extract(self.rgb)
  self.s.put('media',dict(id='m',path='uploads/a',fingerprint=fingerprint(self.s.root/'uploads/a'),camera_id='A',location='fixture',duration=30,width=60,height=80,status='complete',relative_start=0,reference_group='r',uncertainty=0,offset=0,start_epoch=None))
  for t in ['t','other']:
   self.s.put('task',dict(id=t,status='active',version=1,encoder=self.e.version,feature=self.f,cameras=[],name=t,entry='photo'))
  for i in ['a','b']:
   self.s.put('observation',dict(id=i,media_id='m',camera_id='A',start=1 if i=='a' else 10,end=3 if i=='a' else 12,representative=2 if i=='a' else 11,box=[0,0,20,30],crop='crops/a.jpg',feature=self.f,encoder=self.e.version))
   self.s.decide('t',i,'confirmed')
  self.s.decide('other','a','confirmed')
  self.c=link(self.s,dict(task_id='t',from_observation='a',to_observation='b',status='confirmed',evidence='fixture'))['connection']
 def tearDown(self):self.s.db.close();self.tmp.cleanup()
 def request(self,**kw):
  return dict(task_id='t',expected_task_version=self.s.get('task','t')['version'],observation_id='a',start=1,end=4,representative=2,box=[0,0,20,40],evidence='checked source',**kw)
 def apply(self,d):
  with patch('tracking3.observations.read_frame',return_value=(self.rgb,2)):
   return change(self.s,self.e,d)
 def test_revision_preserves_global_source_and_other_task(self):
  old=self.s.get('observation','a');other=self.s.get('task','other')
  new=self.apply(self.request())['observation']
  self.assertEqual(self.s.get('observation','a'),old)
  self.assertEqual(self.s.get('task','other'),other)
  self.assertEqual(self.s.get('decision','other:a')['status'],'confirmed')
  self.assertEqual(self.s.get('connection',self.c['id'])['status'],'needs_review')
  self.assertEqual(self.s.get('decision','t:'+new['id'])['status'],'pending')
  self.assertNotIn('a',[o['id'] for o in search(self.s,self.e,'t')])
  self.assertNotIn(new['id'],[o['id'] for o in search(self.s,self.e,'other')])
  with self.assertRaises(ValueError):self.s.decide('t','a','confirmed')
  with self.assertRaises(ValueError):self.s.decide('other',new['id'],'confirmed')
 def test_invalid_stale_and_changed_source_rejected(self):
  d=self.request();d['expected_task_version']=-1
  with self.assertRaises(ValueError):self.apply(d)
  d=self.request();d['box']=[0,0,61,40]
  with self.assertRaises(ValueError):self.apply(d)
  (self.s.root/'uploads/a').write_bytes(b'changed')
  with self.assertRaises(ValueError):self.apply(self.request())
 def test_audit_failure_rolls_back_and_cleans_crop(self):
  before=self.s.get('task','t');count=len(self.s.list('observation'))
  with patch.object(self.s,'audit',side_effect=RuntimeError('failure')):
   with self.assertRaises(RuntimeError):self.apply(self.request())
  self.assertEqual(self.s.get('task','t'),before)
  self.assertEqual(len(self.s.list('observation')),count)
  self.assertEqual(self.s.get('decision','t:a')['status'],'confirmed')
  self.assertEqual(self.s.get('connection',self.c['id'])['status'],'confirmed')
  self.assertFalse(list((self.s.root/'crops').glob('manual-*')))
 def test_add_missing_observation_without_auto_confirmation(self):
  d=self.request();d.pop('observation_id');d['media_id']='m'
  o=self.apply(d)['observation']
  self.assertIsNone(o['replaces']);self.assertEqual(o['owner_task'],'t')
  self.assertEqual(self.s.get('decision','t:a')['status'],'confirmed')
  self.assertEqual(self.s.get('connection',self.c['id'])['status'],'confirmed')
