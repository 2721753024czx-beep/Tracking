import tempfile
import unittest
from unittest.mock import patch

from tracking3.appearance_review import generate, decide, list_reviews
from tracking3.engine import Encoder, Indexer
from tracking3.store import Store, fingerprint
from tools.generate_demo_dataset import write_video


class AppearanceReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.path = self.store.root / 'uploads/a.mp4'
        write_video(self.path, 0, 1)
        self.encoder = Encoder()
        self.store.put('media', dict(id='m', camera_id='a', path='uploads/a.mp4',
            fingerprint=fingerprint(self.path), status='unprocessed', processed_until=-1))
        self.store.put('task', dict(id='t', version=1, status='active', encoder=self.encoder.version))
        Indexer(self.store, self.encoder, 'missing.pt').process('m', 5,
            detector=lambda rgb: [([10, 10, 40, 80], .9, 1)])
        self.observation = self.store.list('observation')[0]
        self.data = dict(task_id='t', observation_id=self.observation['id'],
            expected_task_version=1, threshold=.6)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def proposal(self):
        vectors = [[1,0,0], [0,1,0], [1,0,0], [0,0,1], [0,0,1]]
        with patch.object(self.encoder, 'extract', side_effect=vectors):
            return generate(self.store, self.encoder, self.data)

    def choice(self, review, **changes):
        return dict(task_id='t', review_id=review['id'], expected_task_version=1,
            expected_review_revision=review['revision'], segment=1, status='dismissed',
            reason='转身或框漂移，继续回看', **changes)

    def test_exact_source_samples_partition_warning_and_no_index_mutation(self):
        before = {kind:self.store.list(kind) for kind in ('task','observation','index_frame')}
        r = self.proposal()
        self.assertEqual([s['indices'] for s in r['segments']], [[0,1,2],[3,4]])
        self.assertEqual([w['index'] for w in r['warnings']], [1])
        self.assertEqual(r['segments'][1]['start'], .6)
        self.assertEqual(r['samples'][3]['frame_index'], 6)
        self.assertFalse(r['identity_certified'])
        self.assertEqual(len(r['crop_rgb_sha256']), 5)
        self.assertEqual(before, {kind:self.store.list(kind) for kind in before})
        self.assertTrue(list_reviews(self.store,'t',self.observation['id'])['items'][0]['valid'])

    def test_persisted_decisions_history_revision_and_no_identity_decision(self):
        r = self.proposal()
        changed = decide(self.store, self.choice(r))
        self.assertEqual(changed['revision'], 1)
        data = self.choice(changed);data['status']='draft_prepared';data['reason']='仅准备采样段草稿'
        changed = decide(self.store,data)
        self.assertEqual([v['status'] for v in changed['history']], ['dismissed','draft_prepared'])
        with self.assertRaisesRegex(ValueError,'核对记录已变化'):
            decide(self.store,data)
        self.assertEqual(self.store.get('task','t')['version'],1)
        self.assertEqual(self.store.list('decision'),[])
        self.assertEqual(list_reviews(self.store,'t',self.observation['id'])['items'][0]['revision'],2)

    def test_foreign_owner_pause_stale_superseded_and_manual_rejected(self):
        r=self.proposal();self.store.put('task',dict(id='other',version=1,status='active'))
        d=self.choice(r);d['task_id']='other'
        with self.assertRaisesRegex(ValueError,'不属于'):decide(self.store,d)
        for changes, message in [(dict(status='paused'),'暂停'),(dict(version=2),'任务已变化'),
                (dict(superseded_observations=[self.observation['id']]),'历史片段')]:
            t=dict(id='t',version=1,status='active',encoder=self.encoder.version,**{})
            t.update(changes);self.store.put('task',t)
            with self.assertRaisesRegex(ValueError,message):generate(self.store,self.encoder,self.data)
            with self.assertRaisesRegex(ValueError,message):decide(self.store,self.choice(r))
        self.store.put('task',dict(id='t',version=1,status='active',encoder=self.encoder.version))
        o=dict(self.observation,source='manual',owner_task='other');self.store.put('observation',o)
        with self.assertRaisesRegex(ValueError,'不属于'):generate(self.store,self.encoder,self.data)
        o['owner_task']='t';self.store.put('observation',o)
        with self.assertRaisesRegex(ValueError,'自动采样'):generate(self.store,self.encoder,self.data)

    def test_source_and_trace_changes_invalidate_or_refuse(self):
        r=self.proposal();row=self.store.list('index_frame')[0]
        row['detections'][0]['box']=[11.,10.,40.,80.];self.store.put('index_frame',row)
        self.assertFalse(list_reviews(self.store,'t',self.observation['id'])['items'][0]['valid'])
        with self.assertRaisesRegex(ValueError,'采样记录已变化'):decide(self.store,self.choice(r))
        self.path.write_bytes(self.path.read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError,'已改变'):generate(self.store,self.encoder,self.data)
        with self.assertRaisesRegex(ValueError,'已改变'):list_reviews(self.store,'t',self.observation['id'])

    def test_forged_pts_refused_before_saving(self):
        row=self.store.list('index_frame')[1];row['frame_index']+=1
        self.store.put('index_frame',row)
        with self.assertRaisesRegex(ValueError,'源帧'):generate(self.store,self.encoder,self.data)
        self.assertEqual(self.store.list('appearance_review'),[])

    def test_change_during_feature_extraction_and_audit_failure_roll_back(self):
        from tracking3.appearance_review import _features
        def changed(*args):
            result=_features(*args)
            self.store.put('task',dict(id='t',version=2,status='active',encoder=self.encoder.version))
            return result
        with patch('tracking3.appearance_review._features',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'任务已变化'):generate(self.store,self.encoder,self.data)
        self.assertEqual(self.store.list('appearance_review'),[])
        self.store.put('task',dict(id='t',version=1,status='active',encoder=self.encoder.version))
        with patch.object(self.store,'audit',side_effect=RuntimeError('fail audit')):
            with self.assertRaises(RuntimeError):self.proposal()
        self.assertEqual(self.store.list('appearance_review'),[])
        r=self.proposal()
        with patch.object(self.store,'audit',side_effect=RuntimeError('fail audit')):
            with self.assertRaises(RuntimeError):decide(self.store,self.choice(r))
        self.assertEqual(self.store.get('appearance_review',r['id'])['revision'],0)

    def test_invalid_params_encoder_and_bad_reviews_do_not_write(self):
        for threshold in (None,True,'0.6',0,1,float('nan')):
            with self.assertRaises(ValueError):generate(self.store,self.encoder,dict(self.data,threshold=threshold))
        self.encoder.version='other'
        with self.assertRaisesRegex(ValueError,'原编码器'):generate(self.store,self.encoder,self.data)
        self.encoder.version=self.observation['encoder'];r=self.proposal()
        for changes in (dict(segment=True),dict(segment=999),dict(reason=''),dict(status='confirmed'),
                dict(expected_review_revision=True),dict(expected_task_version=True)):
            d=self.choice(r);d.update(changes)
            with self.assertRaises(ValueError):decide(self.store,d)
        self.assertEqual(self.store.get('appearance_review',r['id'])['revision'],0)


if __name__=='__main__':unittest.main()
