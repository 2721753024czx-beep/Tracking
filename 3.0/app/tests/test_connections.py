import tempfile
import unittest
from unittest.mock import patch

from tracking3.connections import change, for_task, preview, source_review
from tracking3.store import fingerprint
from tracking3.server import report
from tracking3.store import Store
from tracking3.timing import update, acknowledge


class Connections(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.put('task', dict(id='task', version=1, status='active', cameras=[],
                                   name='连接测试', entry='photo', encoder='hsv-body-test'))
        for i, ident in enumerate('abcd'):
            source=self.store.root/'uploads'/ident;source.write_bytes(ident.encode())
            self.store.put('media', dict(id=ident, camera_id=ident, reference_group='r',
                path='uploads/'+ident,fingerprint=fingerprint(source),
                relative_start=0, offset=0, uncertainty=1, start_epoch=None, time_version=1,
                time_evidence='人工核对', location=ident, name=ident, status='complete'))
            self.store.put('observation', dict(id=ident, media_id=ident, start=i*10, end=i*10+5,
                                              crop='crops/example.jpg'))
            self.store.decide('task', ident, 'confirmed')

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def link(self, a, b, dependencies=(), relation='transition', status='confirmed'):
        return change(self.store, dict(task_id='task', from_observation=a, to_observation=b,
                        depends_on=list(dependencies), relation=relation, status=status,
                        evidence='人工回看两端片段'))['connection']

    def revise(self, c, status):
        return change(self.store, dict(task_id='task', id=c['id'],
                        expected_version=c['version'], status=status))

    def test_transitive_undo_preserves_independent_identity_and_connection(self):
        ab = self.link('a', 'b')
        bc = self.link('b', 'c', [ab['id']])
        cd = self.link('c', 'd', [bc['id']])
        independent = self.link('a', 'd')
        result = self.revise(ab, 'revoked')
        self.assertEqual(set(result['affected']), {bc['id'], cd['id']})
        self.assertEqual(self.store.get('connection', independent['id'])['status'], 'confirmed')
        self.assertTrue(all(d['status']=='confirmed' for d in self.store.decisions('task')))
        with self.assertRaises(ValueError):
            self.revise(self.store.get('connection', bc['id']), 'confirmed')
        self.revise(self.store.get('connection', ab['id']), 'confirmed')
        self.revise(self.store.get('connection', bc['id']), 'confirmed')
        self.revise(self.store.get('connection', cd['id']), 'confirmed')
        self.assertEqual(self.store.get('connection', cd['id'])['status'], 'confirmed')

    def test_source_failure_marks_dependency_evidence_without_erasing_history(self):
        for ident in 'abcd':
            path=self.store.root/'uploads'/ident;path.write_bytes(ident.encode())
            m=self.store.get('media',ident);m.update(path='uploads/'+ident,fingerprint=fingerprint(path));self.store.put('media',m)
        ab=self.link('a','b');cd=self.link('c','d',[ab['id']]);independent=self.link('b','d')
        original=self.store.get('task','task')
        (self.store.root/'uploads/a').write_bytes(b'changed')
        states={c['id']:c['source_valid'] for c in source_review(self.store,'task')}
        self.assertFalse(states[ab['id']]);self.assertFalse(states[cd['id']]);self.assertTrue(states[independent['id']])
        self.assertEqual(self.store.get('task','task'),original)
        self.assertEqual(self.store.get('connection',cd['id'])['status'],'confirmed')
        with self.assertRaises(ValueError):self.revise(self.store.get('connection',ab['id']),'confirmed')
        with self.assertRaises(ValueError):self.revise(self.store.get('connection',cd['id']),'confirmed')

    def test_identity_reversal_invalidates_connections_not_other_identities(self):
        ab = self.link('a','b')
        bc = self.link('b','c',[ab['id']])
        cd = self.link('c','d')
        changed = self.store.decide('task','b','rejected')
        self.assertEqual(set(changed),{ab['id'],bc['id']})
        self.assertEqual(self.store.get('connection',cd['id'])['status'],'confirmed')
        self.assertEqual(self.store.get('decision','task:c')['status'],'confirmed')
        with self.assertRaises(ValueError):
            self.revise(self.store.get('connection',ab['id']),'confirmed')

    def test_clock_update_invalidates_and_ack_does_not_reconfirm(self):
        ab = self.link('a','b')
        bc = self.link('b','c',[ab['id']])
        update(self.store,'a',dict(reference_group='r',relative_start=1,offset=0,
                                  uncertainty=1,time_evidence='重新校时'))
        self.assertEqual(self.store.get('connection',bc['id'])['status'],'needs_review')
        with self.assertRaises(ValueError):
            self.revise(self.store.get('connection',ab['id']),'confirmed')
        acknowledge(self.store,'task',{m['id']:m.get('time_version',1) for m in self.store.list('media')})
        self.assertEqual(self.store.get('connection',ab['id'])['status'],'needs_review')
        content=self.store.asset(report(self.store,'task')).read_text()
        self.assertIn('needs_review',content)
        self.assertIn('素材时间已修订',content)

    def test_stale_versions_and_foreign_dependencies_are_rejected(self):
        ab=self.link('a','b')
        self.revise(ab,'pending')
        with self.assertRaises(ValueError):
            self.revise(ab,'confirmed')
        self.store.put('task',dict(id='other',version=1,status='active'))
        with self.assertRaises(ValueError):
            change(self.store,dict(task_id='other',from_observation='b',to_observation='c',
                                  depends_on=[ab['id']],status='pending'))
        with self.assertRaises(ValueError):
            change(self.store,dict(task_id='task',from_observation='a',to_observation='d',
                                  expected_task_version=-1,status='pending'))

    def test_unknown_clock_keeps_relationship_uncertain_and_reversed_time_blocks(self):
        with self.assertRaises(ValueError):
            self.link('c','a')
        c=self.link('c','a',status='pending')
        self.assertEqual(self.revise(c,'revoked')['connection']['status'],'revoked')
        m=self.store.get('media','a');m['uncertainty']=None;self.store.put('media',m)
        ab=self.link('a','b',relation='unknown')
        self.assertFalse(ab['assessment']['comparable'])
        self.assertIsNone(ab['assessment']['gap_range'])
        self.assertEqual(ab['relation'],'unknown')

    def test_impact_preview_and_version_checked_identity_change(self):
        ab=self.link('a','b');bc=self.link('b','c',[ab['id']]);cd=self.link('c','d')
        impact=preview(self.store,'task',connection_id=ab['id'])
        self.assertEqual([c['id'] for c in impact['affected']],[bc['id']])
        self.assertTrue(impact['identities_preserved'])
        identity=preview(self.store,'task',observation_id='b')
        self.assertEqual({c['id'] for c in identity['affected']},{ab['id'],bc['id']})
        self.store.decide('task','d','pending')
        with self.assertRaises(ValueError):
            self.store.decide('task','b','pending',identity['task_version'])
        self.assertEqual(self.store.get('decision','task:b')['status'],'confirmed')

    def test_transaction_failure_rolls_back_decision_link_and_task(self):
        ab=self.link('a','b')
        before=self.store.get('task','task')['version']
        with patch.object(self.store,'audit',side_effect=RuntimeError('injected write failure')):
            with self.assertRaises(RuntimeError):
                self.store.decide('task','b','rejected')
        self.assertEqual(self.store.get('decision','task:b')['status'],'confirmed')
        self.assertEqual(self.store.get('connection',ab['id'])['status'],'confirmed')
        self.assertEqual(self.store.get('task','task')['version'],before)
        with patch.object(self.store,'audit',side_effect=RuntimeError('injected write failure')):
            with self.assertRaises(RuntimeError):
                self.revise(ab,'revoked')
        self.assertEqual(self.store.get('connection',ab['id'])['status'],'confirmed')
        self.assertEqual(self.store.get('task','task')['version'],before)


if __name__ == '__main__':
    unittest.main()
