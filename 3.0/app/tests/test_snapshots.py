import json
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import patch

from tracking3.bundle import create
from tracking3.server import report
from tracking3.snapshots import save, sidecar_path
from tracking3.store import Store, fingerprint


class ReportSnapshots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        (self.store.root/'crops/query.jpg').write_bytes(b'query')
        self.store.put('task', {'id':'t', 'version':1, 'name':'snapshot', 'entry':'photo',
                               'encoder':'hsv-body-v1:experimental', 'feature':[1.0],
                               'crop':'crops/query.jpg', 'status':'active'})

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_real_report_generator_persists_a_bound_public_snapshot(self):
        path = report(self.store, 't')
        snapshot = json.loads(self.store.asset(sidecar_path(path)).read_text())
        self.assertNotIn('feature', snapshot['task'])
        self.assertIn('tracking3-report-snapshot-sha256', self.store.asset(path).read_text())
        task = self.store.get('task', 't')
        task.update(version=2, name='after-report')
        self.store.put('task', task)
        with zipfile.ZipFile(self.store.asset(create(self.store, 't', path))) as archive:
            self.assertEqual(json.loads(archive.read('task.json'))['version'], 1)
            self.assertEqual(json.loads(archive.read('manifest.json'))['task_version'], 1)

    def test_concurrent_change_after_report_does_not_mix_bundle_versions(self):
        boundary = threading.Event()
        writer_attempted = threading.Event()
        finished = threading.Event()
        failure = []

        def writer():
            try:
                if not boundary.wait(2):
                    raise RuntimeError('report boundary not reached')
                writer_attempted.set()
                task = self.store.get('task', 't')
                task.update(version=2, name='concurrent-change')
                self.store.put('task', task)
            except BaseException as exc:
                failure.append(exc)
            finally:
                finished.set()

        def save_at_boundary(*args):
            boundary.set()
            self.assertTrue(writer_attempted.wait(2))
            # The writer tries Store.get while report owns Store.lock.
            self.assertFalse(finished.is_set())
            return save(*args)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            with patch('tracking3.server.save_report_snapshot', side_effect=save_at_boundary):
                path = report(self.store, 't')
            self.assertTrue(finished.wait(2))
            self.assertEqual(failure, [])
            self.assertEqual(self.store.get('task', 't')['version'], 2)
            with zipfile.ZipFile(self.store.asset(create(self.store, 't', path))) as archive:
                self.assertEqual(json.loads(archive.read('manifest.json'))['task_version'], 1)
                self.assertEqual(json.loads(archive.read('task.json'))['version'], 1)
                self.assertIn('结果版本：1', archive.read('report.md').decode())
        finally:
            boundary.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_separate_db_writer_cannot_mix_media_metadata_into_report(self):
        source = self.store.root/'uploads/video.mp4'
        source.write_bytes(b'video')
        self.store.put('media', {'id':'m', 'path':'uploads/video.mp4', 'name':'video.mp4',
                                'camera_id':'A', 'location':'lab', 'duration':1, 'fps':5,
                                'status':'complete', 'fingerprint':fingerprint(source)})
        writer_store = Store(self.store.root)
        boundary = threading.Event()
        finished = threading.Event()
        failure = []
        original_get = self.store.get

        def writer():
            try:
                if not boundary.wait(2):
                    raise RuntimeError('task read not reached')
                task = writer_store.get('task', 't')
                media = writer_store.get('media', 'm')
                task.update(version=2, name='other-process')
                media['camera_id'] = 'B'
                writer_store.batch([('task',task), ('media',media)])
            except BaseException as exc:
                failure.append(exc)
            finally:
                finished.set()

        def get_at_boundary(kind, ident):
            result = original_get(kind, ident)
            if kind == 'task' and not boundary.is_set():
                boundary.set()
                self.assertTrue(finished.wait(2))
            return result

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            with patch.object(self.store, 'get', side_effect=get_at_boundary):
                path = report(self.store, 't')
            self.assertEqual(failure, [])
            snapshot = json.loads(self.store.asset(sidecar_path(path)).read_text())
            self.assertEqual(snapshot['task']['version'], 1)
            self.assertEqual(snapshot['media'][0]['camera_id'], 'A')
            self.assertEqual(self.store.get('task', 't')['version'], 2)
            self.assertEqual(self.store.get('media', 'm')['camera_id'], 'B')
        finally:
            boundary.set()
            thread.join(3)
            writer_store.db.close()
        self.assertFalse(thread.is_alive())

    def test_readonly_export_does_not_pause_an_existing_processing_job(self):
        self.store.put('media', {'id':'m', 'status':'processing'})
        readonly = Store.open_readonly(self.store.root)
        try:
            self.assertEqual(readonly.get('media', 'm')['status'], 'processing')
            self.assertEqual(self.store.get('media', 'm')['status'], 'processing')
            with self.assertRaises(sqlite3.OperationalError):
                readonly.put('task', {'id':'write-denied'})
        finally:
            readonly.db.close()


if __name__ == '__main__':
    unittest.main()
