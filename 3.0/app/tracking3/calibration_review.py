"""Source-bound calibration reviews and previews; never write time metadata."""
import hashlib
import json
import tempfile
import time
import zipfile
from pathlib import Path

from .calibration import analyze
from .integrity import require as require_source
from .media import finite
from .store import Store, fingerprint, uid
from tools.prepare_event_review import _sample_frames


LABELS = {
    'consistent_at_observed_points': '给定定位误差下，已观察事件与固定偏移相容',
    'fixed_offset_inconsistent': '给定定位误差下，固定偏移无法同时对齐这些事件',
    'annotation_bounds_unknown': '事件定位误差未知，不能判断固定偏移是否足够',
    'insufficient_points': '共同事件不足3个，仅提供诊断偏移',
    'event_order_conflict': '两份录像的事件顺序冲突，须核对匹配或时间轴',
    'frame_selection_needs_review': '请求秒数与实际解码帧时刻不同，请核对导出画面后修订事件时间',
}


def prepare(data_root, input_path, output):
    output = Path(output)
    if output.exists():
        raise ValueError('输出目录已存在，请指定新目录以保留历史证据')
    raw = Path(input_path).read_bytes()
    request = json.loads(raw)
    result = analyze(request.get('points'))
    if request.get('reference_media_id') == request.get('target_media_id'):
        raise ValueError('请选择两份不同录像')
    store = Store.open_readonly(data_root)
    try:
        store.db.execute('BEGIN')
        sources, samples = {}, {}
        for role in ('reference', 'target'):
            media = store.get('media', request[role+'_media_id'])
            expected = request.get(role+'_fingerprint')
            if not expected or expected != media.get('fingerprint'):
                raise ValueError('事件文件与当前登记的源指纹不同或未绑定：'+role)
            require_source(store, media)
            path = store.asset(media['path'])
            seconds = sorted(point[role+'_second'] for point in result['points'])
            if seconds[-1] > media['duration']:
                raise ValueError('事件时刻超过录像时长：'+role)
            decoded = _sample_frames(path, seconds)
            if len(decoded) != len(seconds):
                raise ValueError('事件时刻没有可解码帧：'+role)
            samples[role] = {sample['requested_media_s']: sample for sample in decoded}
            sources[role] = dict(media_id=media['id'], camera_id=media['camera_id'],
                name=media['name'], path=media['path'], fingerprint=expected,
                time_version=media.get('time_version', 1),
                capture_video_timestamp_mode=media.get('capture_video_timestamp_mode'),
                capture_video_timestamp_basis=media.get('capture_video_timestamp_basis'))
            if fingerprint(path) != expected:
                raise ValueError('解码期间源文件变化，请重新核对：'+role)
        frame_review = []
        mismatch = False
        for index, point in enumerate(result['points']):
            row = dict(event_id=point['event_id'])
            for role in ('reference', 'target'):
                sample = samples[role][point[role+'_second']]
                row[role] = {k:v for k,v in sample.items() if k != 'image'}
                row[role]['image'] = f'frames/{index+1:03d}-{role}.jpg'
                if abs(sample['actual_media_s']-point[role+'_second']) > 1e-6:
                    mismatch = True
            frame_review.append(row)
        if mismatch:
            result['numeric_diagnostic_status'] = result['status']
            result.update(status='frame_selection_needs_review', compatible_at_observed_points=None,
                          candidate_offset_to_reference_s=None)
        result.update(format='tracking3-time-calibration-review-v1', sources=sources,
            created_at=time.time(),
            input_sha256=hashlib.sha256(raw).hexdigest(), decoded_frames=frame_review,
            frames_match_requested_times=not mismatch, event_identity_checked_by_tool=False)
        for source in sources.values():
            if fingerprint(store.asset(source['path'])) != source['fingerprint']:
                raise ValueError('生成报告前源文件变化，请重新核对')
        output.mkdir(parents=True)
        (output/'frames').mkdir()
        for index, point in enumerate(result['points']):
            for role in ('reference', 'target'):
                sample = samples[role][point[role+'_second']]
                target = output/frame_review[index][role]['image']
                sample['image'].save(target, format='JPEG', quality=90)
                frame_review[index][role]['image_sha256'] = fingerprint(target)
        (output/'input.json').write_bytes(raw)
        (output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        lines = ['# 双路多点校时诊断', '', LABELS[result['status']], '',
            f"映射：{result['mapping']}", f"诊断中位偏移：{result['median_offset_to_reference_s']:.6f}秒；不是已应用配置。",
            f"相容区间中点候选：{result['candidate_offset_to_reference_s']}；仍须人工核对，不是同步精度。",
            f"固定偏移可行区间：{result['feasible_offset_interval_s']}；空列表表示无共同解，null表示定位误差未知。",
            f"已观察偏移跨度：{result['observed_offset_range_s']:.6f}秒。",
            '', '所有计算仅基于填写的事件；工具不判断两张画面是否同一事件，不自动登记共同时间组或误差，不修改任务/媒体/审计。',
            '固定偏移不相容可能来自事件误配、时间伸缩、网络缓冲或时钟变化；不能仅据此确定物理时钟漂移。',
            '', '## 源绑定', '', '```json', json.dumps(sources,ensure_ascii=False,indent=2), '```',
            '', '## 逐事件定位核对', '']
        for index, point in enumerate(result['points']):
            lines += [f"### 事件{index+1}：{point['event_id']}", '',
                      f"填写时刻：参考{point['reference_second']}秒 / 待校{point['target_second']}秒；差{point['difference']:.6f}秒。",
                      f"定位依据：{point['evidence']}"]
            for role in ('reference', 'target'):
                row=frame_review[index][role]
                lines += [f"{role}实际解码：{row['actual_media_s']:.6f}秒，帧{row['frame_index']}，PTS {row['raw_pts']}，time_base {row['time_base']}。",
                          f"![{role}原画面](<{row['image']}>)"]
            lines.append('')
        (output/'report.md').write_text('\n'.join(lines)+'\n')
        (output/'artifact.json').write_text(json.dumps({name:fingerprint(output/name)
            for name in ('input.json','result.json','report.md')}, indent=2)+'\n')
        return result
    finally:
        store.db.close()


def preview_frame(store, data):
    if not isinstance(data, dict): raise ValueError('选帧请求须为对象')
    media = store.get('media', data['media_id'])
    if not data.get('fingerprint') or data['fingerprint'] != media.get('fingerprint'):
        raise ValueError('所选录像指纹已变化，请刷新后重新选帧')
    require_source(store, media)
    if data.get('second') is None or isinstance(data.get('second'), bool):
        raise ValueError('请填写事件帧时刻')
    second = finite(data['second'], '事件帧时刻', 0)
    if second > media['duration']:
        raise ValueError('事件时刻超过录像时长')
    decoded = _sample_frames(store.asset(media['path']), [second])
    if not decoded:
        raise ValueError('该时刻没有可解码帧')
    require_source(store, media)
    sample = decoded[0]
    relative = f'reports/calibration-previews/{uid()}.jpg'
    path = store.root/relative
    path.parent.mkdir(parents=True, exist_ok=True)
    sample['image'].save(path, format='JPEG', quality=90)
    return dict(media_id=media['id'], fingerprint=media['fingerprint'],
                time_version=media.get('time_version', 1), path=relative,
                image_sha256=fingerprint(path),
                **{key:value for key,value in sample.items() if key != 'image'})


def save(store, data):
    if not isinstance(data, dict): raise ValueError('校时请求须为对象')
    analyze(data.get('points'))  # Validate before creating any report file.
    if not all(point.get('human_matching_confirmed') is True for point in data['points']):
        raise ValueError('请逐事件核对两张画面对应同一事件的同一时刻')
    ident = uid()
    relative = f'reports/calibrations/{ident}'
    output = store.root/relative
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.json',
                                         dir=store.root/'reports', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2)
        prepare(store.root, temporary, output)
        return load(store, ident)
    finally:
        if temporary: temporary.unlink(missing_ok=True)


