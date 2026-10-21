import io
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tracking3.live import (LiveManager, Preview, camera_key, clean_error,
                            configuration, jpeg_frames, preview_command)
from tracking3.store import Store


def config(**values):
    return configuration(dict(camera_id='cam_a', label='A', username='admin', password='canary@secret',
                              preview_url='rtsp://127.0.0.1:8554/preview',
                              record_url='rtsp://127.0.0.1:8554/main', **values))


class LiveTests(unittest.TestCase):
    def test_credentials_are_encoded_and_errors_do_not_leak_them(self):
        data = config()
        self.assertIn('admin:canary%40secret@', data['preview_input'])
        message = 'failed ' + data['preview_input'] + ' password=canary@secret canary%40secret'
        cleaned = clean_error(message, data)
        self.assertNotIn('canary', cleaned)
        self.assertNotIn('admin:', cleaned)
        self.assertIn('-rtsp_transport', preview_command(data))

    def test_rejects_wrong_protocol_embedded_credentials_and_mixed_cameras(self):
        base = dict(camera_id='cam_a', preview_url='rtsp://one.test/stream2', record_url='rtsp://one.test/stream1')
        for change in ({'preview_url': 'file:///etc/passwd'},
                       {'preview_url': 'rtsp://admin:secret@one.test/stream2'},
                       {'record_url': 'rtsp://two.test/stream1'},
                       {'camera_id': '../../bad'}, {'preview_url': 'rtsp://one.test:70000/live'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                configuration({**base, **change})
        self.assertEqual(camera_key('cam_b'), 'cam_b')

    def test_jpeg_parser_handles_split_markers_multiple_frames_and_limits(self):
        self.assertEqual(list(jpeg_frames([b'noise\xff', b'\xd8one\xff', b'\xd9\xff\xd8two\xff\xd9'])),
                         [b'\xff\xd8one\xff\xd9', b'\xff\xd8two\xff\xd9'])
        with patch('tracking3.live.MAX_JPEG', 16), self.assertRaises(ValueError):
            list(jpeg_frames([b'\xff\xd8' + b'x' * 32]))
        self.assertEqual(list(jpeg_frames([b'x' * 100000, b'\xff\xd8ok\xff\xd9'])),
                         [b'\xff\xd8ok\xff\xd9'])

    def test_worker_failure_cleans_up_and_public_status_excludes_credentials(self):
        data = config()
        class Process:
            def __init__(self):
                self.stdout = io.BytesIO(b'')
                self.stderr = io.BytesIO(('401 ' + data['preview_input']).encode())
                self.returncode = 1
            def poll(self): return self.returncode
            def wait(self, timeout=None): return self.returncode
            def kill(self): self.returncode = -9
            def terminate(self): self.returncode = -15
        preview = Preview(data, popen=lambda *a, **kw: Process())
        preview.thread.join(timeout=3)
        self.assertFalse(preview.thread.is_alive())
        public = preview.public()
        self.assertEqual(public['status'], 'failed')
        self.assertNotIn('canary', json.dumps(public))
        self.assertIsNone(preview.config)
        self.assertIsNone(preview.process)
        preview.stop()

    def test_record_uses_main_stream_and_rejects_stale_previews(self):
        manager = LiveManager()
        data = config()
        class Session:
            condition = threading.Condition()
            status = 'live'
            last_frame_at = time.time()
            config = data
        class Capture:
            def start(self, store, request): return request
        manager.sessions['cam_a'] = Session()
        job = manager.record(None, Capture(), {'camera_id': 'cam_a', 'duration': 5})
        self.assertEqual(job['url'], data['record_input'])
        self.assertEqual(job['codec'], 'h264')
        self.assertEqual(job['timestamp_mode'], 'source')
        manager.sessions['cam_a'].last_frame_at = time.time() - 20
        with self.assertRaises(ValueError):
            manager.record(None, Capture(), {'camera_id': 'cam_a'})

    def test_reconnect_does_not_reassign_a_recording_camera_id(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.put('capture', dict(id='active', status='recording', camera_id='cam_a',
                                      url_redacted='rtsp://***:***@other.test:554/main'))
            with self.assertRaises(ValueError):
                LiveManager().start(dict(camera_id='cam_a', preview_url='rtsp://one.test/sub',
                                         record_url='rtsp://one.test/main'), store)
            store.db.close()

    def test_metadata_update_keeps_preview_and_old_recording_but_changes_next_recording(self):
        manager = LiveManager()
        data = config(location='旧机位')
        class Session:
            condition = threading.Condition()
            status = 'live'
            last_frame_at = time.time()
            config = data
            label = 'A'
            location = '旧机位'
            frames = 123
            thread = object()
        class Capture:
            def start(self, store, request): return request
        preview = Session()
        manager.sessions['cam_a'] = preview
        original_thread = preview.thread
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            old_recording = dict(id='old', camera_id='cam_a', location='旧机位', status='recording')
            store.put('capture', old_recording)
            saved = manager.update_metadata(store, dict(camera_id='cam_a', label='入口', location='左侧，朝向走廊',
                                                       password='must-not-persist', record_url='rtsp://other.test/main'))
            self.assertEqual(preview.location, '左侧，朝向走廊')
            self.assertEqual(preview.frames, 123)
            self.assertIs(preview.thread, original_thread)
            self.assertEqual(preview.status, 'live')
            self.assertEqual(preview.config['record_url'], data['record_url'])
            self.assertEqual(store.get('capture', 'old'), old_recording)
            job = manager.record(store, Capture(), {'camera_id': 'cam_a'})
            self.assertEqual(job['location'], '左侧，朝向走廊')
            self.assertEqual(set(saved), {'id', 'label', 'location', 'updated_at'})
            self.assertNotIn('must-not-persist', json.dumps(manager.metadata(store)))
            store.db.close()
            reopened = Store(directory)
            self.assertEqual(LiveManager().metadata(reopened)[0]['location'], '左侧，朝向走廊')
            reopened.db.close()

    def test_invalid_metadata_does_not_partially_overwrite_saved_values(self):
        manager = LiveManager()
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            saved = manager.update_metadata(store, dict(camera_id='cam_a', label='A', location='右侧'))
            for values in ({'location': 'x' * 201}, {'label': 'x' * 101}, {'location': None}, {'camera_id': '../bad'}):
                with self.subTest(values=values), self.assertRaises(ValueError):
                    manager.update_metadata(store, dict(camera_id='cam_a', label='A', location='右侧') | values)
                self.assertEqual(manager.metadata(store), [saved])
            manager.update_metadata(store, dict(camera_id='cam_a', label='A', location=''))
            self.assertEqual(manager.metadata(store)[0]['location'], '')
            store.db.close()

    def test_connect_uses_saved_metadata_without_persisting_credentials(self):
        manager = LiveManager()
        class Session:
            def __init__(self, data): self.data = data
            def public(self): return {'status': 'connecting', 'location': self.data['location']}
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            manager.update_metadata(store, dict(camera_id='cam_a', label='入口', location='右侧'))
            with patch('tracking3.live.Preview', Session):
                result = manager.start(dict(camera_id='cam_a', username='admin', password='private-secret',
                                            preview_url='rtsp://one.test/sub', record_url='rtsp://one.test/main'), store)
            self.assertEqual(result['location'], '右侧')
            self.assertNotIn('private-secret', json.dumps(store.list('live_camera')))
            store.db.close()


if __name__ == '__main__':
    unittest.main()
