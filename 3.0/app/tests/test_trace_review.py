import base64
import io
import tempfile
import unittest
from unittest.mock import patch

import av
import numpy as np
from PIL import Image

from tracking3.engine import Encoder, Indexer
from tracking3.store import Store, fingerprint
from tracking3.trace_review import inspect, preview
from tools.generate_demo_dataset import write_video


class TraceReview(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.path=self.store.root/'uploads/a.mp4';write_video(self.path,0,1)
        self.media=dict(id='m',camera_id='a',path='uploads/a.mp4',fingerprint=fingerprint(self.path),
            status='unprocessed',processed_until=-1)
        self.store.put('media',self.media)
        self.store.put('task',dict(id='t',version=1,status='active'))
        self.encoder=Encoder();calls=0
        def detector(rgb):
            nonlocal calls
            calls+=1
            return [([10+calls,10,40+calls,80],.95 if calls==1 else .7,1),
                    ([100,10,140,80],.8,2)]
        Indexer(self.store,self.encoder,'missing.pt').process('m',5,detector=detector)
        self.observation=self.store.list('observation')[0]

    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()

    def rows(self):return inspect(self.store,'t',self.observation['id'])

    def documents(self):
        return [tuple(row) for row in self.store.db.execute('SELECT * FROM documents ORDER BY kind,id')]

    def test_exact_frame_pixels_current_box_and_readonly_inspection(self):
        before=self.documents();rows=self.rows()
        self.assertEqual(rows['sample_count'],5)
        self.assertFalse(rows['trajectory_identity_certified'])
        sample=rows['samples'][2]
        self.assertNotEqual(sample['detection']['box'],self.observation['box'])
        image=preview(self.store,'t',self.observation['id'],sample['id'])
        payload=base64.b64decode(image['image_data_url'].split(',',1)[1])
        import hashlib
        self.assertEqual(hashlib.sha256(payload).hexdigest(),image['image_sha256'])
        with av.open(str(self.path)) as source:
            rgb=next(frame for n,frame in enumerate(source.decode(video=0)) if n==4).to_ndarray(format='rgb24')
        np.testing.assert_array_equal(np.asarray(Image.open(io.BytesIO(payload))),rgb)
        self.assertEqual(image['actual_frame_index'],4);self.assertEqual(image['actual_media_s'],.4)
        self.assertEqual(self.documents(),before)

    def test_foreign_manual_and_legacy_query_owner_are_rejected(self):
        for observation in [dict(self.observation,id='foreign',source='manual',owner_task='other'),
                dict(self.observation,id='legacy',source='manual',crop='crops/query-'+('a'*32)+'.jpg')]:
            self.store.put('observation',observation)
            with self.assertRaisesRegex(ValueError,'不属于'):inspect(self.store,'t',observation['id'])
            with self.assertRaisesRegex(ValueError,'不属于'):preview(self.store,'t',observation['id'],'anything')

    def test_manual_revision_does_not_inherit_old_automatic_samples(self):
        revision=dict(self.observation,id='manual',source='manual',owner_task='t',replaces=self.observation['id'])
        self.store.put('observation',revision)
        self.assertEqual(inspect(self.store,'t','manual')['status'],'manual_without_automatic_trace')
        self.assertEqual(inspect(self.store,'t','manual')['samples'],[])
        self.store.put('task',dict(id='t',version=2,status='paused',superseded_observations=[self.observation['id']]))
        self.assertTrue(self.rows()['superseded'])
        # Read-only history remains usable for a paused task.
        self.assertEqual(self.rows()['sample_count'],5)
        self.store.remove_where('index_frame','media_id','m')
        self.assertEqual(self.rows()['status'],'not_recorded_legacy_or_unindexed')

    def test_other_observation_sample_and_missing_frame_rejected(self):
        row=self.rows()['samples'][0]
        other=self.store.list('observation')[1]
        # A sample frame can contain both people, but the selected person's box is bound to its ID.
        self.assertNotEqual(preview(self.store,'t',other['id'],row['id'])['sample']['detection']['box'],row['detection']['box'])
        with self.assertRaises(ValueError):preview(self.store,'t',self.observation['id'],'missing')
        saved=self.store.get('index_frame',row['id'])
        self.store.put('index_frame',dict(saved,frame_index=999))
        with self.assertRaisesRegex(ValueError,'没有保存'):preview(self.store,'t',self.observation['id'],row['id'])

    def test_forged_timestamp_or_frame_position_never_substitutes_nearby_frame(self):
        row=self.rows()['samples'][1];saved=self.store.get('index_frame',row['id'])
        for changed in (dict(saved,media_s=3),dict(saved,frame_index=3),dict(saved,width=1)):
            self.store.put('index_frame',changed)
            with self.assertRaises(ValueError):preview(self.store,'t',self.observation['id'],row['id'])
        self.store.put('index_frame',saved)
        self.assertEqual(preview(self.store,'t',self.observation['id'],row['id'])['actual_frame_index'],2)

    def test_changed_source_and_change_during_decode_refuse_preview(self):
        sample=self.rows()['samples'][0];original=self.path.read_bytes()
        self.path.write_bytes(original+b'changed')
        with self.assertRaisesRegex(ValueError,'已改变'):self.rows()
        with self.assertRaisesRegex(ValueError,'已改变'):preview(self.store,'t',self.observation['id'],sample['id'])
        self.path.write_bytes(original)
        from tracking3.integrity import require
        calls=0
        def change_after_decode(store,media):
            nonlocal calls
            calls+=1
            if calls==2:self.path.write_bytes(original+b'changed')
            return require(store,media)
        with patch('tracking3.trace_review.require_source',side_effect=change_after_decode):
            with self.assertRaisesRegex(ValueError,'已改变'):preview(self.store,'t',self.observation['id'],sample['id'])

    def test_nonzero_first_pts_uses_original_frame_number_and_relative_time(self):
        from fractions import Fraction
        source=self.store.root/'uploads/offset.mp4'
        with av.open(str(self.path)) as old, av.open(str(source),'w') as out:
            stream=out.add_stream('mpeg4',rate=10)
            stream.width,stream.height,stream.pix_fmt=320,240,'yuv420p'
            for index,decoded in enumerate(old.decode(video=0)):
                frame=av.VideoFrame.from_ndarray(decoded.to_ndarray(format='rgb24'),format='rgb24')
                frame.pts=index+8;frame.time_base=Fraction(1,10)
                for packet in stream.encode(frame):out.mux(packet)
            for packet in stream.encode():out.mux(packet)
        self.store.put('media',dict(self.media,id='offset',path='uploads/offset.mp4',fingerprint=fingerprint(source)))
        Indexer(self.store,self.encoder,'missing.pt').process('offset',5,detector=lambda rgb:[([10,10,40,80],.9,1)])
        observation=next(o for o in self.store.list('observation') if o['media_id']=='offset')
        row=inspect(self.store,'t',observation['id'])['samples'][1]
        self.assertAlmostEqual(row['origin_pts_s'],.8)
        image=preview(self.store,'t',observation['id'],row['id'])
        self.assertEqual(image['actual_frame_index'],2);self.assertAlmostEqual(image['actual_media_s'],.2)

    def test_external_wal_update_is_not_mixed_into_trace_snapshot(self):
        first=self.rows()['samples'][0]
        original_get=Store.get
        changed=False
        def get_then_write(reader,kind,ident):
            nonlocal changed
            result=original_get(reader,kind,ident)
            if reader is not self.store and kind=='task' and not changed:
                changed=True
                self.store.put('task',dict(id='t',version=2,status='active'))
                row=self.store.get('index_frame',first['id'])
                row['detections'][0]['box']=[40.,10.,60.,80.]
                self.store.put('index_frame',row)
            return result
        with patch.object(Store,'get',get_then_write):result=self.rows()
        self.assertEqual(result['task_version'],1)
        self.assertEqual(result['samples'][0]['detection']['box'],first['detection']['box'])
        self.assertEqual(self.store.get('task','t')['version'],2)


if __name__=='__main__':unittest.main()
