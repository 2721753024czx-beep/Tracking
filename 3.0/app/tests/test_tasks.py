import tempfile,unittest
from unittest.mock import patch
from tracking3.store import Store
from tracking3.tasks import change_status

class TaskLifecycle(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name)
  self.s.put('task',dict(id='t',version=1,status='active',name='fixture'))
 def tearDown(self):self.s.db.close();self.tmp.cleanup()
 def change(self,status,version=None):
  return change_status(self.s,dict(task_id='t',status=status,reason='operator',expected_task_version=version if version is not None else self.s.get('task','t')['version']))
 def test_stale_requests_noop_and_end_reopen(self):
  paused=self.change('paused');self.assertEqual(paused['version'],2)
  with self.assertRaises(ValueError):self.change('active',1)
  same=self.change('paused');self.assertEqual(same,paused)
  ended=self.change('ended');self.assertEqual(ended['version'],3)
  with self.assertRaises(ValueError):self.change('paused')
  self.assertEqual(self.change('active')['version'],4)
  self.assertEqual(self.s.db.execute("SELECT count(*) FROM audit WHERE action='status'").fetchone()[0],3)
 def test_audit_failure_rolls_back_state_and_version(self):
  before=self.s.get('task','t')
  with patch.object(self.s,'audit',side_effect=RuntimeError('write failure')):
   with self.assertRaises(RuntimeError):self.change('paused')
  self.assertEqual(self.s.get('task','t'),before)
  self.assertEqual(self.s.db.execute('SELECT count(*) FROM audit').fetchone()[0],0)
