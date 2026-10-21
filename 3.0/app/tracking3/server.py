import argparse
import io
import json
import mimetypes
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from types import SimpleNamespace

import numpy as np
from PIL import Image

from .connections import change as change_connection, source_review as task_connections, preview as connection_preview
from .engine import Encoder, Indexer, search, timestamp
from .media import absolute_start, crop, finite, probe, read_frame
from .store import Store, fingerprint, uid
from .timing import coordinate, fields as time_fields, latest, metadata as time_metadata, update as update_time, acknowledge as acknowledge_time

from .topology import configuration as topology_config, update as update_topology, scope as search_scope, set_scope

from .conditions import values as query_values, update as update_conditions
from .coverage import build as build_coverage
from .observations import change as change_observation
from .integrity import check as check_source, require as require_source
from .tasks import change_status
from .summary import build as build_summary
from .venue import camera as venue_camera, configuration as venue_config, update as update_venue
from .capture import CaptureManager
from .live import LiveManager
from .references import configure as configure_references, values as reference_values
from .bundle import create as create_bundle
from .snapshots import save as save_report_snapshot
from .trace_review import inspect as inspect_trace, preview as preview_trace
from .playback import inspect as inspect_playback, prepare as prepare_playback
from .appearance_review import generate as generate_appearance, list_reviews as appearance_reviews, decide as review_appearance
from .calibration_review import preview_frame as calibration_frame, save as save_calibration, load as load_calibration, reports as calibration_reports, bundle as bundle_calibration

BASE = Path(__file__).resolve().parent.parent


def public(item):
    return {k: v for k, v in item.items() if k != 'feature'}


def label_time(media, second):
    domain, at = coordinate(media, second)
    error = media.get('uncertainty')
    suffix = f'（±{error}s）' if error is not None else '（跨路时间误差未知）'
    if domain[0] == 'absolute':
        return datetime.fromtimestamp(at, timezone.utc).isoformat() + suffix
    if domain[0] == 'relative':
        return f'时间组 {domain[1]} 内 {at:.3f}s' + suffix
    return f'文件内 {second:.3f}s（绝对时间未知）'


def report(store, task_id):
    # Store.lock serializes this server; a SQLite read transaction also keeps
    # another process/CLI connection from mixing newer metadata into the report.
    with store.lock:
        owns_read = not store.db.in_transaction
        if owns_read:
            store.db.execute('BEGIN')
        try:
            return report_snapshot(store, task_id)
        finally:
            if owns_read:
                store.db.rollback()


