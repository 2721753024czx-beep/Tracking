import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tracking3.bundle import create
from tracking3.engine import Encoder, Indexer
from tracking3.server import report
from tracking3.snapshots import sidecar_path, _canonical
from tracking3.store import Store, fingerprint
from tools.generate_demo_dataset import write_video


class ReportTrace(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name)
        path=self.store.root/'uploads/a.mp4'
        write_video(path,0,1.0)
        self.store.put('media',dict(id='m',camera_id='a',location='fixture',
            path='uploads/a.mp4',name='a.mp4',fingerprint=fingerprint(path),
            status='unprocessed',processed_until=-1))
        self.encoder=Encoder()
        self.indexer=Indexer(self.store,self.encoder,'missing.pt')
        (self.store.root/'crops/query.jpg').write_bytes(b'query')
        self.store.put('task',dict(id='t',name='trace',version=1,entry='photo',
            crop='crops/query.jpg',encoder=self.encoder.version,feature=[1.]*512,
            status='active',cameras=['a']))

    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()

    def index(self,empty=False):
        self.indexer.process('m',5,detector=lambda rgb:[] if empty else [([10,10,40,80],.9,1)])

    def bundle(self,path):
        return zipfile.ZipFile(self.store.asset(create(self.store,'t',path)))

    def test_report_freezes_all_actual_points_and_pending_observation_mapping(self):
        self.index();path=report(self.store,'t')
        frozen=json.loads(self.store.asset(sidecar_path(path)).read_text())
        diagnostic=frozen['tracking_diagnostics']
        self.assertEqual(len(diagnostic['samples']['frames']),5)
        self.assertEqual(diagnostic['observations'][0]['sample_ids'],
                         [f['id'] for f in diagnostic['samples']['frames']])
        self.assertEqual(frozen['observations'][0]['id'],self.store.list('observation')[0]['id'])
        self.assertNotIn('feature',frozen['observations'][0])
        with self.bundle(path) as z:
            samples=json.loads(z.read('tracking-samples.json'))
            binding=json.loads(z.read('tracking-diagnostics.json'))
            self.assertEqual(samples,diagnostic['samples'])
            self.assertEqual(binding['samples_sha256'],hashlib.sha256(z.read('tracking-samples.json')).hexdigest())
            self.assertEqual(binding['source_report_sha256'],fingerprint(self.store.asset(path)))
            self.assertFalse(samples['trajectory_identity_certified'])

    def test_zero_detection_frames_are_present_even_without_candidates(self):
        self.index(empty=True);path=report(self.store,'t')
        with self.bundle(path) as z:
            samples=json.loads(z.read('tracking-samples.json'))
            self.assertEqual(len(samples['frames']),5)
            self.assertTrue(all(not f['detections'] for f in samples['frames']))
            self.assertFalse(samples['empty_detections_are_ground_truth_absence'])

    def test_later_db_samples_and_versions_cannot_change_an_existing_report_bundle(self):
        self.index();path=report(self.store,'t')
        row=self.store.list('index_frame')[0];row['detections'][0]['confidence']=.1
        self.store.put('index_frame',row)
        task=self.store.get('task','t');task['version']=2;self.store.put('task',task)
        with self.bundle(path) as z:
            self.assertEqual(json.loads(z.read('tracking-diagnostics.json'))['task_version'],1)
            self.assertEqual(json.loads(z.read('tracking-samples.json'))['frames'][0]['detections'][0]['confidence'],.9)

    def test_external_wal_writer_at_task_read_cannot_mix_new_samples(self):
        self.index();other=Store(self.store.root)
        old_get=self.store.get;done=False
        def get(kind,ident):
            nonlocal done
            value=old_get(kind,ident)
            if kind=='task' and not done:
                done=True;row=other.list('index_frame')[0]
                row['detections'][0]['confidence']=.1;other.put('index_frame',row)
            return value
        try:
            with patch.object(self.store,'get',side_effect=get):path=report(self.store,'t')
            with self.bundle(path) as z:
                self.assertEqual(json.loads(z.read('tracking-samples.json'))['frames'][0]['detections'][0]['confidence'],.9)
            changed=next(f for f in self.store.list('index_frame') if f['frame_index']==0)
            self.assertEqual(changed['detections'][0]['confidence'],.1)
        finally:other.db.close()

    def test_bad_source_or_point_time_declares_unavailable_without_fabricating_empty_trace(self):
        self.index();row=self.store.list('index_frame')[0]
        for key,value in [('source_sha256','wrong'),('media_s',99)]:
            with self.subTest(key=key):
                self.store.put('index_frame',{**row,key:value});path=report(self.store,'t')
                with self.bundle(path) as z:
                    diagnostic=json.loads(z.read('tracking-diagnostics.json'))
                    self.assertEqual(diagnostic['status'],'unavailable_at_report')
                    self.assertIsNone(diagnostic['samples_file'])
                    self.assertNotIn('tracking-samples.json',z.namelist())
                self.store.put('index_frame',row)

    def test_unindexed_media_is_explicit_and_later_index_does_not_fill_frozen_gap(self):
        path=report(self.store,'t');self.index()
        with self.bundle(path) as z:
            trace=json.loads(z.read('tracking-samples.json'))
            self.assertEqual(trace['frames'],[])
            self.assertEqual(trace['sources'][0]['status'],'not_recorded_legacy_or_unindexed')

    def test_old_v1_report_without_trace_remains_readable_and_does_not_use_current_db(self):
        path=report(self.store,'t');sidecar=self.store.asset(sidecar_path(path))
        frozen=json.loads(sidecar.read_text());frozen.pop('tracking_diagnostics');frozen['format']='tracking3-report-snapshot-v1'
        payload={k:v for k,v in frozen.items() if k not in ('metadata_sha256','report_sha256')}
        digest=hashlib.sha256(_canonical(payload)).hexdigest()
        text=self.store.asset(path).read_text().replace(frozen['metadata_sha256'],digest)
        frozen['metadata_sha256']=digest;frozen['report_sha256']=hashlib.sha256(text.encode()).hexdigest()
        sidecar.write_text(json.dumps(frozen));self.store.asset(path).write_text(text)
        self.index()
        with self.bundle(path) as z:
            diagnostic=json.loads(z.read('tracking-diagnostics.json'))
            self.assertEqual(diagnostic['status'],'not_recorded_at_report')
            self.assertNotIn('tracking-samples.json',z.namelist())

    def test_trace_tampering_breaks_the_report_binding(self):
        self.index();path=report(self.store,'t');sidecar=self.store.asset(sidecar_path(path))
        frozen=json.loads(sidecar.read_text());frozen['tracking_diagnostics']['samples']['frames'][0]['media_s']=77
        sidecar.write_text(json.dumps(frozen))
        with self.assertRaisesRegex(ValueError,'已改变'):create(self.store,'t',path)

    def test_manual_revision_does_not_inherit_the_replaced_automatic_trace(self):
        self.index();parent=self.store.list('observation')[0]
        manual={**parent,'id':'manual','source':'manual','owner_task':'t','local_track':None,'revision':1,
            'replaces':parent['id'],'evidence':'fixture correction'}
        self.store.put('observation',manual);path=report(self.store,'t')
        with self.bundle(path) as z:
            row=next(o for o in json.loads(z.read('tracking-diagnostics.json'))['observations'] if o['observation_id']=='manual')
            self.assertEqual(row['status'],'manual_without_automatic_trace');self.assertEqual(row['sample_ids'],[])


if __name__=='__main__':unittest.main()
