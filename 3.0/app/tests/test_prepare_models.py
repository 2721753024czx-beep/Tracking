from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import prepare_models


class PrepareModels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.directory = self.root / 'models'
        self.contents = {'one': b'first model fixture', 'two': b'second model fixture'}
        self.models = {name: dict(filename=name + '.pt', bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(), url='https://example.invalid/' + name)
            for name, data in self.contents.items()}
        self.constants = patch.object(prepare_models, 'MODELS', self.models)
        self.constants.start()

    def tearDown(self):
        self.constants.stop(); self.tmp.cleanup()

    def response(self, request, timeout):
        return io.BytesIO(self.contents[request.full_url.rsplit('/', 1)[-1]])

    def test_offline_check_does_not_connect_or_create_directories(self):
        with patch.object(prepare_models, 'urlopen', side_effect=AssertionError('no network')):
            result = prepare_models.prepare(self.directory, check=True)
        self.assertFalse(result['ready']); self.assertTrue(result['check_only'])
        self.assertTrue(all(item['status'] == 'missing' for item in result['models']))
        self.assertFalse(self.directory.exists())

    def test_verified_downloads_are_idempotent_and_never_overwrite_local_files(self):
        with patch.object(prepare_models, 'urlopen', side_effect=self.response) as opener:
            result = prepare_models.prepare(self.directory)
        self.assertTrue(result['ready']); self.assertEqual(opener.call_count, 2)
        for name, data in self.contents.items():
            self.assertEqual((self.directory / (name + '.pt')).read_bytes(), data)
        stats = {p.name: p.stat().st_mtime_ns for p in self.directory.iterdir()}
        with patch.object(prepare_models, 'urlopen', side_effect=AssertionError('existing model must not download')):
            self.assertTrue(prepare_models.prepare(self.directory)['ready'])
        self.assertEqual({p.name: p.stat().st_mtime_ns for p in self.directory.iterdir()}, stats)

    def test_all_existing_models_are_checked_before_any_download(self):
        self.directory.mkdir()
        conflict = self.directory / 'two.pt'; conflict.write_bytes(b'keep this custom model')
        with patch.object(prepare_models, 'urlopen', side_effect=AssertionError('preflight first')):
            with self.assertRaisesRegex(ValueError, '拒绝覆盖'):
                prepare_models.prepare(self.directory)
        self.assertFalse((self.directory / 'one.pt').exists())
        self.assertEqual(conflict.read_bytes(), b'keep this custom model')
        self.assertEqual(list(self.directory.glob('*.part')), [])

    def test_bad_hash_short_or_large_download_never_publishes_partial_model(self):
        for data in (b'x' * len(self.contents['one']), b'short', b'x' * 1000):
            with self.subTest(data_size=len(data)):
                with patch.object(prepare_models, 'urlopen', return_value=io.BytesIO(data)):
                    with self.assertRaisesRegex(ValueError, '大小|SHA256'):
                        prepare_models.prepare(self.directory, model='one')
                self.assertEqual(list(self.directory.iterdir()), [])

    def test_interrupted_download_cleans_temporary_file(self):
        class Broken(io.BytesIO):
            def read(self, _): raise OSError('fixture disconnect')
        with patch.object(prepare_models, 'urlopen', return_value=Broken()):
            with self.assertRaisesRegex(OSError, 'disconnect'):
                prepare_models.prepare(self.directory, model='one')
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_concurrent_file_publication_never_clobbers_another_writer(self):
        original_link = prepare_models.os.link
        def another_writer(temporary, destination):
            destination.write_bytes(b'other writer model')
            original_link(temporary, destination)
        with patch.object(prepare_models, 'urlopen', side_effect=self.response), \
             patch.object(prepare_models.os, 'link', side_effect=another_writer):
            with self.assertRaisesRegex(ValueError, '拒绝覆盖'):
                prepare_models.prepare(self.directory, model='one')
        self.assertEqual((self.directory / 'one.pt').read_bytes(), b'other writer model')
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    def test_existing_symlink_or_directory_is_refused(self):
        self.directory.mkdir()
        path = self.directory / 'one.pt'
        path.mkdir()
        with self.assertRaisesRegex(ValueError, '普通文件'):
            prepare_models.prepare(self.directory, model='one', check=True)
        path.rmdir(); path.symlink_to(self.root / 'missing-target')
        with self.assertRaisesRegex(ValueError, '普通文件'):
            prepare_models.prepare(self.directory, model='one', check=True)

    def test_cli_check_reports_missing_models_with_nonzero_exit(self):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(prepare_models, 'urlopen', side_effect=AssertionError('offline')):
            code = prepare_models.main(['--models-dir', str(self.directory), '--check'])
        self.assertEqual(code, 1); self.assertFalse(json.loads(output.getvalue())['ready'])
        self.assertFalse(self.directory.exists())

    def test_timeout_and_unknown_model_rejected_before_io(self):
        for timeout in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                prepare_models.prepare(self.directory, timeout=timeout)
        with self.assertRaises(ValueError):
            prepare_models.prepare(self.directory, model='unknown')
        self.assertFalse(self.directory.exists())


class DefaultDetectorPath(unittest.TestCase):
    def test_new_model_preferred_legacy_fallback_and_clean_checkout_default(self):
        from tracking3 import server
        class Parsed(Exception): pass
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / '3.0/app'
            current = base / 'models/yolov8n.pt'
            legacy = root / 'Tracking-2.0/yolov8n.pt'
            current.parent.mkdir(parents=True); legacy.parent.mkdir()
            captured = []
            def parse(parser):
                captured.append(parser.get_default('weights'))
                raise Parsed()
            with patch.object(server, 'BASE', base), patch.object(server.argparse.ArgumentParser, 'parse_args', parse):
                for expected, files in [(current, []), (legacy, [legacy]), (current, [current])]:
                    for path in files: path.write_bytes(b'not loaded by this argument test')
                    with self.assertRaises(Parsed): server.main()
                    self.assertEqual(captured[-1], str(expected))
            # Argument inspection did not start a service or create a data DB.
            self.assertFalse((base / 'data').exists())


if __name__ == '__main__':
    unittest.main()
