import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tracking3.engine import Encoder, Indexer, search
from tracking3.evaluation_export import export
from tracking3.index_trace import snapshot
from tracking3.store import Store, fingerprint
from tools.generate_demo_dataset import write_video


class IndexTrace(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name)
        path=self.store.root/'uploads/a.mp4'
        write_video(path,0,1.0)
        self.media=dict(id='m',camera_id='a',path='uploads/a.mp4',name='a.mp4',
            location='fixture',fingerprint=fingerprint(path),status='unprocessed',processed_until=-1)
        self.store.put('media',self.media)
        self.encoder=Encoder()
        self.indexer=Indexer(self.store,self.encoder,'missing.pt')

    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()

    def test_current_boxes_and_empty_model_samples_preserved_without_changing_representative(self):
        calls=0
        def detector(rgb):
            nonlocal calls
            calls+=1
            if calls==3:return []
            return [([10+calls,10,40+calls,80],.95 if calls==1 else .7,1)]
        self.indexer.process('m',5,detector=detector)
        frames=snapshot(self.store,{'m'})['frames']
        self.assertEqual([r['frame_index'] for r in frames],[0,2,4,6,8])
        self.assertEqual([r['media_s'] for r in frames],[0,.2,.4,.6,.8])
        self.assertEqual(frames[2]['detections'],[])
        self.assertEqual(frames[1]['detections'][0]['box'],[12.,10.,42.,80.])
        observation=self.store.list('observation')[0]
        self.assertEqual(observation['box'],[11,10,41,80])
        self.assertEqual(observation['representative'],0)
        self.assertEqual(observation['end'],.8)
        self.assertTrue(all(r['source_sha256']==self.media['fingerprint'] for r in frames))

    def test_pause_resume_preserves_source_frame_numbers_and_separates_runs(self):
        calls=0
        def pause(rgb):
            nonlocal calls
            calls+=1
            if calls==2:self.indexer.stop.set()
            return [([10,10,40,80],.9,1)]
        self.indexer.process('m',5,detector=pause)
        before=self.store.list('index_frame')
        self.assertEqual([r['frame_index'] for r in before],[0,2])
        self.assertEqual(self.store.get('media','m')['status'],'paused')
        self.indexer.stop.clear()
        self.indexer.process('m',5,detector=lambda rgb:[([20,10,50,80],.8,1)])
        frames=snapshot(self.store,{'m'})['frames']
        self.assertEqual(frames[:2],before)
        self.assertTrue(all(r['frame_index']>2 and r['run_no']==2 for r in frames[2:]))
        first=next(o for o in self.store.list('observation') if o['start']==0)
        second=next(o for o in self.store.list('observation') if o['start']>0)
        self.assertNotEqual(first['id'],second['id'])

    def test_batch_failure_does_not_commit_trajectory_without_sample_or_progress(self):
        original=self.store.batch;calls=0
        def fail_second(entries):
            nonlocal calls
            calls+=1
            self.assertEqual({kind for kind,item in entries},{'observation','index_frame','media'})
            if calls==2:raise RuntimeError('controlled commit failure')
            original(entries)
        with patch.object(self.store,'batch',side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError,'commit failure'):
                self.indexer.process('m',5,detector=lambda rgb:[([10,10,40,80],.9,1)])
        self.assertEqual(len(self.store.list('index_frame')),1)
        self.assertEqual(self.store.list('observation')[0]['end'],0)
        self.assertEqual(self.store.get('media','m')['processed_until'],0)

    def make_task(self):
        (self.store.root/'crops/query.jpg').write_bytes(b'query')
        self.store.put('task',dict(id='t',version=1,entry='photo',crop='crops/query.jpg',
            encoder=self.encoder.version,feature=[1.]*512,status='active',cameras=['a']))

    def test_zero_detection_frames_are_in_same_readonly_export_even_with_zero_candidates(self):
        self.indexer.process('m',5,detector=lambda rgb:[])
        self.make_task()
        before=[tuple(r) for r in self.store.db.execute('SELECT * FROM documents ORDER BY kind,id')]
        output=Path(self.tmp.name)/'export'
        result=export(self.store.root,'t',output)
        self.assertEqual(result['candidates'],0)
        samples=json.loads((output/'tracking-samples.json').read_text())
        self.assertEqual(len(samples['frames']),5)
        self.assertFalse(samples['empty_detections_are_ground_truth_absence'])
        artifact=json.loads((output/'artifact.json').read_text())
        self.assertIn('uploads/a.mp4',artifact['source_sha256'])
        self.assertEqual(fingerprint(output/'tracking-samples.json'),artifact['files']['tracking-samples.json'])
        self.assertEqual([tuple(r) for r in self.store.db.execute('SELECT * FROM documents ORDER BY kind,id')],before)

    def test_legacy_source_is_explicitly_missing_not_filled_from_observation(self):
        self.make_task();self.media['status']='complete';self.store.put('media',self.media)
        result=snapshot(self.store,{'m'})
        self.assertEqual(result['frames'],[])
        self.assertEqual(result['sources'][0]['status'],'not_recorded_legacy_or_unindexed')
        self.assertFalse(result['unsampled_frames_inferred'])

    def test_wal_update_does_not_mix_new_samples_into_old_task_snapshot(self):
        self.indexer.process('m',5,detector=lambda rgb:[([10,10,40,80],.9,1)])
        self.make_task()
        original=self.store.list('index_frame')[0]
        def external_write(store,encoder,ident,limit):
            ranked=search(store,encoder,ident,limit)
            changed=json.loads(json.dumps(original))
            changed['detections'][0]['confidence']=.1
            self.store.put('index_frame',changed)
            return ranked
        output=Path(self.tmp.name)/'wal-export'
        with patch('tracking3.evaluation_export.search',side_effect=external_write):
            export(self.store.root,'t',output)
        frozen=json.loads((output/'tracking-samples.json').read_text())
        self.assertEqual(frozen['frames'][0]['detections'][0]['confidence'],.9)
        self.assertEqual(self.store.get('index_frame',original['id'])['detections'][0]['confidence'],.1)

    def test_trace_source_or_time_mismatch_refuses_export_before_output(self):
        self.indexer.process('m',5,detector=lambda rgb:[]);self.make_task()
        original=self.store.list('index_frame')[0]
        for key,value in [('source_sha256','wrong'),('media_s',10)]:
            with self.subTest(key=key):
                changed={**original,key:value};self.store.put('index_frame',changed)
                output=Path(self.tmp.name)/key
                with self.assertRaises(ValueError):export(self.store.root,'t',output)
                self.assertFalse(output.exists())
                self.store.put('index_frame',original)


if __name__=='__main__':unittest.main()