def load(store, ident, verify_frames=True):
    if not isinstance(ident, str) or len(ident) != 32 or any(c not in '0123456789abcdef' for c in ident):
        raise ValueError('校时报告编号无效')
    relative = f'reports/calibrations/{ident}'
    artifact = json.loads(store.asset(relative+'/artifact.json').read_text())
    for name in ('input.json','result.json','report.md'):
        if fingerprint(store.asset(relative+'/'+name)) != artifact.get(name):
            raise ValueError('校时报告或元数据已改变，请恢复原件或重新核对')
    result = json.loads(store.asset(relative+'/result.json').read_text())
    if verify_frames:
        for row in result['decoded_frames']:
            for role in ('reference','target'):
                frame=row[role]
                if fingerprint(store.asset(relative+'/'+frame['image'])) != frame['image_sha256']:
                    raise ValueError('校时报告原画面已改变，请恢复原件或重新核对')
    return dict(id=ident, report_path=relative+'/report.md', result_path=relative+'/result.json',
                frame_base=relative, label=LABELS[result['status']], **result)


def reports(store):
    root = store.root/'reports/calibrations'
    if not root.is_dir(): return []
    rows = []
    directories = sorted(root.iterdir(), key=lambda path:path.stat().st_mtime, reverse=True)
    for path in directories[:30]:
        if not path.is_dir() or not (path/'artifact.json').is_file(): continue
        result = load(store, path.name, verify_frames=False)
        rows.append({key:result[key] for key in ('id','created_at','label','sources','point_count',
                                               'candidate_offset_to_reference_s','report_path')})
    return rows


def bundle(store, ident):
    result = load(store, ident)
    base = f'reports/calibrations/{ident}'
    expected = json.loads(store.asset(base+'/artifact.json').read_text())
    expected['artifact.json'] = fingerprint(store.asset(base+'/artifact.json'))
    for index,row in enumerate(result['decoded_frames']):
        for role in ('reference','target'):
            frame=row[role]
            name=f'frames/{index+1:03d}-{role}.jpg'
            if frame['image'] != name:
                raise ValueError('校时原帧路径不符合保存格式')
            expected[name]=frame['image_sha256']
    relative = f'reports/calibration-bundles/{ident}-{uid()}.zip'
    output = store.root/relative
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            for name,digest in expected.items():
                content=store.asset(base+'/'+name).read_bytes()
                if hashlib.sha256(content).hexdigest() != digest:
                    raise ValueError('打包期间校时证据变化，请恢复原件后重试')
                archive.writestr(name,content)
            archive.writestr('bundle-manifest.json',json.dumps(dict(
                format='tracking3-calibration-bundle-v1',calibration_id=ident,
                members_sha256=expected,includes_source_video=False,time_metadata_changed=False),
                ensure_ascii=False,indent=2)+'\n')
        return dict(path=relative,sha256=fingerprint(output),includes_source_video=False)
    except Exception:
        output.unlink(missing_ok=True)
        raise

