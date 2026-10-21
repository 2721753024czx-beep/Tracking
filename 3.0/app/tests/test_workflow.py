import json
import hashlib
import re
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

import av
import numpy as np
from PIL import Image

from tracking3.engine import Encoder, Indexer, search
from tracking3.media import absolute_start, read_frame
from tracking3.server import make_handler, report
from tracking3.store import Store, fingerprint


def video(path):
    with av.open(str(path), 'w') as out:
        stream = out.add_stream('mpeg4', rate=10)
        stream.width, stream.height, stream.pix_fmt = 96, 64, 'yuv420p'
        for i in range(30):
            rgb = np.zeros((64, 96, 3), dtype=np.uint8)
            rgb[8:56, 12:36] = [20, 70, 220]
            frame = av.VideoFrame.from_ndarray(rgb, format='rgb24')
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)


class Workflow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.encoder = Encoder()
        self.indexer = Indexer(self.store, self.encoder, 'missing.pt')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.store, self.encoder, self.indexer))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        video(self.store.root / 'uploads/a.mp4')
        Image.new('RGB', (24, 48), (20, 70, 220)).save(self.store.root / 'uploads/target.png')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.store.db.close()
        self.tmp.cleanup()

    def call(self, path, data=None, headers=None):
        body = json.dumps(data).encode() if data is not None else None
        req = Request(self.base + path, data=body, headers=headers or {})
        with urlopen(req) as r:
            return json.load(r)

    def import_media(self, camera='cam_A', path='uploads/a.mp4', start=''):
        return self.call('/api/media', dict(path=path,name='a.mp4',camera_id=camera,
            location='试验区域',start=start,offset=0,uncertainty=0 if start else None))

    def index(self, media):
        self.indexer.process(media['id'], 3, detector=lambda rgb: [([12,8,36,56],.9,1)])

    def test_all_candidates_includes_isolated_detections_beyond_default_limit(self):
        media=self.import_media()
        evidence=dict(detection_kind='single_frame',retention_reason='tracker_filtered')
        self.indexer.process(media['id'],3,detector=lambda rgb:[([12,8,36,56],.8,None,evidence) for _ in range(20)])
        task=self.call('/api/task',dict(entry='photo',path='uploads/target.png'))
        self.assertEqual(len(self.call('/api/search?task='+task['id'])),100)
        full=self.call('/api/search?task='+task['id']+'&limit=all')
        self.assertEqual(len(full),160)
        self.assertTrue(all(o['status']=='pending' and o['detection_kind']=='single_frame' for o in full))
        trace=self.call('/api/observation-trace?task='+task['id']+'&observation='+full[-1]['id'])
        self.assertEqual(trace['sample_count'],1)

    def test_appearance_suggestions_review_and_correction_are_separate(self):
        media=self.import_media();self.index(media)
        task=self.call('/api/task',dict(entry='photo',path='uploads/target.png'))
        observation=self.call('/api/search?task='+task['id'])[0]
        query='?task='+task['id']+'&observation='+observation['id']
        original=self.store.get('observation',observation['id'])
        r=self.call('/api/appearance-proposal',dict(task_id=task['id'],
            observation_id=observation['id'],expected_task_version=task['version'],threshold=.6))
        self.assertEqual(len(r['segments']),1)
        self.assertFalse(r['identity_certified'])
        self.assertTrue(self.call('/api/appearance-reviews'+query)['items'][0]['valid'])
        data=dict(task_id=task['id'],review_id=r['id'],expected_task_version=task['version'],
            expected_review_revision=0,segment=0,status='draft_prepared',reason='只生成待核对草稿')
        changed=self.call('/api/appearance-review',data)
        self.assertEqual(changed['revision'],1)
        self.assertEqual(self.store.get('observation',observation['id']),original)
        self.assertEqual(self.store.decisions(task['id']),[])
        with self.assertRaises(HTTPError):self.call('/api/appearance-review',data)
        self.call('/api/task-status',dict(task_id=task['id'],expected_task_version=task['version'],status='paused'))
        self.assertTrue(self.call('/api/appearance-reviews'+query)['items'][0]['valid'])
        with self.assertRaises(HTTPError):self.call('/api/appearance-proposal',dict(task_id=task['id'],
            observation_id=observation['id'],expected_task_version=task['version']+1,threshold=.6))

    def test_trace_api_returns_bound_actual_frame_and_no_automatic_manual_history(self):
        media=self.import_media();self.index(media)
        task=self.call('/api/task',dict(entry='photo',path='uploads/target.png'))
        observation=self.call('/api/search?task='+task['id'])[0]
        query='?task='+task['id']+'&observation='+observation['id']
        trace=self.call('/api/observation-trace'+query)
        self.assertEqual(trace['sample_count'],8)
        sample=trace['samples'][1]
        frame=self.call('/api/trace-frame'+query+'&sample='+sample['id'])
        self.assertEqual(frame['actual_frame_index'],sample['frame_index'])
        self.assertEqual(frame['sample']['detection']['observation_id'],observation['id'])
        self.assertTrue(frame['image_data_url'].startswith('data:image/png;base64,'))
        self.assertFalse(frame['trajectory_identity_certified'])
        with self.assertRaises(HTTPError):self.call('/api/trace-frame'+query+'&sample=missing')
        monitor=self.call('/api/task',dict(entry='monitor',media_id=media['id'],time=1,box=[12,8,36,56]))
        manual=self.call('/api/observation-trace?task='+monitor['id']+'&observation='+monitor['anchor'])
        self.assertEqual(manual['status'],'manual_without_automatic_trace')
        with self.assertRaises(HTTPError):self.call('/api/observation-trace?task='+task['id']+'&observation='+monitor['anchor'])

    def test_monitor_seed_is_private_including_legacy_query_crops(self):
        media=self.import_media();self.index(media)
        monitor=self.call('/api/task',dict(entry='monitor',media_id=media['id'],time=1,box=[12,8,36,56]))
        seed=self.store.get('observation',monitor['anchor'])
        self.assertEqual(seed['owner_task'],monitor['id'])
        photo=self.call('/api/task',dict(entry='photo',path='uploads/target.png'))
        for legacy in (False,True):
            if legacy:
                seed.pop('owner_task');self.store.put('observation',seed)
            self.assertIn(seed['id'],[row['id'] for row in self.call('/api/search?task='+monitor['id'])])
            self.assertNotIn(seed['id'],[row['id'] for row in self.call('/api/search?task='+photo['id'])])
            before=self.store.get('task',photo['id'])
            audit=list(self.store.db.execute('SELECT * FROM audit'))
            with self.assertRaises(HTTPError) as error:
                self.call('/api/decision',dict(task_id=photo['id'],observation_id=seed['id'],status='confirmed'))
            self.assertEqual(error.exception.code,400)
            with self.assertRaises(HTTPError):
                self.call('/api/task-references',dict(task_id=photo['id'],expected_task_version=before['version'],
                                                     observation_ids=[seed['id']]))
            with self.assertRaises(HTTPError):
                self.call('/api/observation',dict(task_id=photo['id'],expected_task_version=before['version'],
                     observation_id=seed['id'],start=0,end=2,representative=1,box=[12,8,36,56],evidence='跨任务误用'))
            self.assertEqual(self.store.get('task',photo['id']),before)
            self.assertEqual(list(self.store.db.execute('SELECT * FROM audit')),audit)
            package=self.call('/api/report-bundle',dict(task_id=monitor['id']))
            self.assertTrue(self.store.asset(package['path']).is_file())

    def test_relative_query_edit_and_coverage_api(self):
        a=self.import_media(camera='A');b=self.import_media(camera='B')
        for i,m in enumerate([a,b]):
            self.index(m)
            self.call('/api/media-time',dict(media_id=m['id'],reference_group='r',relative_start=i*10,offset=0,uncertainty=0,time_evidence='受控API时间'))
        t=self.call('/api/task',dict(entry='photo',path='uploads/target.png',cameras=['A']))
        self.assertEqual({o['camera_id'] for o in self.call('/api/search?task='+t['id'])},{'A'})
        self.call('/api/query-conditions',dict(task_id=t['id'],expected_task_version=t['version'],
            cameras=['A','B','missing'],time_filter='relative',query_group='r',relative_from=9,relative_to=15))
        self.assertEqual({o['camera_id'] for o in self.call('/api/search?task='+t['id'])},{'B'})
        coverage=self.call('/api/coverage?task='+t['id'])
        self.assertEqual(coverage['missing_cameras'],['missing'])
        content=self.store.asset(report(self.store,t['id'])).read_text()
        self.assertIn('录像可用与采样覆盖',content)

    def test_calibration_frames_reports_integrity_and_no_time_mutation(self):
        a,b=self.import_media(camera='A'),self.import_media(camera='B')
        before=self.store.db.execute('select * from documents order by kind,id').fetchall()
        audit=self.store.db.execute('select * from audit order by id').fetchall()
        preview=self.call('/api/calibration-frame',dict(media_id=a['id'],fingerprint=a['fingerprint'],second=.21))
        self.assertAlmostEqual(preview['actual_media_s'],.3)
        self.assertEqual(preview['frame_index'],3)
        self.assertEqual(fingerprint(self.store.asset(preview['path'])),preview['image_sha256'])
        data=dict(reference_media_id=a['id'],target_media_id=b['id'],reference_fingerprint=a['fingerprint'],target_fingerprint=b['fingerprint'],
            points=[dict(event_id='E'+str(i),reference_second=t+.2,target_second=t,reference_tolerance=.01,target_tolerance=.01,
                         evidence='受控定位协议，不是实际身份或校时',human_matching_confirmed=True) for i,t in enumerate((0,1,2))])
        data['points'][0]['human_matching_confirmed']=False
        with self.assertRaises(HTTPError) as error:self.call('/api/calibration-report',data)
        self.assertEqual(error.exception.code,400)
        self.assertEqual(self.call('/api/calibration-reports'),[])
        data['points'][0]['human_matching_confirmed']=True
        result=self.call('/api/calibration-report',data)
        self.assertEqual(result['status'],'consistent_at_observed_points')
        self.assertAlmostEqual(result['candidate_offset_to_reference_s'],.2)
        self.assertEqual(self.call('/api/calibration-reports')[0]['id'],result['id'])
        loaded=self.call('/api/calibration-report?id='+result['id'])
        self.assertEqual(loaded['sources']['reference']['fingerprint'],a['fingerprint'])
        self.assertIsNone(loaded['uncertainty_to_register'])
        package=self.call('/api/calibration-bundle',dict(id=result['id']))
        self.assertEqual(fingerprint(self.store.asset(package['path'])),package['sha256'])
        with zipfile.ZipFile(self.store.asset(package['path'])) as archive:
            manifest=json.loads(archive.read('bundle-manifest.json'))
            self.assertFalse(manifest['includes_source_video'])
            for name,digest in manifest['members_sha256'].items():
                self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(),digest)
            refs=re.findall(r'!\[[^\]]*\]\(<([^>]+)>\)',archive.read('report.md').decode())
            self.assertEqual(len(refs),6)
            self.assertTrue(all(ref in archive.namelist() for ref in refs))
        self.assertEqual(self.store.db.execute('select * from documents order by kind,id').fetchall(),before)
        self.assertEqual(self.store.db.execute('select * from audit order by id').fetchall(),audit)
        frame=self.store.asset(result['frame_base']+'/'+result['decoded_frames'][0]['reference']['image'])
        frame.write_bytes(b'changed image')
        with self.assertRaises(HTTPError) as error:self.call('/api/calibration-report?id='+result['id'])
        self.assertEqual(error.exception.code,400)
        with self.store.asset(a['path']).open('ab') as handle:handle.write(b'changed source')
        with self.assertRaises(HTTPError) as error:self.call('/api/calibration-frame',dict(media_id=a['id'],fingerprint=a['fingerprint'],second=1))
        self.assertEqual(error.exception.code,400)
        with self.assertRaises(HTTPError):self.call('/api/calibration-report?id=..')

    def test_venue_direction_and_references_combined_api_workflow(self):
        a, b, c = [self.import_media(camera=name) for name in 'ABC']
        for i, media in enumerate([a, b, c]):
            self.index(media)
            self.call('/api/media-time', dict(media_id=media['id'], reference_group='controlled',
                relative_start=i*10, offset=0, uncertainty=0, time_evidence='受控测试时基'))
        task = self.call('/api/task', dict(entry='photo', path='uploads/target.png'))
        obs = {o['camera_id']: o for o in self.call('/api/search?task='+task['id'])}
        for camera in 'BC':
            self.call('/api/decision', dict(task_id=task['id'], observation_id=obs[camera]['id'], status='confirmed'))
        old = self.store.get('task', task['id'])
        venue = self.call('/api/venue', dict(expected_version=0, name='受控宿舍模拟',
            cameras=[dict(id='B', label='走廊', floor='3F', area='门口', orientation='朝东')]))
        self.assertEqual(venue['version'], 1)
        current = self.store.get('task', task['id'])
        self.assertEqual(current['version'], old['version']+1)
        self.assertEqual(len(self.call('/api/timeline?task='+task['id'])), 2)
        bm = next(m for m in self.call('/api/state')['media'] if m['camera_id']=='B')
        self.assertEqual(bm['venue_camera']['floor'], '3F')
        with self.assertRaises(HTTPError):
            self.call('/api/venue', dict(expected_version=0, name='过期修改', cameras=[]))
        for direction, expected in [('before', {'A','B'}), ('after', {'B','C'}), ('both', {'A','B','C'})]:
            current = self.store.get('task', task['id'])
            self.call('/api/search-scope', dict(task_id=task['id'], expected_task_version=current['version'],
                mode='all', anchor_id=obs['B']['id'], direction=direction))
            self.assertEqual({o['camera_id'] for o in self.call('/api/search?task='+task['id'])}, expected)
        current = self.store.get('task', task['id'])
        self.call('/api/task-references', dict(task_id=task['id'], expected_task_version=current['version'],
            observation_ids=[obs['B']['id'], obs['C']['id']]))
        refs = self.call('/api/task-references?task='+task['id'])
        self.assertEqual(len(refs['observation_ids']), 2)
        self.assertFalse(refs['review_required'])
        # Revoke C, not the B search anchor: failure must be caused by the reference guard.
        self.call('/api/decision', dict(task_id=task['id'], observation_id=obs['C']['id'], status='pending'))
        self.assertTrue(self.call('/api/task-references?task='+task['id'])['review_required'])
        with self.assertRaises(HTTPError):
            self.call('/api/search?task='+task['id'])
        current = self.store.get('task', task['id'])
        self.call('/api/task-references', dict(task_id=task['id'], expected_task_version=current['version'],
            observation_ids=[obs['B']['id']]))
        self.assertEqual(len(self.call('/api/search?task='+task['id'])), 3)
        content = self.store.asset(report(self.store, task['id'])).read_text()
        self.assertIn('受控宿舍模拟', content)
        self.assertIn('3F', content)

    def test_incompatible_reference_api_is_atomic_and_search_still_works(self):
        media = self.import_media()
        self.index(media)
        task = self.call('/api/task', dict(entry='photo', path='uploads/target.png'))
        observation = self.call('/api/search?task='+task['id'])[0]
        self.call('/api/decision', dict(task_id=task['id'], observation_id=observation['id'], status='confirmed'))
        raw = self.store.get('observation', observation['id'])
        raw['encoder'] = 'incompatible-model'
        self.store.put('observation', raw)
        before = self.store.get('task', task['id'])
        with self.assertRaises(HTTPError) as error:
            self.call('/api/task-references', dict(task_id=task['id'], expected_task_version=before['version'],
                observation_ids=[observation['id']]))
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.store.get('task', task['id']), before)
        # Existing search excludes incompatible observations rather than crashing.
        self.assertEqual(self.call('/api/search?task='+task['id']), [])

    def test_manual_observation_api_uses_real_decoded_frame(self):
        media=self.import_media(); self.index(media)
        t=self.call('/api/task',dict(entry='photo',path='uploads/target.png'))
        old=self.call('/api/search?task='+t['id'])[0]
        d=dict(task_id=t['id'],expected_task_version=t['version'],observation_id=old['id'],
               start=0,end=2,representative=1,box=[12,8,36,56],evidence='受控原录像帧核对')
        result=self.call('/api/observation',d)
        o=result['observation']
        self.assertEqual(o['source'],'manual')
        self.assertEqual(o['representative'],1)
        self.assertTrue(self.store.asset(o['crop']).is_file())
        ids=[r['id'] for r in self.call('/api/search?task='+t['id'])]
        self.assertIn(o['id'],ids); self.assertNotIn(old['id'],ids)
        with self.assertRaises(HTTPError): self.call('/api/observation',d)
        self.call('/api/decision',dict(task_id=t['id'],observation_id=o['id'],status='confirmed'))
        self.assertIn('受控原录像帧核对',self.store.asset(report(self.store,t['id'])).read_text())

    def test_report_preserves_query_candidate_snapshot_and_source_hash(self):
        media=self.import_media();self.index(media)
        t=self.call('/api/task',dict(entry='photo',path='uploads/target.png',cameras=['cam_A']))
        o=self.call('/api/search?task='+t['id'])[0]
        old_path=report(self.store,t['id']);old=self.store.asset(old_path).read_text()
        self.assertIn('导出时的查询条件',old)
        self.assertIn(media['fingerprint'],old)
        self.assertIn('状态 pending',old)
        self.assertIn('[源录像](../'+media['path']+')',old)
        self.assertIn('[截图](../'+o['crop']+')',old)
        self.store.decide(t['id'],o['id'],'confirmed')
        new_path=report(self.store,t['id'])
        self.assertNotEqual(old_path,new_path)
        self.assertEqual(self.store.asset(old_path).read_text(),old)
        self.assertIn('状态 confirmed',self.store.asset(new_path).read_text())

    def test_report_bundle_api_exports_bound_snapshot_and_pending_crops(self):
        media = self.import_media()
        self.index(media)
        task = self.call('/api/task', dict(entry='photo',path='uploads/target.png'))
        candidates = self.call('/api/search?task='+task['id'])
        result = self.call('/api/report-bundle', dict(task_id=task['id'],include_media=True))
        original = self.store.asset(result['report_path']).read_bytes()
        with zipfile.ZipFile(self.store.asset(result['path'])) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            snapshot = json.loads(archive.read('report-snapshot.json'))
            self.assertEqual(manifest['task_version'], task['version'])
            self.assertEqual(snapshot['task']['version'], task['version'])
            self.assertEqual(manifest['metadata_basis'], 'report_snapshot')
            self.assertEqual(archive.read('report-source.md'), original)
            self.assertTrue(manifest['media'][0]['included_in_bundle'])
            for candidate in candidates:
                self.assertIn('evidence/'+candidate['crop'], archive.namelist())

    def test_changed_missing_and_restored_source_do_not_reuse_wrong_video(self):
        media=self.import_media(start='2026-10-09T15:00:00+08:00');self.index(media)
        t=self.call('/api/task',dict(entry='photo',path='uploads/target.png'))
        o=self.call('/api/search?task='+t['id'])[0]
        self.call('/api/decision',dict(task_id=t['id'],observation_id=o['id'],status='confirmed'))
        self.assertEqual(self.call('/api/last?task='+t['id'])['status'],'unique')

        source=self.store.asset(media['path']);original=source.read_bytes()
        source.write_bytes(bytes([original[0]^1])+original[1:])
        self.assertEqual(self.call('/api/state')['media'][0]['source_integrity']['status'],'changed')
        self.assertFalse(self.call('/api/search?task='+t['id'])[0]['source_integrity']['valid'])
        self.assertEqual(self.call('/api/last?task='+t['id'])['status'],'evidence_unavailable')
        self.assertEqual(self.store.decisions(t['id'])[0]['status'],'confirmed')
        for route in ['/api/frame?media='+media['id']+'&time=1','/asset?path='+media['path']]:
            with self.assertRaises(HTTPError):self.call(route)
        with self.assertRaises(HTTPError):
            self.call('/api/decision',dict(task_id=t['id'],observation_id=o['id'],status='confirmed'))
        content=self.store.asset(report(self.store,t['id'])).read_text()
        self.assertIn('源录像失效或无法核实',content)
        self.assertIn('源核验 changed',content)
        renamed=source.with_suffix('.saved');source.rename(renamed)
        self.assertEqual(self.call('/api/state')['media'][0]['source_integrity']['status'],'missing')
        source.write_bytes(original)
        self.assertTrue(self.call('/api/timeline?task='+t['id'])[0]['source_integrity']['valid'])
        self.assertEqual(self.call('/api/last?task='+t['id'])['status'],'unique')

    def test_task_pause_resume_preserves_evidence_and_blocks_local_edits(self):
        media=self.import_media();self.index(media)
        t=self.call('/api/task',dict(entry='photo',path='uploads/target.png'))
        o=self.call('/api/search?task='+t['id'])[0]
        self.call('/api/decision',dict(task_id=t['id'],observation_id=o['id'],status='confirmed'))
        version=self.store.get('task',t['id'])['version']
        paused=self.call('/api/task-status',dict(task_id=t['id'],status='paused',expected_task_version=version))
        self.assertEqual(self.call('/api/timeline?task='+t['id'])[0]['status'],'confirmed')
        self.assertTrue(self.store.asset(self.call('/api/report',dict(task_id=t['id']))['path']).is_file())
        for route,d in [('/api/decision',dict(observation_id=o['id'],status='pending')),
                        ('/api/query-conditions',dict(cameras=[])),('/api/search-scope',dict(mode='all')),
                        ('/api/observation',dict()),('/api/connection',dict())]:
            with self.assertRaises(HTTPError):self.call(route,dict(task_id=t['id'],expected_task_version=paused['version'],**d))
        with self.assertRaises(HTTPError):self.call('/api/task-status',dict(task_id=t['id'],status='active',expected_task_version=version))
        resumed=self.call('/api/task-status',dict(task_id=t['id'],status='active',expected_task_version=paused['version']))
        self.call('/api/decision',dict(task_id=t['id'],observation_id=o['id'],status='pending',expected_task_version=resumed['version']))
        self.assertEqual(self.store.get('media',media['id'])['status'],'complete')
    def test_topology_scope_api_and_expansion(self):
        a=self.import_media(camera='A');b=self.import_media(camera='B');c=self.import_media(camera='C')
        for m in [a,b,c]: self.index(m)
        task=self.call('/api/task',dict(entry='photo',path='uploads/target.png'))
        observations=self.call('/api/search?task='+task['id'])
        anchor=next(o for o in observations if o['camera_id']=='A')
        self.call('/api/decision',dict(task_id=task['id'],observation_id=anchor['id'],status='confirmed'))
        self.assertEqual(self.call('/api/topology')['version'],0)
        self.call('/api/topology',dict(expected_version=0,edges=[dict(**{'from':'A','to':'B'},evidence='受控API测试')]))
        current=self.store.get('task',task['id'])
        t=self.call('/api/search-scope',dict(task_id=task['id'],expected_task_version=current['version'],mode='neighbors',anchor_id=anchor['id']))
        self.assertEqual({o['camera_id'] for o in self.call('/api/search?task='+task['id'])},{'A','B'})
        self.assertEqual(self.call('/api/search-context?task='+task['id'])['remaining_cameras'],['C'])
        self.call('/api/search-scope',dict(task_id=task['id'],expected_task_version=t['version'],mode='all',anchor_id=anchor['id']))
        self.assertEqual({o['camera_id'] for o in self.call('/api/search?task='+task['id'])},{'A','B','C'})
        self.assertEqual(self.store.decisions(task['id'])[0]['status'],'confirmed')

    def test_summary_separates_latest_time_ranking_history_and_source_coverage(self):
        media=[self.import_media(camera=c,start='2026-10-09T15:00:'+sec+'+08:00')
               for c,sec in [('A','00'),('B','10'),('C','20')]]
        for m in media:self.index(m)
        t=self.call('/api/task',dict(entry='photo',path='uploads/target.png'))
        obs=self.call('/api/search?task='+t['id'])
        b=next(o for o in obs if o['camera_id']=='B');c=next(o for o in obs if o['camera_id']=='C')
        raw=self.store.get('observation',b['id']);raw['feature']=[-v for v in self.store.get('task',t['id'])['feature']];self.store.put('observation',raw)
        self.call('/api/decision',dict(task_id=t['id'],observation_id=c['id'],status='confirmed'))
        s=self.call('/api/summary?task='+t['id'])
        self.assertEqual(s['latest_pending']['event']['camera_id'],'B')
        self.assertEqual(s['last_confirmed']['event']['camera_id'],'C')
        self.assertEqual(s['counts']['pending_current'],2)
        bm=self.store.get('media',media[1]['id']);bm['uncertainty']=None;self.store.put('media',bm)
        self.assertEqual(self.call('/api/summary?task='+t['id'])['latest_pending']['status'],'unknown_error')
        current=self.store.get('task',t['id'])
        self.call('/api/query-conditions',dict(task_id=t['id'],expected_task_version=current['version'],cameras=['A','missing']))
        s=self.call('/api/summary?task='+t['id'])
        self.assertEqual(s['latest_pending']['event']['camera_id'],'A')
        self.assertEqual(s['last_confirmed']['event']['camera_id'],'C')
        self.assertEqual(s['coverage']['missing_cameras'],['missing'])
        source=self.store.asset(media[0]['path']);source.write_bytes(b'changed')
        s=self.call('/api/summary?task='+t['id'])
        self.assertEqual(s['last_confirmed']['status'],'evidence_unavailable')
        self.assertEqual(s['latest_pending']['status'],'evidence_unavailable')
        group=s['coverage']['groups'][0]
        self.assertTrue(group['available']);self.assertEqual(group['verified_source_intervals'],[])

    def test_connection_api_is_separate_from_identity_and_exports_revocation(self):
        a = self.import_media(start='2026-10-09T15:00:00+08:00')
        b = self.import_media(camera='cam_B', start='2026-10-09T15:00:04+08:00')
        self.index(a)
        self.index(b)
        task = self.call('/api/task', dict(entry='photo',path='uploads/target.png'))
        observations = self.call('/api/search?task='+task['id'])
        observations.sort(key=lambda o:o['camera_id'])
        for o in observations:
            self.call('/api/decision',dict(task_id=task['id'],observation_id=o['id'],status='confirmed'))
        self.assertEqual(self.call('/api/connections?task='+task['id']),[])
        saved = self.call('/api/connection',dict(task_id=task['id'],
            from_observation=observations[0]['id'],to_observation=observations[1]['id'],
            relation='transition',status='confirmed',evidence='受控素材接口测试'))
        c = saved['connection']
        preview = self.call('/api/impact?task='+task['id']+'&observation='+observations[0]['id'])
        self.assertEqual([item['id'] for item in preview['affected']], [c['id']])
        self.assertEqual(preview['task_version'], saved['task_version'])
        self.assertEqual(c['assessment']['media_time_versions'],{a['id']:1,b['id']:1})
        self.call('/api/connection',dict(task_id=task['id'],id=c['id'],expected_version=c['version'],status='revoked'))
        self.assertEqual(self.call('/api/connections?task='+task['id'])[0]['status'],'revoked')
        self.assertTrue(all(o['status']=='confirmed' for o in self.call('/api/timeline?task='+task['id'])))
        exported = self.call('/api/report',dict(task_id=task['id']))
        self.assertIn('revoked',self.store.asset(exported['path']).read_text())
        reopened = Store(self.tmp.name)
        self.assertEqual(reopened.list('connection')[0]['status'],'revoked')
        reopened.db.close()

    def test_two_entries_review_undo_export_and_restart(self):
        a = self.import_media(start='2026-10-09T15:00:00+08:00')
        b = self.import_media(camera='cam_B',start='2026-10-09T15:00:04+08:00')
        self.index(a)
        self.index(b)
        photo = self.call('/api/task', dict(entry='photo',name='照片任务',path='uploads/target.png'))
        candidates = self.call('/api/search?task='+photo['id'])
        self.assertEqual({o['camera_id'] for o in candidates}, {'cam_A','cam_B'})
        self.assertTrue(all(o['status']=='pending' for o in candidates))
        candidate = candidates[0]
        self.call('/api/decision',dict(task_id=photo['id'],observation_id=candidate['id'],status='confirmed'))
        self.assertEqual(self.call('/api/timeline?task='+photo['id'])[0]['status'],'confirmed')
        exported = self.call('/api/report',dict(task_id=photo['id']))
        self.assertIn('这不是当前实时位置',self.store.asset(exported['path']).read_text())
        self.call('/api/decision',dict(task_id=photo['id'],observation_id=candidate['id'],status='pending'))
        self.assertEqual(self.call('/api/timeline?task='+photo['id'])[0]['status'],'pending')
        monitor = self.call('/api/task',dict(entry='monitor',media_id=a['id'],time=.4,
                                           box=[12,8,36,56],name='框选任务'))
        self.assertEqual(self.call('/api/timeline?task='+monitor['id'])[0]['source'],'manual')
        reopened = Store(self.tmp.name)
        self.assertEqual(len(reopened.list('task')),2)
        self.assertEqual(reopened.decisions(photo['id'])[0]['status'],'pending')
        reopened.db.close()

    def test_unknown_time_not_silently_excluded_and_no_unique_last(self):
        m=self.import_media()
        self.index(m)
        t=self.call('/api/task',dict(entry='photo',path='uploads/target.png',
                                    **{'from':'2030-01-01T00:00:00+08:00'}))
        results=search(self.store,self.encoder,t['id'])
        self.assertEqual(len(results),1)
        self.store.decide(t['id'],results[0]['id'],'confirmed')
        content=self.store.asset(report(self.store,t['id'])).read_text()
        self.assertIn('不能确定唯一最后位置',content)

    def test_pause_resume_keeps_confirmed_evidence(self):
        m=self.import_media()
        calls=[0]
        def det(rgb):
            calls[0]+=1
            if calls[0]==2:
                self.indexer.stop.set()
            return [([12,8,36,56],.9,1)]
        self.indexer.process(m['id'],3,detector=det)
        self.assertEqual(self.store.get('media',m['id'])['status'],'paused')
        old=self.store.list('observation')[0]
        t=self.call('/api/task',dict(entry='photo',path='uploads/target.png'))
        self.store.decide(t['id'],old['id'],'confirmed')
        self.indexer.stop.clear()
        self.index(m)
        self.assertEqual(self.store.get('media',m['id'])['status'],'complete')
        self.assertEqual(self.store.get('observation',old['id']),old)
        self.assertEqual(self.store.decisions(t['id'])[0]['status'],'confirmed')
        self.assertEqual(len(self.store.list('observation')),2)

    def test_validation_duplicate_paths_origin_time_and_ended_task(self):
        m=self.import_media()
        with self.assertRaises(HTTPError):self.import_media()
        with self.assertRaises(ValueError):self.store.asset('../outside')
        with self.assertRaises(ValueError):absolute_start('2026-10-09T15:00:00')
        with self.assertRaises(HTTPError):self.call('/api/state',headers={'Origin':'https://evil.example'})
        with self.assertRaises(HTTPError):self.call('/api/task',dict(entry='monitor',media_id=m['id'],time=0,box=[5,5,5,6]))
        self.index(m)
        t=self.call('/api/task',dict(entry='photo',path='uploads/target.png'))
        self.call('/api/task-status',dict(task_id=t['id'],status='ended',expected_task_version=self.store.get('task',t['id'])['version']))
        with self.assertRaises(HTTPError):self.call('/api/decision',dict(task_id=t['id'],observation_id=self.store.list('observation')[0]['id'],status='confirmed'))

    def test_video_byte_range_and_actual_frame_time(self):
        rgb,at=read_frame(self.store.root/'uploads/a.mp4',.45)
        self.assertGreaterEqual(at,.45)
        self.assertLess(at,.56)
        req=Request(self.base+'/asset?path=uploads/a.mp4',headers={'Range':'bytes=0-31'})
        with urlopen(req) as r:
            self.assertEqual(r.status,206)
            self.assertEqual(len(r.read()),32)

    def test_relative_time_revision_preserves_evidence_and_requires_review(self):
        a = self.import_media()
        b = self.import_media(camera='cam_B')
        self.index(a)
        self.index(b)
        task = self.call('/api/task',dict(entry='photo',path='uploads/target.png'))
        observations = self.store.list('observation')
        for o in observations:
            self.store.decide(task['id'],o['id'],'confirmed')
        config = dict(start='',offset=0,uncertainty=1,reference_group='trial',
                      relative_start=0,time_evidence='同一闪光事件人工对齐')
        self.call('/api/media-time', dict(config,media_id=a['id'],expected_time_version=1))
        self.call('/api/media-time', dict(config,media_id=b['id'],relative_start=10))
        self.assertEqual(self.store.list('observation'), observations)
        self.assertTrue(all(d['status']=='confirmed' for d in self.store.decisions(task['id'])))
        revised = self.store.get('task',task['id'])
        self.assertTrue(revised['time_review_required'])
        self.assertEqual(self.call('/api/last?task='+task['id'])['status'],'needs_review')
        timeline = self.call('/api/timeline?task='+task['id'])
        self.assertTrue(all(o['time_domain']==['relative','trial'] and o['absolute_start'] is None for o in timeline))
        content = self.store.asset(report(self.store,task['id'])).read_text()
        self.assertIn('待重新核对', content)
        with self.assertRaises(HTTPError):
            self.call('/api/time-reviewed',dict(task_id=task['id'],versions={a['id']:1,b['id']:1}))
        versions = {m['id']:m.get('time_version',1) for m in self.store.list('media')}
        self.call('/api/time-reviewed',dict(task_id=task['id'],versions=versions))
        self.assertEqual(self.call('/api/last?task='+task['id'])['event']['camera_id'],'cam_B')
        content = self.store.asset(report(self.store,task['id'])).read_text()
        self.assertIn('时间组 trial',content)
        self.assertIn('cam_B / 试验区域',content.split('## 最后可信线索')[1].split('## 待确认')[0])
        with self.assertRaises(HTTPError):
            self.call('/api/media-time',dict(config,media_id=b['id'],expected_time_version=1))
        self.indexer.lock.acquire()
        try:
            with self.assertRaises(HTTPError):
                self.call('/api/media-time',dict(config,media_id=b['id']))
        finally:
            self.indexer.lock.release()


if __name__=='__main__':unittest.main()
