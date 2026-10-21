import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[2] / '工具/sync_github.py'
SPEC = importlib.util.spec_from_file_location('tracking_public_sync', SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)


class GitHubSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run_git('init', '-b', 'main')
        self.run_git('config', 'user.email', 'fixture@example.invalid')
        self.run_git('config', 'user.name', 'Sync fixture')
        self.run_git('config', 'commit.gpgsign', 'false')
        self.run_git('remote', 'add', 'origin', 'https://github.com/example/project.git')

    def run_git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def write(self, path, text='public\n'):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target

    def check(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = sync.main(list(args), root=self.root)
        return code, output.getvalue()

    def commit(self):
        self.run_git('add', '-A')
        self.run_git('commit', '-m', 'fixture')

    def test_allowlist_includes_public_code_and_excludes_runtime_files(self):
        for path in ['README.md', 'LICENSE', 'AGENTS.md', '.github/workflows/ci.yml',
                     '3.0/app/requirements.lock.txt', '3.0/app/run-reid.sh',
                     '3.0/app/tracking3/vendor/osnet.py',
                     '3.0/app/tracking3/vendor/OSNET-LICENSE.txt',
                     '3.0/app/static/playback.js', '3.0/app/tests/test_github_sync.py',
                     '3.0/app/tools/export_bundle.py', '3.0/app/models/README.md',
                     '3.0/文档/开源/GITHUB_SYNC.md', '3.0/工具/sync_github.py']:
            self.assertTrue(sync.allowed(path), path)
        for path in ['private.sqlite', '.agent-memory/index.md', '3.0/app/data/state.json',
                     '3.0/app/models/model.pth', '3.0/app/examples/query.jpg',
                     '3.0/app/tracking3/__pycache__/module.pyc',
                     '3.0/app/tests/test_l01_launcher.py',
                     '3.0/app/tools/benchmark_reid.py', '3.0/app/tools/verify_real_entries.py',
                     '3.0/app/tools/prepare_connection_demo.py', '../README.md',
                     '3.0/app/static/../secret.js', '3.0/app/static/.hidden.js']:
            self.assertFalse(sync.allowed(path), path)

    def test_default_check_does_not_stage_commit_or_push(self):
        self.write('README.md')
        before = self.run_git('status', '--porcelain')
        with patch.object(sync, 'push_origin') as push:
            code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertEqual(before, self.run_git('status', '--porcelain'))
        self.assertEqual(self.run_git('ls-files'), b'')
        push.assert_not_called()
        self.assertIn('仅预览', output)

    def test_personal_path_is_rejected_but_placeholders_and_fake_password_are_allowed(self):
        private = '/' + 'Users/' + 'alice/project'
        target = self.write('README.md', private)
        code, output = self.check('--check')
        self.assertEqual(code, 2)
        self.assertIn('personal-absolute-path', output)
        self.assertNotIn(private, output)
        target.write_text('/' + 'Users/' + '<username>/project\n' +
                          '/' + 'Users/' + 'YOUR_USERNAME/project\n' +
                          "password = 'intentional-fixture'\n")
        self.assertEqual(self.check()[0], 0)

    def test_token_and_private_key_are_reported_without_secret_values(self):
        secret = 'ghp_' + 'A' * 36
        target = self.write('README.md', secret)
        code, output = self.check()
        self.assertEqual(code, 2)
        self.assertIn('github-token', output)
        self.assertNotIn(secret, output)
        target.write_text('-----BEGIN ' + 'PRIVATE KEY-----\nfixture\n')
        self.assertIn('pem-private-key', self.check()[1])

    def test_staged_secret_cannot_be_hidden_by_clean_worktree(self):
        secret = 'github_pat_' + 'Z' * 45
        target = self.write('README.md', secret)
        self.run_git('add', 'README.md')
        target.write_text('clean current version\n')
        code, output = self.check()
        self.assertEqual(code, 2)
        self.assertIn('"version": "index"', output)
        self.assertIn('github-token', output)
        self.assertNotIn(secret, output)

    def test_worktree_secret_cannot_be_hidden_by_clean_index(self):
        target = self.write('README.md')
        self.run_git('add', 'README.md')
        target.write_text('gho_' + 'B' * 36)
        code, output = self.check()
        self.assertEqual(code, 2)
        self.assertIn('"version": "worktree"', output)
        self.assertIn('github-token', output)

    def test_ignored_but_force_tracked_private_file_is_rejected(self):
        self.write('.gitignore', 'private.sqlite\n')
        self.write('private.sqlite')
        self.run_git('add', '-f', 'private.sqlite')
        code, output = self.check()
        self.assertEqual(code, 2)
        self.assertIn('outside-public-allowlist', output)
        self.assertIn('private.sqlite', output)

    def test_symlink_is_rejected_in_worktree_and_staged_version(self):
        target = self.write('README.md')
        link = self.root / 'CHANGELOG.md'
        link.symlink_to(target.name)
        self.assertIn('symlink', self.check()[1])
        self.run_git('add', 'CHANGELOG.md')
        link.unlink()
        link.write_text('safe current file\n')
        code, output = self.check()
        self.assertEqual(code, 2)
        self.assertIn('"version": "index"', output)
        self.assertIn('symlink', output)

    def test_utf8_and_size_are_checked(self):
        target = self.write('README.md')
        target.write_bytes(b'\xff\xfe')
        self.assertIn('not-utf8', self.check()[1])
        target.write_bytes(b'x' * (sync.MAX_BYTES + 1))
        self.assertIn('file-over-10MiB', self.check()[1])

    def test_origin_and_branch_are_validated_without_echoing_raw_url(self):
        self.write('README.md')
        bad = 'https://fixture-password@github.com/example/project.git'
        self.run_git('remote', 'set-url', 'origin', bad)
        code, output = self.check()
        self.assertEqual(code, 2)
        self.assertNotIn('fixture-password', output)
        self.assertNotIn(bad, output)
        self.run_git('remote', 'set-url', 'origin', 'https://elsewhere.invalid/example/project')
        self.assertEqual(self.check()[0], 2)
        self.run_git('remote', 'set-url', 'origin', 'git@github.com:example/project.git')
        self.run_git('checkout', '-b', 'other')
        self.assertEqual(self.check()[0], 0)
        self.assertIn('branch-must-be-main', self.check('--push', '--message', 'Wrong branch')[1])

    def test_content_check_allows_missing_origin_or_detached_head(self):
        self.write('README.md')
        self.run_git('remote', 'remove', 'origin')
        self.assertEqual(self.check()[0], 0)
        self.assertEqual(self.check('--push', '--message', 'Missing remote')[0], 2)
        self.commit()
        self.run_git('checkout', '--detach')
        self.assertEqual(self.check()[0], 0)
        self.assertIn('branch-must-be-main', self.check('--push', '--message', 'Detached')[1])

    def test_different_or_nongithub_push_destination_is_rejected(self):
        self.write('README.md')
        self.run_git('config', 'remote.origin.pushurl', 'https://elsewhere.invalid/example/project')
        self.assertEqual(self.check()[0], 2)
        self.run_git('config', 'remote.origin.pushurl', 'https://github.com/example/other')
        self.assertIn('destinations-differ', self.check()[1])

    def test_push_commits_checked_changes_then_normal_push_is_mocked(self):
        self.write('README.md')
        with patch.object(sync, 'push_origin') as push:
            code, output = self.check('--push', '--message', 'Public update')
        self.assertEqual(code, 0, output)
        self.assertEqual(self.run_git('log', '-1', '--format=%s').strip(), b'Public update')
        self.assertEqual(self.run_git('status', '--porcelain'), b'')
        push.assert_called_once_with(self.root)

    def test_no_new_changes_still_pushes_existing_ahead_commit(self):
        self.write('README.md')
        self.commit()
        base = self.run_git('rev-parse', 'HEAD').strip().decode()
        self.run_git('update-ref', 'refs/remotes/origin/main', base)
        self.write('README.md', 'second version\n')
        self.commit()
        before = self.run_git('rev-parse', 'HEAD')
        with patch.object(sync, 'push_origin') as push:
            code, output = self.check('--push', '--message', 'Already committed update')
        self.assertEqual(code, 0, output)
        self.assertEqual(before, self.run_git('rev-parse', 'HEAD'))
        self.assertIn('ahead 1', output)
        push.assert_called_once_with(self.root)

    def test_push_stops_before_add_or_commit_on_rejected_file(self):
        self.write('private.mp4')
        with patch.object(sync, 'push_origin') as push:
            code, output = self.check('--push', '--message', 'Never publish this')
        self.assertEqual(code, 2, output)
        self.assertEqual(self.run_git('ls-files'), b'')
        push.assert_not_called()

    def test_normal_push_uses_no_force_or_pull(self):
        with patch.object(sync, 'git') as command:
            sync.push_origin(self.root)
        command.assert_called_once_with(self.root, 'push', 'origin', 'HEAD')


if __name__ == '__main__':
    unittest.main()
