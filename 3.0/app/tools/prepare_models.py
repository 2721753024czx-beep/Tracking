"""Download pinned official model files, or check local files without network.

Uses only the Python standard library. Existing files are never overwritten;
every download is checked before an atomic, no-clobber publication.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from urllib.request import Request, urlopen


APP = Path(__file__).resolve().parents[1]
MODELS = {
    'yolov8n': dict(filename='yolov8n.pt', bytes=6549796,
        sha256='f59b3d833e2ff32e194b5bb8e08d211dc7c5bdf144b90d2c8412c47ccfc83b36',
        url='https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8n.pt',
        source='https://github.com/ultralytics/assets/releases/expanded_assets/v8.4.0'),
    'osnet': dict(filename='osnet_x0_25_msmt17.pth', bytes=9336983,
        sha256='cf55163d78fc44c62c82f85ab62d39f10438679b5abe8c698ae08cfa84aa6e18',
        url='https://huggingface.co/kaiyangzhou/osnet/resolve/a5c5cc037c24235cda3b21085b93ad77c9616224/'
            'osnet_x0_25_msmt17_combineall_256x128_amsgrad_ep150_stp60_lr0.0015_b64_fb10_softmax_labelsmooth_flip_jitter.pth',
        source='https://huggingface.co/kaiyangzhou/osnet'),
}


def fingerprint(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verify(path, specification):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'{path.name}: 已有路径不是普通文件，拒绝修改')
    if path.stat().st_size != specification['bytes'] or fingerprint(path) != specification['sha256']:
        raise ValueError(f"{path.name}: 已有文件与固定版本的大小/SHA256不符；拒绝覆盖。"
                         '请另选空目录，或自行保留原件后处理。')


def _download(directory, specification, timeout):
    destination = directory / specification['filename']
    fd, temporary = tempfile.mkstemp(prefix='.' + destination.name + '-', suffix='.part', dir=directory)
    temporary = Path(temporary)
    try:
        digest, received = hashlib.sha256(), 0
        request = Request(specification['url'], headers={'User-Agent': 'tracking3-model-preparation/1'})
        with os.fdopen(fd, 'wb') as handle:
            with urlopen(request, timeout=timeout) as response:
                while True:
                    block = response.read(min(1024 * 1024, specification['bytes'] - received + 1))
                    if not block:
                        break
                    received += len(block)
                    if received > specification['bytes']:
                        raise ValueError(f'{destination.name}: 下载大小超过固定版本，未保存')
                    digest.update(block); handle.write(block)
            handle.flush(); os.fsync(handle.fileno())
        if received != specification['bytes'] or digest.hexdigest() != specification['sha256']:
            raise ValueError(f'{destination.name}: 下载大小或SHA256校验失败，未保存')
        try:
            # Unlike replace(), link() fails if a concurrent writer created the
            # destination. The complete, verified file appears in one operation.
            os.link(temporary, destination)
        except FileExistsError:
            verify(destination, specification)
    finally:
        temporary.unlink(missing_ok=True)


def prepare(directory, *, model='all', check=False, timeout=60):
    directory = Path(directory).expanduser().resolve()
    if model != 'all' and model not in MODELS:
        raise ValueError('未知模型')
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('网络超时须为正数秒')
    specifications = MODELS if model == 'all' else {model: MODELS[model]}
    missing, results = [], []
    # Inspect every existing destination before creating a directory or opening
    # any connection. One conflicting local model prevents all downloads.
    for name, specification in specifications.items():
        path = directory / specification['filename']
        present = path.exists() or path.is_symlink()
        if present:
            verify(path, specification)
        else:
            missing.append((path, specification))
        results.append(dict(model=name, path=str(path), status='verified' if present else 'missing',
            sha256=specification['sha256'], bytes=specification['bytes'], url=specification['url']))
    if check or not missing:
        return dict(ready=not missing, check_only=check, models=results)
    directory.mkdir(parents=True, exist_ok=True)
    for path, specification in missing:
        _download(directory, specification, timeout)
    for item in results:
        item['status'] = 'verified'
    return dict(ready=True, check_only=False, models=results)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models-dir', type=Path, default=APP / 'models')
    parser.add_argument('--model', choices=('all', *MODELS), default='all')
    parser.add_argument('--check', action='store_true', help='只校验本地文件；不联网、不创建目录')
    parser.add_argument('--timeout', type=float, default=60, help='单次网络操作超时秒数（默认60）')
    args = parser.parse_args(argv)
    try:
        result = prepare(args.models_dir, model=args.model, check=args.check, timeout=args.timeout)
    except (ValueError, OSError) as exc:
        parser.exit(1, f'模型准备失败：{exc}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
