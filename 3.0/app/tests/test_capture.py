import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tracking3.capture import CaptureManager, command, redact_url, sidecar_payload, validate
from tracking3.store import Store


class CaptureTests(unittest.TestCase):
    def test_explicit_stop_preserves_decodable_partial_video(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            manager = CaptureManager()
            job = dict(id='stop-case', status='recording', path='captures/stop.mp4',
                       camera_id='A', location='controlled', duration=60, codec='h264',
                       started_at=1, media_id=None, error=None, sidecar='captures/stop.json')
            store.put('capture', job)
            config = validate(dict(url='http://example.test/live', camera_id='A', duration=60, codec='h264'))
            config['timestamp_mode'] = 'receiver'
            job['timestamp_mode'] = 'receiver'

            class Process:
                returncode = 255
                terminated = False
                def terminate(self):
                    self.terminated = True
                def communicate(self, timeout=None):
                    manager.stop(store, job['id'])
                    return None, 'Exiting normally, received signal 15.'
            process = Process()
            def spawn(*args, **kwargs):
                output = store.root/job['path']
                output.write_bytes(b'controlled decodable partial fixture')
                return process
            info = dict(duration=2, width=320, height=240, codec='h264', fps='10/1', stream_start='0')
            with patch('tracking3.capture.subprocess.Popen', side_effect=spawn), patch('tracking3.capture.probe', return_value=info), patch('tracking3.capture.read_frame', return_value=(None, 0)):
                manager._run(store, job, config)
            saved = store.get('capture', job['id'])
            self.assertTrue(process.terminated)
            self.assertEqual(saved['status'], 'complete')
            self.assertEqual(saved['finish_reason'], 'user_stopped')
            self.assertEqual(saved['recorded_duration'], 2)
            self.assertTrue((store.root/job['path']).is_file())
            self.assertEqual(store.get('media', saved['media_id'])['duration'], 2)
            media = store.get('media', saved['media_id'])
            self.assertEqual(media['capture_video_timestamp_mode'], 'receiver')
            self.assertIn('不是摄像头拍摄时间', media['capture_video_timestamp_basis'])
            self.assertIsNone(media['start_epoch'])
            self.assertIsNone(media['uncertainty'])
            self.assertEqual(json.loads(store.asset(job['sidecar']).read_text())['timestamp_mode'], 'receiver')
            self.assertEqual(json.loads(store.asset(job['sidecar']).read_text())['finish_reason'], 'user_stopped')
            store.db.close()

    def test_stop_before_process_registration_and_invalid_partial(self):
        for invalid in (False, True):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                store = Store(directory)
                manager = CaptureManager()
                job = dict(id='early', status='recording', path='captures/early.mp4',
                           camera_id='A', location='', duration=60, codec='h264',
                           started_at=1, media_id=None, error=None, sidecar='captures/early.json')
                store.put('capture', job)
                config = validate(dict(url='http://example.test/live', camera_id='A'))
                class Process:
                    returncode = 255
                    terminated = False
                    def terminate(self): self.terminated = True
                    def communicate(self, timeout=None): return None, 'signal 15'
                process = Process()
                def spawn(*args, **kwargs):
                    manager.stop(store, job['id'])  # process is not registered yet
                    (store.root/job['path']).write_bytes(b'partial')
                    return process
                info = dict(duration=2, width=320, height=240, codec='h264', fps='10/1')
                with patch('tracking3.capture.subprocess.Popen', side_effect=spawn), patch('tracking3.capture.probe', return_value=info), patch('tracking3.capture.read_frame', side_effect=ValueError('无法解码') if invalid else None):
                    manager._run(store, job, config)
                saved = store.get('capture', job['id'])
                self.assertTrue(process.terminated)
                self.assertEqual(saved['status'], 'failed' if invalid else 'complete')
                self.assertEqual(len(store.list('media')), 0 if invalid else 1)
                self.assertEqual((store.root/job['path']).exists(), not invalid)
                store.db.close()

    def test_redacts_credentials_and_builds_safe_argument_list(self):
        config = validate({'url': 'rtsp://alice:secret@example.test:8554/live',
                           'camera_id': 'cam_a', 'duration': 12})
        self.assertNotIn('secret', config['url_redacted'])
        args = command(config, Path('/tmp/out.mp4'))
        self.assertIn('-rtsp_transport', args)
        self.assertEqual(args[-1], '/tmp/out.mp4')
        self.assertTrue(any('secret' in arg for arg in args))  # only the subprocess receives the raw URL

    def test_http_and_transcode_mode_are_supported(self):
        config = validate({'url': 'http://camera.test/live.mjpg', 'camera_id': 'cam_b',
                           'duration': 3, 'codec': 'h264'})
        args = command(config, '/tmp/out.mp4')
        self.assertIn('libx264', args)
        self.assertNotIn('-rtsp_transport', args)

    def test_receiver_mode_command_preserves_intervals_only_when_explicit(self):
        data = dict(url='http://camera.test/live.mjpg', camera_id='A', codec='h264')
        default = validate(data)
        self.assertEqual(default['timestamp_mode'], 'source')
        self.assertNotIn('-use_wallclock_as_timestamps', command(default, '/tmp/source.mp4'))
        receiver = validate(dict(data, timestamp_mode='receiver'))
        args = command(receiver, '/tmp/receiver.mp4')
        self.assertLess(args.index('-use_wallclock_as_timestamps'), args.index('-i'))
        self.assertGreater(args.index('-fps_mode'), args.index('-i'))
        self.assertEqual(args[args.index('-fps_mode')+1], 'vfr')
        self.assertEqual(args[args.index('-enc_time_base')+1], '1:1000000')
        self.assertEqual(args[-1], '/tmp/receiver.mp4')
        self.assertNotIn('-r', args)

    def test_rejects_unsupported_receiver_modes_before_start_mutates_store(self):
        cases = [dict(timestamp_mode='unknown', codec='h264'),
                 dict(timestamp_mode='receiver', codec='copy'),
                 dict(timestamp_mode='receiver', codec='h264', url='rtsp://camera.test/live')]
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            manager = CaptureManager()
            for case in cases:
                data = dict(url='http://camera.test/live', camera_id='A')
                data.update(case)
                with self.subTest(case=case), self.assertRaises(ValueError):
                    manager.start(store, data)
                self.assertEqual(store.list('capture'), [])
                self.assertEqual(manager.threads, {})
            store.db.close()

    def test_rejects_invalid_scheme_duration_and_duplicate_codec(self):
        with self.assertRaisesRegex(ValueError, 'RTSP'):
            validate({'url': 'file:///tmp/x.mp4', 'camera_id': 'cam_a'})
        with self.assertRaisesRegex(ValueError, '不能超过24小时'):
            validate({'url': 'rtsp://camera.test/live', 'camera_id': 'cam_a', 'duration': 86401})
        with self.assertRaisesRegex(ValueError, '只能是'):
            validate({'url': 'rtsp://camera.test/live', 'camera_id': 'cam_a', 'codec': 'h265'})

    def test_sidecar_records_receiver_time_basis_without_credentials(self):
        payload = sidecar_payload({'id': 'c1', 'status': 'complete', 'path': 'captures/c1.mp4',
                                   'camera_id': 'cam_a', 'location': 'lab', 'duration': 5,
                                   'codec': 'copy', 'url_redacted': 'rtsp://***:***@camera/live',
                                   'started_at': 1, 'finished_at': 6, 'media_id': 'm1',
                                   'error': None})
        self.assertEqual(payload['time_basis'], '接收端墙上时钟；不是摄像头帧时间，须另行校时')
        self.assertNotIn('secret', json.dumps(payload))