def report_snapshot(store, task_id):
    task = store.get('task', task_id)
    venue = venue_config(store)
    decisions = store.decisions(task_id)
    lines = [f"# 回溯报告：{task['name']}", '', f"任务：{task_id}；结果版本：{task['version']}",
             f"生成时间：{datetime.now(timezone.utc).isoformat()}", f"入口：{task['entry']}；编码器：{task['encoder']}",
             '', '已确认事件保留全部历史核对记录，可能超出当前查询条件；最后线索基于全部已确认记录。', '', '所有位置均为录像中的观察；未知连接不代表已观察路线。', '', '## 场所与机位配置', '',
             f"场所：{venue.get('name') or '未命名'}；配置版本：{venue.get('version', 0)}。", '', '```json', json.dumps(venue, ensure_ascii=False, indent=2), '```', '', '## 已确认事件', '']
    if task.get('time_review_required'):
        lines.insert(8, '时间配置已修订，本任务的时间先后与查找范围待重新核对；原身份确认保留。')
    confirmed = []
    for d in decisions:
        if d['status'] == 'confirmed':
            o = store.get('observation', d['observation_id'])
            m = store.get('media', o['media_id'])
            confirmed.append((m, o))
    confirmed.sort(key=lambda pair: coordinate(pair[0], pair[1]['end']))
    integrity={m['id']:check_source(store,m) for m in store.list('media')}
    lines.append('列表按时间基准分组；组内名义时刻排序不代表误差范围内的确定先后。')
    for m, o in confirmed:
        profile = venue_camera(store, m['camera_id']) or {}
        place = ' / '.join(x for x in (profile.get('floor'), profile.get('area'), profile.get('orientation')) if x)
        place = f"；机位结构 {place}" if place else ''
        lines.append(f"- {m['camera_id']} / {m['location']}{place}：{label_time(m,o['start'])} 至 {label_time(m,o['end'])}；"
                     f"素材 {m['id']}，原文件 {m['name']}，文件内 {o['start']:.3f}–{o['end']:.3f}s；"
                     f"[截图](../{o['crop']})；连接未自动确认。")
        lines.append(f"  源证据核验：{integrity[m['id']]['status']}；{integrity[m['id']]['reason']}。历史身份记录保留，不代表当前源文件有效。")
    if not confirmed:
        lines.append('暂无已确认事件。')
    lines += ['', '## 人工观察与纠正版本', '']
    for o in store.list('observation'):
        if o.get('owner_task') == task_id and o.get('revision') is not None:
            lines.append(f"- 观察 {o['id']}；版本 {o['revision']}；替代 {o.get('replaces') or '无（补漏）'}；"
                         f"文件内 {o['start']:.3f}–{o['end']:.3f}s；代表帧 {o['representative']:.3f}s；"
                         f"框 {o['box']}；依据：{o['evidence']}；身份须独立核对。")
    lines += ['', '## 片段连接（独立于身份核对）', '']
    connections = task_connections(store, task_id)
    for c in connections:
        lines.append(f"- 连接 {c['id']}：{c['from_observation']} → {c['to_observation']}；"
                     f"关系 {c['relation']}；状态 {c['status']}；版本 {c['version']}；"
                     f"当前两端及依赖源核验 {'有效' if c['source_valid'] else '失效或未核实（历史关系保留）'}；"
                     f"依据：{c['evidence'] or '未登记'}；依赖：{', '.join(c['depends_on']) or '无'}；"
                     f"重核原因：{c.get('review_reason') or '无'}；时间核查：{c['assessment']}")
    if not connections:
        lines.append('尚未登记片段连接；身份确认不自动表示路线成立。')
    lines.append('连接为操作者核对的片段关系，不代表盲区内存在录像观测或精确物理通路。')
    lines += ['', '## 最后可信线索', '']
    conclusion = latest(confirmed)
    invalid_confirmed=any(not integrity[m['id']]['valid'] for m,o in confirmed)
    if invalid_confirmed:
        lines.append('已确认历史记录的源录像失效或无法核实，不能据此确定当前可核验的最后位置；请恢复原件或重新导入。')
    elif conclusion['status'] == 'unique' and not task.get('time_review_required'):
        m, o = next((m, o) for m, o in confirmed if o['id'] == conclusion['observation_id'])
        lines.append(f"{m['camera_id']} / {m['location']}，{label_time(m,o['end'])}；这不是当前实时位置。")
    elif confirmed:
        lines.append('有已确认事件，但存在未知时间或时钟不确定性，不能确定唯一最后位置。请核对上方证据。')
    lines += ['', '## 待确认及已排除', '']
    for d in decisions:
        if d['status'] != 'confirmed':
            lines.append(f"- 片段 {d['observation_id']}：{d['status']}")
    lines += ['', '未人工确认的自动检索结果均为候选；未在本节列出不代表已排除。', '', '## 处理覆盖', '']
    lines += ['', '## 导出时的查询条件', '', '```json',
              json.dumps(public(task),ensure_ascii=False,indent=2), '```',
              '', '## 导出时的候选与回看索引', '',
              '以下仅为当前条件内的排序结果，包含已排除状态供回溯；分数不是身份概率。']
    try:
        candidates=search(store,SimpleNamespace(version=task['encoder']),task_id,
                          limit=len(store.list('observation')))
        for o in candidates:
            evidence_kind = '单帧检测线索（无连续轨迹，身份须人工核对）' if o.get('detection_kind') == 'single_frame' else '观察片段'
            lines.append(f"- 候选 {o['id']}；状态 {o['status']}；分数 {o['score']}；"
                         f"证据类型 {evidence_kind}；"
                         f"镜头 {o['camera_id']}；素材 {o['media_id']}；"
                         f"文件内 {o['start']:.3f}–{o['end']:.3f}s；代表帧 {o['representative']:.3f}s；"
                         f"时间范围核对 {o['query_time_status']}；[截图](../{o['crop']})；"
                         f"[源录像](../{o['media_path']})；源核验 {o['source_integrity']['status']}。")
        if not candidates:
            lines.append('当前条件下无索引候选；不证明录像中没有目标。')
    except (ValueError, KeyError) as exc:
        lines.append(f'当前条件暂无法检索：{exc}；历史身份与连接记录见上文。')
    try:
        context = search_scope(store,task)
        lines += ['', '## 当前查找范围', '',
                  f"方式：{context['mode']}；方向：{context.get('direction', 'after')}；起点：{context['anchor_id']}；邻接版本：{context['topology_version']}",
                  f"纳入摄像头：{context['selected_cameras']}；可扩大到：{context['remaining_cameras']}",
                  f"任务条件之外：{context['outside_task_cameras']}；无素材邻居：{context['unavailable_neighbors']}",
                  '范围及邻接仅为查找条件，不代表目标沿这些路线经过；未命中不证明没有目标。']
    except ValueError as exc:
        lines += ['', f'当前查找范围待修订：{exc}']
    for m in store.list('media'):
        profile = venue_camera(store, m['camera_id']) or {}
        lines.append(f"- {m['camera_id']} / {m['name']}：{m['status']}；已推进到文件内 {max(0,m.get('processed_until',-1)):.3f}s；"
                     f"采样 {m.get('sample_fps','未设置')} fps；时间版本{m.get('time_version',1)}；"
                     f"时间依据：{m.get('time_evidence') or '未登记'}；{m.get('error') or ''}")
        lines.append(f"  素材 {m['id']}；SHA256 {m.get('fingerprint') or '未登记'}；源引用 {m.get('path') or '未登记'}；机位结构 {json.dumps(profile, ensure_ascii=False) if profile else '未配置'}。")
        lines.append(f"  源核验：{integrity[m['id']]['status']}；{integrity[m['id']]['reason']}。")
        if m.get('capture_id'):
            lines.append(f"  录制帧时间模式：{m.get('capture_video_timestamp_mode') or '历史记录未登记'}；"
                         f"帧时间依据：{m.get('capture_video_timestamp_basis') or '历史记录未登记'}。"
                         '跨镜头时间仍须独立校时，录制任务开始时刻不等于首帧拍摄时刻。')
    try:
        lines += ['', '## 录像可用与采样覆盖', '', '```json', json.dumps(build_coverage(store,task),ensure_ascii=False,indent=2), '```']
    except ValueError as exc:
        lines += ['', f'覆盖待核对：{exc}']
    lines += ['', '## 限制', '', '当前原型按采样生成候选，不证明未命中区间没有目标。',
              '跨摄像头身份由操作者核对；原型尚未生成或确认物理通路。',
              ('hsv-body 是衣着颜色流程基线，不是经过训练的人员 ReID。' if task['encoder'].startswith('hsv-body')
               else '使用专用外观模型生成候选，分数不代表身份概率；实际效果以独立素材验收为准。'),
              '本地原始视频通过素材标识引用；分享报告时须同时保留对应素材、截图与时间元数据。']
    lines += ['', '## 采样轨迹诊断', '',
              '报告元数据快照同时冻结实际采样；证据包中的tracking-diagnostics.json说明绑定版本、采样来源及缺失原因。',
              '有记录时tracking-samples.json保存当时源帧、检测框和单帧保留来源；它不是连续身份认证，零检测不证明无人。']
    try:
        lines += ['', '## 结果摘要快照', '', '```json',json.dumps(build_summary(store,task_id),ensure_ascii=False,indent=2),'```']
    except (ValueError,KeyError) as exc:
        lines += ['',f'结果摘要待补数据：{exc}']
    relative = f"reports/{task_id}-v{task['version']}-{time.time_ns()}.md"
    return save_report_snapshot(store, relative, '\n'.join(lines), task, integrity)


