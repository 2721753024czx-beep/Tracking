"""Archive the two explicitly assigned captures and index an isolated take.

This never uses camera credentials or modifies the original capture workspace.
It creates no identity labels or successful ReID claims from planned routes.
"""
import argparse
import csv
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from tracking3.store import Store, fingerprint
from tracking3.media import probe
from tracking3.timing import metadata


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--take', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--capture-a', required=True)
    parser.add_argument('--capture-b', required=True)
    parser.add_argument('--run', default='osnet-bytetrack-v1/run01')
    parser.add_argument('--sample-fps', type=float, default=3)
    args = parser.parse_args()
    take = args.take.resolve()
    output = (take / '结果' / args.run).resolve()
    if not output.is_relative_to(take / '结果'):
        raise ValueError('运行目录超出结果目录')
    data = output / 'workbench-data'
    if data.exists():
        raise ValueError('运行工作区已存在；请换运行编号，不覆盖旧索引')
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((take / '批次.json').read_text())
    source = Store.open_readonly(args.source)
    records = []
    try:
        for camera, ident in [('cam_a', args.capture_a), ('cam_b', args.capture_b)]:
            job = source.get('capture', ident)
            if job['status'] != 'complete' or job['camera_id'] != camera:
                raise ValueError('采集未完成或摄像头编号不匹配')
            if manifest['take_id'] not in job.get('location', ''):
                raise ValueError('采集缺少当前批次标签，需先人工核对来源')
            original = source.asset(job['path'])
            original_sha = fingerprint(original)
            info = probe(original)
            checked = subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(original),
                                      '-f', 'null', '-'], capture_output=True, text=True, timeout=120)
            if checked.returncode:
                raise ValueError('完整解码失败：' + checked.stderr[-1500:])
            relative = Path('原始录像') / camera / original.name
            archived = take / relative
            archived.parent.mkdir(parents=True, exist_ok=True)
            if archived.exists() and fingerprint(archived) != original_sha:
                raise ValueError('归档目录同名文件内容不同，拒绝覆盖')
            if not archived.exists():
                shutil.copy2(original, archived)
            sidecar = source.asset(job['sidecar'])
            archived_sidecar = archived.with_suffix('.json')
            if archived_sidecar.exists() and archived_sidecar.read_bytes() != sidecar.read_bytes():
                raise ValueError('归档元数据内容不同，拒绝覆盖')
            if not archived_sidecar.exists():
                shutil.copy2(sidecar, archived_sidecar)
            assert fingerprint(archived) == original_sha == fingerprint(original)
            records.append(dict(camera_id=camera, capture_id=ident, original_media_id=job['media_id'],
                workspace_source_path=str(original), archived_relative_path=relative.as_posix(),
                sha256=original_sha, sidecar_relative_path=archived_sidecar.relative_to(take).as_posix(),
                actual_duration_s=info['duration'], codec=info['codec'], width=info['width'], height=info['height'],
                started_at_receiver=job['started_at'], finished_at_receiver=job['finished_at'],
                finish_reason=job['finish_reason'], full_decode_exit=checked.returncode,
                byte_size=original.stat().st_size, native_probe=info))
    finally:
        source.db.close()
    manifest.update(status='recorded_archived_not_identity_reviewed', source_files=records,
                    recorded_at=datetime.fromtimestamp(min(r['started_at_receiver'] for r in records),
                        ZoneInfo('Asia/Shanghai')).isoformat(), quality_review='both_full_decodes_passed',
                    notes='两路原视频和sidecar已归档且指纹与原件一致；尚未确认目标身份、跟踪或ReID效果。时间误差未知。')
    manifest['capture_assignment']['status'] = 'assigned_by_capture_id_and_batch_label'
    save(take / '批次.json', manifest)
    save(output / '录像完整性.json', dict(status='passed', records=records, identity_reviewed=False))
    dataset_root = take.parents[3]
    index = dataset_root / '采集索引.csv'
    with index.open(newline='', encoding='utf-8') as handle:
        reader = csv.DictReader(handle); fields = reader.fieldnames; rows = list(reader)
    for row in rows:
        if row['take_relative_path'] == take.relative_to(dataset_root).as_posix():
            row.update(status=manifest['status'], notes='两路已归档且完整解码通过；身份与算法效果待核对')
    with index.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(rows)

    os.environ.setdefault('YOLO_CONFIG_DIR', str(output / 'model-settings'))
    os.environ.setdefault('MPLCONFIGDIR', str(output / 'matplotlib'))
    os.environ.setdefault('YOLO_AUTOINSTALL', 'false')
    Path(os.environ['YOLO_CONFIG_DIR']).mkdir(exist_ok=True)
    Path(os.environ['MPLCONFIGDIR']).mkdir(exist_ok=True)
    import torch
    torch.set_num_threads(4)
    from tracking3.engine import Encoder, Indexer
    store = Store(data)
    encoder = Encoder(osnet=APP / 'models/osnet_x0_25_msmt17.pth', device='cpu')
    weights = APP / 'models/yolov8n.pt'
    legacy_weights = APP.parent.parent / 'Tracking-2.0/yolov8n.pt'
    if not weights.is_file() and legacy_weights.is_file():
        weights = legacy_weights
    indexer = Indexer(store, encoder, weights, device='cpu')
    progress = dict(status='indexing', encoder=encoder.version, sample_fps=args.sample_fps,
                    device='cpu', started_at=time.time(), media=[])
    try:
        for record in records:
            path = Path('sources') / record['camera_id'] / Path(record['archived_relative_path']).name
            dest = data / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.link(take / record['archived_relative_path'], dest)
            media = dict(id=record['original_media_id'], camera_id=record['camera_id'],
                path=path.as_posix(), name=dest.name, location=record['camera_id'] + ' / ' + manifest['layout_id'],
                fingerprint=record['sha256'], status='unprocessed', processed_until=-1,
                time_version=1, **metadata({}), **record['native_probe'])
            store.put('media', media)
            progress['media'].append({'id': media['id'], 'camera_id': media['camera_id'], 'status': 'unprocessed'})
        save(output / '处理进度.json', progress)
        for record in records:
            print('indexing ' + record['camera_id'], flush=True)
            started = time.perf_counter()
            indexer.process(record['original_media_id'], args.sample_fps)
            current = store.get('media', record['original_media_id'])
            entry = next(m for m in progress['media'] if m['id'] == current['id'])
            entry.update(status=current['status'], seconds=time.perf_counter()-started,
                         processed_until=current.get('processed_until'))
            save(output / '处理进度.json', progress)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
        progress.update(status='indexed_identity_review_pending', finished_at=time.time(),
                        observations=len(store.list('observation')), samples=len(store.list('index_frame')))
        save(output / '处理进度.json', progress)
    except Exception as exc:
        progress.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        save(output / '处理进度.json', progress)
        raise
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
