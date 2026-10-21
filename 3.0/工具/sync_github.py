#!/usr/bin/env python3
"""Preview public files, or explicitly commit and push a checked GitHub main branch.

Only the Python standard library and Git are required. No fetch, pull, force push,
credential storage, file deletion, or history rewriting is performed.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from urllib.parse import urlsplit


MAX_BYTES = 10 * 1024 * 1024
ROOT_FILES = frozenset({
    'README.md', 'CHANGELOG.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
    'AGENTS.md', '.gitignore', '.gitattributes',
})
APP_FILES = frozenset({'README.md', '.gitignore'})
EXCLUDED_TOOLS = frozenset({
    'benchmark_reid.py', 'verify_real_entries.py', 'prepare_connection_demo.py',
})
SECRET_RULES = (
    ('github-token', re.compile(r'\bgh[pousr]_[A-Za-z0-9]{25,}\b')),
    ('github-token', re.compile(r'\bgithub_pat_[A-Za-z0-9_]{25,}\b')),
    ('pem-private-key', re.compile(
        r'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----')),
)
# Split the literal so this scanner's own source is not a personal-path fixture.
PRIVATE_HOME = re.compile('/' + r'Users/([^/\s\"\x27<>`]+)')
PLACEHOLDER_USERS = frozenset({
    'user', 'username', 'your_username', 'your-user', 'your_user', 'yourname',
    'example', 'demo', 'placeholder', '$user', '${user}',
})


class SyncError(Exception):
    """An error whose message is safe to display without Git stderr or URLs."""


@dataclass(frozen=True)
class Finding:
    path: str
    rule: str
    version: str


@dataclass
class Preview:
    paths: list[str]
    staged: int
    modified: int
    remote: str
    ahead: int | None
    behind: int | None


def git(repo, *args, ok=(0,)):
    try:
        result = subprocess.run(['git', '-C', str(repo), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=False)
    except OSError as exc:
        raise SyncError('git-unavailable') from exc
    if result.returncode not in ok:
        # Git errors can contain credentialed URLs, paths, and commit messages.
        raise SyncError('git-command-failed: ' + args[0])
    return result


def nul_paths(data):
    return [part.decode('utf-8', errors='surrogateescape')
            for part in data.split(b'\0') if part]


def allowed(path):
    try:
        path.encode('utf-8')
    except UnicodeError:
        return False
    parts = PurePosixPath(path).parts
    if (not parts or path.startswith('/') or '\\' in path or
            any(part in ('', '.', '..') for part in path.split('/'))):
        return False
    if path in ROOT_FILES or path == '.github/workflows/ci.yml':
        return True
    if path == '3.0/工具/sync_github.py':
        return True
    if parts[:3] == ('3.0', '文档', '开源'):
        return len(parts) == 4 and parts[-1].endswith('.md') and not parts[-1].startswith('.')
    if parts[:2] != ('3.0', 'app') or len(parts) < 3:
        return False
    rest = parts[2:]
    if any(part.startswith('.') for part in rest):
        return len(rest) == 1 and rest[0] == '.gitignore'
    if len(rest) == 1:
        name = rest[0]
        return (name in APP_FILES or
                (name.startswith('requirements') and name.endswith('.txt')) or
                (name.startswith('run') and name.endswith('.sh')))
    directory, name = rest[0], rest[-1]
    if directory == 'tracking3':
        return name.endswith('.py') or rest == ('tracking3', 'vendor', 'OSNET-LICENSE.txt')
    if directory == 'static':
        return PurePosixPath(name).suffix in ('.html', '.css', '.js')
    if directory == 'tests':
        return name != 'test_l01_launcher.py' and PurePosixPath(name).suffix in ('.py', '.cjs')
    if directory == 'tools':
        return name.endswith('.py') and name not in EXCLUDED_TOOLS
    return rest == ('models', 'README.md')


def content_findings(path, content, version):
    if len(content) > MAX_BYTES:
        return [Finding(path, 'file-over-10MiB', version)]
    try:
        text = content.decode('utf-8')
    except UnicodeError:
        return [Finding(path, 'not-utf8', version)]
    findings = []
    for rule, pattern in SECRET_RULES:
        if pattern.search(text):
            findings.append(Finding(path, rule, version))
    if any(match.group(1).lower() not in PLACEHOLDER_USERS
           for match in PRIVATE_HOME.finditer(text)):
        findings.append(Finding(path, 'personal-absolute-path', version))
    return findings


def index_entries(repo):
    entries = []
    for item in git(repo, 'ls-files', '--stage', '-z').stdout.split(b'\0'):
        if not item:
            continue
        try:
            metadata, raw_path = item.split(b'\t', 1)
            mode, oid, stage = metadata.decode('ascii').split()
            path = raw_path.decode('utf-8', errors='surrogateescape')
        except (ValueError, UnicodeError) as exc:
            raise SyncError('invalid-git-index') from exc
        if not re.fullmatch(r'[a-f0-9]{40,64}', oid):
            raise SyncError('invalid-git-index')
        entries.append((path, mode, oid, stage))
    return entries


def scan(repo):
    """Scan every nonignored candidate and every staged blob, including ignored tracked files."""
    paths = sorted(set(nul_paths(git(repo, 'ls-files', '--cached', '--others',
                                   '--exclude-standard', '-z').stdout)))
    findings = []
    for path in paths:
        if not allowed(path):
            findings.append(Finding(path, 'outside-public-allowlist', 'worktree'))
            continue
        local = repo / path
        # Never follow a symlink, including one in an ancestor directory.
        if any((repo.joinpath(*PurePosixPath(path).parts[:i])).is_symlink()
               for i in range(1, len(PurePosixPath(path).parts) + 1)):
            findings.append(Finding(path, 'symlink', 'worktree'))
            continue
        try:
            info = local.stat()
        except FileNotFoundError:
            continue  # A tracked deletion is legitimate; its index blob is checked below.
        except OSError:
            findings.append(Finding(path, 'unreadable-file', 'worktree'))
            continue
        if not stat.S_ISREG(info.st_mode):
            findings.append(Finding(path, 'not-regular-file', 'worktree'))
        elif info.st_size > MAX_BYTES:
            findings.append(Finding(path, 'file-over-10MiB', 'worktree'))
        else:
            try:
                findings.extend(content_findings(path, local.read_bytes(), 'worktree'))
            except OSError:
                findings.append(Finding(path, 'unreadable-file', 'worktree'))
    for path, mode, oid, stage in index_entries(repo):
        if not allowed(path):
            findings.append(Finding(path, 'outside-public-allowlist', 'index'))
            continue
        if stage != '0':
            findings.append(Finding(path, 'unmerged-index', 'index'))
            continue
        if mode not in ('100644', '100755'):
            findings.append(Finding(path, 'symlink' if mode == '120000' else 'unsupported-git-mode', 'index'))
            continue
        size = int(git(repo, 'cat-file', '-s', oid).stdout)
        if size > MAX_BYTES:
            findings.append(Finding(path, 'file-over-10MiB', 'index'))
        else:
            findings.extend(content_findings(path, git(repo, 'cat-file', 'blob', oid).stdout, 'index'))
    return paths, sorted(set(findings), key=lambda item: (item.path, item.version, item.rule))


def github_remote(url):
    """Return a printable GitHub identity; never return an unvalidated raw URL."""
    if url.startswith('git@github.com:'):
        path = url[len('git@github.com:'):]
    else:
        try:
            parsed = urlsplit(url)
            valid = (parsed.hostname == 'github.com' and parsed.port is None and
                     not parsed.query and not parsed.fragment)
        except ValueError as exc:
            raise SyncError('origin-must-be-github-without-credentials') from exc
        if parsed.scheme == 'https':
            valid = valid and parsed.username is None and parsed.password is None
        elif parsed.scheme == 'ssh':
            valid = valid and parsed.username == 'git' and parsed.password is None
        else:
            valid = False
        if not valid:
            raise SyncError('origin-must-be-github-without-credentials')
        path = parsed.path.lstrip('/')
    if path.endswith('.git'):
        path = path[:-4]
    parts = path.split('/')
    if (len(parts) != 2 or any(part in ('.', '..', '') for part in parts) or
            not all(re.fullmatch(r'[A-Za-z0-9_.-]+', part) for part in parts)):
        raise SyncError('invalid-github-repository')
    return 'github.com/' + '/'.join(parts)


def preflight(repo, require_push=False):
    top = git(repo, 'rev-parse', '--show-toplevel').stdout.decode().strip()
    if Path(top).resolve() != repo.resolve():
        raise SyncError('run-from-project-root')
    branch = git(repo, 'branch', '--show-current').stdout.decode().strip()
    if require_push and branch != 'main':
        raise SyncError('branch-must-be-main')
    identities = []
    for extra in ((), ('--push',)):
        urls = git(repo, 'remote', 'get-url', *extra, '--all', 'origin',
                   ok=(0, 2, 128)).stdout.decode().splitlines()
        if not urls and not require_push:
            identities.append('origin-not-configured')
            continue
        if len(urls) != 1:
            raise SyncError('origin-must-have-one-github-destination')
        identities.append(github_remote(urls[0]))
    if identities[0].lower() != identities[1].lower():
        raise SyncError('origin-fetch-push-destinations-differ')
    ahead = behind = None
    has_head = git(repo, 'rev-parse', '--verify', '--quiet', 'HEAD', ok=(0, 1)).returncode == 0
    has_remote = git(repo, 'rev-parse', '--verify', '--quiet', 'refs/remotes/origin/main', ok=(0, 1)).returncode == 0
    if has_head and has_remote:
        counts = git(repo, 'rev-list', '--left-right', '--count', 'refs/remotes/origin/main...HEAD').stdout.split()
        behind, ahead = map(int, counts)
    paths, findings = scan(repo)
    preview = Preview(paths, len(set(nul_paths(git(repo, 'diff', '--cached', '--name-only', '-z').stdout))),
                      len(set(nul_paths(git(repo, 'diff', '--name-only', '-z').stdout))),
                      identities[1], ahead, behind)
    return preview, findings


def report(preview, findings):
    print('公开文件检查 → ' + preview.remote)
    print(f'候选 {len(preview.paths)} 个；暂存变更 {preview.staged} 个；工作区变更 {preview.modified} 个。')
    if preview.ahead is None:
        print('本地没有完整远端比较依据（本次不 fetch）。')
    else:
        print(f'相对本地 origin/main：ahead {preview.ahead}，behind {preview.behind}（本次不 fetch）。')
    for finding in findings:
        print(json.dumps({'path': finding.path, 'version': finding.version, 'rule': finding.rule}, ensure_ascii=True))
    if findings:
        print(f'拒绝同步：{len(findings)} 项检查未通过。')
    else:
        print('检查通过。未发现上述规则命中；本检查不扫描历史提交。')


def push_origin(repo):
    try:
        git(repo, 'push', 'origin', 'HEAD')
    except SyncError as exc:
        raise SyncError('normal-push-failed: 检查认证、网络或远端分歧；未 force/pull') from exc


def synchronize(repo, message=None, push=False):
    preview, findings = preflight(repo, require_push=push)
    report(preview, findings)
    if findings:
        return 2
    if not push:
        print('仅预览：没有暂存、提交或推送。')
        return 0
    if not message or not message.strip():
        raise SyncError('push-requires-nonempty-message')
    if preview.behind:
        raise SyncError('local-main-behind-origin: 请先人工处理远端分歧')
    if preview.paths:
        git(repo, 'add', '-A', '--', *preview.paths)
    # The index may differ from the worktree. Recheck after staging before commit.
    checked, findings = preflight(repo, require_push=True)
    if findings:
        report(checked, findings)
        return 2
    if git(repo, 'diff', '--cached', '--quiet', ok=(0, 1)).returncode:
        git(repo, 'commit', '-m', message)
        print('已创建通过检查的提交。')
    else:
        print('没有新文件变更；仍执行正常推送以同步已有本地提交。')
    if git(repo, 'rev-parse', '--verify', '--quiet', 'HEAD', ok=(0, 1)).returncode:
        raise SyncError('no-commit-to-push')
    push_origin(repo)
    print('已完成正常 push origin HEAD。')
    return 0


def main(argv=None, root=None):
    parser = argparse.ArgumentParser(prog='sync_github.py', description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--check', action='store_true', help='只预览扫描（默认）')
    modes.add_argument('--push', action='store_true', help='检查后暂存、必要时提交并正常推送')
    parser.add_argument('--message', help='--push 必填的提交说明')
    args = parser.parse_args(argv)
    if args.push and (not args.message or not args.message.strip()):
        parser.error('--push 需要非空 --message')
    if args.message is not None and not args.push:
        parser.error('--message 仅与 --push 同用')
    repo = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    try:
        return synchronize(repo, args.message, args.push)
    except (SyncError, OSError, ValueError) as exc:
        # Only controlled SyncError messages are displayed, never raw OS/Git errors.
        print('同步未执行完成：' + (str(exc) if isinstance(exc, SyncError) else 'local-check-failed'))
        return 2


if __name__ == '__main__':
    sys.exit(main())