def make_handler(store, encoder, indexer, capture=None, live=None):
    capture = capture or CaptureManager()
    live = live or LiveManager()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send_bytes(self, body, kind='application/json', status=200):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def output(self, obj, status=200):
            self.send_bytes(json.dumps(obj, ensure_ascii=False).encode(), status=status)

        def check_local(self):
            host = self.headers.get('Host', '').split(':')[0]
            if host not in ('localhost', '127.0.0.1'):
                raise ValueError('只允许本地访问')
            origin = self.headers.get('Origin')
            if origin and urlparse(origin).netloc != self.headers.get('Host'):
                raise ValueError('拒绝跨站请求')

        def do_GET(self):
            try:
                self.check_local()
                parsed = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                if parsed.path == '/api/state':
                    self.output(dict(media=[{**m,'venue_camera':venue_camera(store,m['camera_id']),'source_integrity':check_source(store,m)} for m in store.list('media')], tasks=[public(t) for t in store.list('task')],
                                     encoder=encoder.version, processing=indexer.lock.locked()))
                elif parsed.path == '/api/coverage':
                    self.output(build_coverage(store,store.get('task',q['task'])))
                elif parsed.path == '/api/venue':
                    self.output(venue_config(store))
                elif parsed.path == '/api/captures':
                    self.output(capture.list(store))
                elif parsed.path == '/api/live-status':
                    self.output({'cameras': live.list(touch=True), 'captures': capture.list(store),
                                 'camera_metadata': live.metadata(store),
                                 'workspace': str(store.root)})
                elif parsed.path == '/api/live.mjpg':
                    self.send_live(live.get(q['camera_id']))
                elif parsed.path == '/api/calibration-reports':
                    self.output(calibration_reports(store))
                elif parsed.path == '/api/calibration-report':
                    self.output(load_calibration(store, q['id']))
                elif parsed.path == '/api/task-references':
                    task = store.get('task', q['task'])
                    self.output({'task_id': task['id'], 'task_version': task['version'],
                                 'observation_ids': reference_values(store, task),
                                 'review_required': bool(task.get('reference_review_required'))})
                elif parsed.path == '/api/summary':
                    self.output(build_summary(store,q['task']))
                elif parsed.path == '/api/topology':
                    self.output(topology_config(store))
                elif parsed.path == '/api/search-context':
                    context = search_scope(store, store.get('task',q['task']))
                    context.pop('anchor', None)
                    self.output(context)
                elif parsed.path == '/api/search':
                    limit = len(store.list('observation')) if q.get('limit') == 'all' else int(q.get('limit', 100))
                    self.output(search(store, encoder, q['task'], limit))
                elif parsed.path == '/api/timeline':
                    items = []
                    integrity={m['id']:check_source(store,m) for m in store.list('media')}
                    for d in store.decisions(q['task']):
                        o = store.get('observation', d['observation_id'])
                        m = store.get('media', o['media_id'])
                        items.append({**public(o), 'status': d['status'], 'location': m['location'],
                                      'venue_camera': venue_camera(store, m['camera_id']),
                                      'source_integrity':integrity[m['id']],
                                      'absolute_start': timestamp(m, o['start']),
                                      'absolute_end': timestamp(m, o['end']),
                                      **time_fields(m, o)})
                    self.output(items)
                elif parsed.path == '/api/impact':
                    self.output(connection_preview(store, q['task'], q.get('observation'), q.get('connection')))
                elif parsed.path == '/api/connections':
                    self.output(task_connections(store,q['task']))
                elif parsed.path == '/api/last':
                    task = store.get('task', q['task'])
                    events = []
                    for d in store.decisions(task['id']):
                        if d['status'] == 'confirmed':
                            o = store.get('observation', d['observation_id'])
                            events.append((store.get('media', o['media_id']), o))
                    result = latest(events)
                    invalid=[m['id'] for m,o in events if not check_source(store,m)['valid']]
                    if invalid:
                        result={'status':'evidence_unavailable','observation_id':None,'invalid_media':sorted(set(invalid))}
                    if task.get('time_review_required') and not invalid:
                        result = {'status': 'needs_review', 'observation_id': None}
                    if result['observation_id']:
                        m, o = next((m, o) for m, o in events if o['id'] == result['observation_id'])
                        result['event'] = {**public(o), **time_fields(m, o), 'location': m['location'],
                                           'absolute_start': timestamp(m, o['start'])}
                    self.output(result)
                elif parsed.path == '/api/observation-trace':
                    self.output(inspect_trace(store, q['task'], q['observation']))
                elif parsed.path == '/api/target-playback':
                    self.output(inspect_playback(store, q['task'], q['observation']))
                elif parsed.path == '/api/trace-frame':
                    self.output(preview_trace(store, q['task'], q['observation'], q['sample']))
                elif parsed.path == '/api/appearance-reviews':
                    self.output(appearance_reviews(store, q['task'], q['observation']))
                elif parsed.path == '/api/frame':
                    m = store.get('media', q['media'])
                    require_source(store,m)
                    rgb, at = read_frame(store.asset(m['path']), finite(q.get('time', 0), '时间', 0))
                    buf = io.BytesIO()
                    Image.fromarray(rgb).save(buf, 'JPEG')
                    self.send_bytes(buf.getvalue(), 'image/jpeg')
                elif parsed.path == '/asset':
                    for m in store.list('media'):
                        if m['path']==q['path']:
                            require_source(store,m)
                    self.send_file(store.asset(q['path']))
                else:
                    relative = 'index.html' if parsed.path == '/' else parsed.path.lstrip('/')
                    path = (BASE / 'static' / relative).resolve()
                    if not path.is_relative_to(BASE / 'static') or not path.is_file():
                        self.output({'error': '页面不存在'}, 404)
                    else:
                        self.send_file(path)
            except (ValueError, KeyError) as exc:
                self.output({'error': str(exc)}, 400)
            except Exception as exc:
                self.output({'error': f'{type(exc).__name__}: {str(exc)[:300]}'}, 500)

        def send_live(self, session):
            # Wait before sending headers so initial connection errors remain JSON.
            previous, frame = session.wait_frame()
            self.connection.settimeout(5)
            self.close_connection = True
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Connection', 'close')
            self.end_headers()
            with session.condition:
                session.watchers += 1
            try:
                while True:
                    self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ' +
                                     str(len(frame)).encode() + b'\r\n\r\n' + frame + b'\r\n')
                    self.wfile.flush()
                    previous, frame = session.wait_frame(previous)
            except (ValueError, OSError):
                pass
            finally:
                with session.condition:
                    session.watchers -= 1

        def send_file(self, path):
            size = path.stat().st_size
            start, end, status = 0, size - 1, 200
            range_header = self.headers.get('Range')
            if range_header:
                import re
                match = re.fullmatch(r'bytes=(\d+)-(\d*)', range_header)
                if not match:
                    self.send_bytes(b'', status=416)
                    return
                start = int(match[1])
                end = min(int(match[2]) if match[2] else end, end)
                if start > end:
                    self.send_bytes(b'', status=416)
                    return
                status = 206
            self.send_response(status)
            self.send_header('Content-Type', mimetypes.guess_type(path)[0] or 'application/octet-stream')
            self.send_header('Accept-Ranges', 'bytes')
            if status == 206:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.send_header('Content-Length', str(max(0, end - start + 1)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            with path.open('rb') as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    block = f.read(min(1024 * 1024, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)

        def do_POST(self):
            try:
                self.check_local()
                parsed = urlparse(self.path)
                length = int(self.headers.get('Content-Length', 0))
                if length < 0 or length > 4 * 1024**3:
                    raise ValueError('文件过大或长度无效，单文件上限4GB')
                if parsed.path == '/api/upload':
                    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                    name = Path(q.get('name', 'file')).name
                    suffix = Path(name).suffix.lower()
                    if suffix not in ('.mp4', '.mov', '.mkv', '.avi', '.jpg', '.jpeg', '.png', '.webm'):
                        raise ValueError('不支持该文件类型')
                    relative = f'uploads/{uid()}{suffix}'
                    path = store.root / relative
                    remaining = length
                    try:
                        with path.open('wb') as f:
                            while remaining:
                                block = self.rfile.read(min(1024**2, remaining))
                                if not block:
                                    raise ValueError('上传中断')
                                f.write(block)
                                remaining -= len(block)
                        self.output({'path': relative, 'name': name})
                    except Exception:
                        path.unlink(missing_ok=True)
                        raise
                    return
                if length > 1024**2:
                    raise ValueError('请求数据过大')
                data = json.loads(self.rfile.read(length) or '{}')
                if parsed.path == '/api/target-playback-prepare':
                    if not isinstance(data, dict):
                        raise ValueError('请求须包含任务与片段标识')
                    self.output(prepare_playback(store, data['task_id'], data['observation_id']))
                elif parsed.path == '/api/live-connect':
                    self.output(live.start(data, store))
                elif parsed.path == '/api/live-metadata':
                    self.output(live.update_metadata(store, data))
                elif parsed.path == '/api/live-disconnect':
                    self.output(live.stop(data['camera_id']))
                elif parsed.path == '/api/live-record':
                    self.output(live.record(store, capture, data))
                elif parsed.path == '/api/calibration-frame':
                    self.output(calibration_frame(store, data))
                elif parsed.path == '/api/calibration-report':
                    self.output(save_calibration(store, data))
                elif parsed.path == '/api/calibration-bundle':
                    self.output(bundle_calibration(store, data['id']))
                elif parsed.path == '/api/media':
                    path = store.asset(data['path'])
                    info = probe(path)
                    fp = fingerprint(path)
                    camera_id = str(data['camera_id']).strip()
                    if not camera_id:
                        raise ValueError('请输入摄像头编号')
                    if any(m['fingerprint'] == fp and m['camera_id'] == camera_id for m in store.list('media')):
                        raise ValueError('同摄像头已导入该素材，避免重复证据')
                    profile = venue_camera(store, camera_id) or {}
                    m = dict(id=uid(), path=data['path'], name=data['name'], camera_id=camera_id,
                        location=data.get('location') or profile.get('area') or profile.get('label', ''),
                        venue_camera=profile or None, fingerprint=fp, status='unprocessed',
                        processed_until=-1, time_version=1, **time_metadata(data), **info)
                    self.output(store.put('media', m))
                elif parsed.path == '/api/media-time':
                    if not indexer.lock.acquire(blocking=False):
                        raise ValueError('请先暂停并等待录像处理停止，再修订时间')
                    try:
                        self.output(update_time(store, data['media_id'], data))
                    finally:
                        indexer.lock.release()
                elif parsed.path == '/api/time-reviewed':
                    self.output(public(acknowledge_time(store, data['task_id'], data['versions'])))
                elif parsed.path == '/api/index':
                    fps = finite(data.get('sample_fps', 3), '采样率', .1)
                    if fps > 30:
                        raise ValueError('采样率不能超过30')
                    store.get('media', data['media_id'])
                    indexer.start(data['media_id'], fps)
                    self.output({'started': True})
                elif parsed.path == '/api/pause':
                    indexer.stop.set()
                    self.output({'requested': True})
                elif parsed.path == '/api/task':
                    if data['entry'] == 'photo':
                        rgb = np.asarray(Image.open(store.asset(data['path'])).convert('RGB'))
                        image = crop(rgb, data.get('box'))
                        seed = None
                    elif data['entry'] == 'monitor':
                        m = store.get('media', data['media_id'])
                        require_source(store,m)
                        rgb, at = read_frame(store.asset(m['path']), finite(data['time'], '时间', 0))
                        image = crop(rgb, data['box'])
                        seed = dict(id=uid(), media_id=m['id'], camera_id=m['camera_id'],
                            start=at, end=at, representative=at, box=data['box'], source='manual',
                            encoder=encoder.version, feature=encoder.extract(image), confidence=1,
                            sampled=False, local_track=None)
                    else:
                        raise ValueError('无效入口')
                    ident = uid()
                    relative = f'crops/query-{ident}.jpg'
                    Image.fromarray(image).save(store.root / relative)
                    task = dict(id=ident, name=data.get('name') or '目标查找', entry=data['entry'],
                        crop=relative, encoder=encoder.version, feature=encoder.extract(image),
                        cameras=data.get('cameras') or [], from_epoch=absolute_start(data.get('from')),
                        to_epoch=absolute_start(data.get('to')), status='active', version=1,
                        search_direction=data.get('direction', 'after'),
                        reference_observations=[seed['id']] if seed else [],
                        reference_review_required=False)
                    task.update(query_values(data))
                    task['anchor'] = seed['id'] if seed else None
                    if task['from_epoch'] is not None and task['to_epoch'] is not None and task['from_epoch'] > task['to_epoch']:
                        raise ValueError('查询开始时间不能晚于结束时间')
                    store.put('task', task)
                    if seed:
                        seed['crop'] = relative
                        seed['owner_task'] = ident
                        store.put('observation', seed)
                        store.decide(ident, seed['id'], 'confirmed')
                    store.log(ident, 'create', public(task))
                    self.output(public(task))
                elif parsed.path == '/api/appearance-proposal':
                    if not indexer.lock.acquire(blocking=False):
                        raise ValueError('请暂停录像处理后生成外观建议')
                    try:
                        self.output(generate_appearance(store, encoder, data))
                    finally:
                        indexer.lock.release()
                elif parsed.path == '/api/appearance-review':
                    self.output(review_appearance(store, data))
                elif parsed.path == '/api/observation':
                    if not indexer.lock.acquire(blocking=False):
                        raise ValueError('请暂停录像处理后纠正片段')
                    try:
                        self.output(change_observation(store, encoder, data))
                    finally:
                        indexer.lock.release()
                elif parsed.path == '/api/decision':
                    changed = store.decide(data['task_id'], data['observation_id'], data['status'], data.get('expected_task_version'))
                    self.output({'saved': True, 'affected_connections': changed})
                elif parsed.path == '/api/topology':
                    self.output(update_topology(store,data))
                elif parsed.path == '/api/venue':
                    self.output(update_venue(store, data))
                elif parsed.path == '/api/capture-start':
                    self.output(capture.start(store, data))
                elif parsed.path == '/api/capture-stop':
                    self.output(capture.stop(store, data['id']))
                elif parsed.path == '/api/task-references':
                    self.output(public(configure_references(store, data)))
                elif parsed.path == '/api/query-conditions':
                    self.output(public(update_conditions(store,data)))
                elif parsed.path == '/api/search-scope':
                    self.output(public(set_scope(store,data)))
                elif parsed.path == '/api/connection':
                    self.output(change_connection(store, data))
                elif parsed.path == '/api/task-status':
                    self.output(public(change_status(store,data)))
                elif parsed.path == '/api/report':
                    self.output({'path': report(store, data['task_id'])})
                elif parsed.path == '/api/report-bundle':
                    task_id = data['task_id']
                    report_path = report(store, task_id)
                    bundle_path = create_bundle(store, task_id, report_path,
                                                 bool(data.get('include_media', False)))
                    self.output({'path': bundle_path, 'report_path': report_path,
                                 'include_media': bool(data.get('include_media', False))})
                else:
                    self.output({'error': '接口不存在'}, 404)
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                self.output({'error': str(exc)}, 400)
            except Exception as exc:
                self.output({'error': f'{type(exc).__name__}: {str(exc)[:300]}'}, 500)
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data', default=str(BASE / 'data'))
    weights = BASE / 'models' / 'yolov8n.pt'
    legacy_weights = BASE.parent.parent / 'Tracking-2.0' / 'yolov8n.pt'
    if not weights.is_file() and legacy_weights.is_file():
        weights = legacy_weights
    parser.add_argument('--weights', default=str(weights))
    parser.add_argument('--device', default='cpu', choices=['cpu', 'mps'])
    parser.add_argument('--reid-onnx', help='NCHW RGB 256x128、ImageNet归一化、单embedding输出的专用ReID模型')
    parser.add_argument('--reid-osnet', help='OSNet x0.25专用行人ReID state_dict权重路径')
    parser.add_argument('--reid-device', default='cpu', choices=['cpu', 'mps'])
    args = parser.parse_args()
    # Keep library settings/cache in this project, not in a global user config.
    os.environ.setdefault('YOLO_CONFIG_DIR', str(Path(args.data) / 'model-settings'))
    os.environ.setdefault('MPLCONFIGDIR', str(Path(args.data) / 'matplotlib'))
    os.environ.setdefault('YOLO_AUTOINSTALL', 'false')
    Path(os.environ['YOLO_CONFIG_DIR']).mkdir(parents=True, exist_ok=True)
    Path(os.environ['MPLCONFIGDIR']).mkdir(parents=True, exist_ok=True)
    store = Store(args.data)
    encoder = Encoder(args.reid_onnx, osnet=args.reid_osnet, device=args.reid_device)
    indexer = Indexer(store, encoder, args.weights, args.device)
    capture = CaptureManager()
    live = LiveManager()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(store, encoder, indexer, capture, live))
    print(f'Tracking 3.0: http://127.0.0.1:{args.port}  外观编码器: {encoder.version}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        indexer.stop.set()
        if indexer.thread:
            indexer.thread.join(timeout=10)
        capture.stop_all()
        live.stop_all()
        server.server_close()


if __name__ == '__main__':
    main()
