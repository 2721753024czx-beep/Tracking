"""Verify actual ffmpeg capture and manual stop against a paced MJPEG fixture.

No physical camera or recognition benchmark is involved.
"""
import argparse
import hashlib
import io
import json
import re
import shutil
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import unquote
from urllib.request import Request, urlopen

import av
from PIL import Image

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from tracking3.capture import CaptureManager
from tracking3 import capture as capture_module
from tracking3.engine import Encoder, Indexer
from tracking3.media import read_frame
from tracking3.server import make_handler
from tracking3.store import Store, fingerprint
from tools.run_synthetic_workflow import blue_body_detector


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--timestamp-mode', choices=('source', 'receiver'), default='source',
        help='通过正式采集API选择帧时间模式；默认保留原流模式')
    parser.add_argument('--pause-after-frame', type=int, default=0)
    parser.add_argument('--pause-seconds', type=float, default=0)
    args = parser.parse_args()
    if not 0 <= args.pause_after_frame < 160 or not 0 <= args.pause_seconds <= 5:
        parser.error('停顿须在0–159帧后，时长0–5秒')
    if args.output.exists():
        raise SystemExit('请指定尚不存在的输出目录')
    source = APP.parent/'数据准备/synthetic-two-camera-v1/demo_cam_a.mp4'
    jpeg_frames = []
    with av.open(str(source)) as container:
        for frame in container.decode(video=0):
            blob = io.BytesIO()
            frame.to_image().save(blob, format='JPEG', quality=85)
            jpeg_frames.append(blob.getvalue())
    assert jpeg_frames
    stopped = threading.Event()
    source_finished = threading.Event()
    metrics = {'sent_frames':0, 'send_fps':20, 'first_frame_at':None, 'last_frame_at':None,
               'frame_sent_at':[], 'pause_after_frame':args.pause_after_frame,
               'pause_seconds':args.pause_seconds}
    original_command = capture_module.command
    used_commands = []
    def observed_command(config, output):
        command = original_command(config, output)
        # The fixture URL contains no credentials. Never use this logging for cameras.
        used_commands.append(command)
        return command
    capture_module.command = observed_command

    class Stream(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
            self.end_headers()
            try:
                while not stopped.is_set():
                    i = metrics['sent_frames']
                    blob = jpeg_frames[i % len(jpeg_frames)]
                    packet = b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(blob)).encode()+b'\r\n\r\n'+blob+b'\r\n'
                    self.wfile.write(packet)
                    self.wfile.flush()
                    at = time.time()
                    metrics.update(sent_frames=i+1, last_frame_at=at)
                    metrics['frame_sent_at'].append(at)
                    if metrics['first_frame_at'] is None: metrics['first_frame_at'] = at
                    if i+1 == args.pause_after_frame:
                        stopped.wait(args.pause_seconds)
                    stopped.wait(1/metrics['send_fps'])
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                source_finished.set()

    source_server = ThreadingHTTPServer(('127.0.0.1', 0), Stream)
    api_server = None
    store = None
    manager = CaptureManager()
    server_threads = []
    try:
        args.output.mkdir(parents=True, exist_ok=False)
        store = Store(args.output/'data')
        encoder = Encoder()
        indexer = Indexer(store, encoder, 'unused-controlled-detector.pt')
        api_server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(store, encoder, indexer, manager))
        for server in (source_server, api_server):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            server_threads.append(thread)
        base = f'http://127.0.0.1:{api_server.server_port}'
        def call(route, data=None):
            request = Request(base+route, data=json.dumps(data).encode() if data else None,
                              headers={'Content-Type':'application/json'})
            with urlopen(request, timeout=15) as response: return json.load(response)
        job = call('/api/capture-start', dict(url=f'http://127.0.0.1:{source_server.server_port}/stream',
            camera_id='paced_mjpeg', location='受控限速MJPEG模拟', duration=30, codec='h264',
            timestamp_mode=args.timestamp_mode))
        initial = dict(job)
        deadline = time.monotonic()+25
        while metrics['sent_frames'] < 160 and time.monotonic() < deadline:
            current = store.get('capture', job['id'])
            assert current['status']=='recording', current
            time.sleep(.05)
        process = manager.processes.get(job['id'])
        assert metrics['sent_frames']>=160 and process and process.poll() is None
        before_stop = json.loads(json.dumps(metrics))
        stopping = call('/api/capture-stop', {'id':job['id']})
        assert stopping['status']=='stopping'
        print(json.dumps({'action':'stop', 'sent_frames':before_stop['sent_frames'], 'process_live_before_stop':True}), flush=True)
        deadline = time.monotonic()+15
        while time.monotonic()<deadline:
            job = next(j for j in call('/api/captures') if j['id']==job['id'])
            if job['status'] in ('complete','failed'): break
            time.sleep(.05)
        stopped.set()
        assert source_finished.wait(timeout=2), '模拟源未结束，不能冻结发送观测'
        (args.output/'capture-observation.json').write_text(json.dumps(dict(initial=initial,
            before_stop=before_stop, stopping=stopping, final=job,
            process_returncode=process.poll(), source_metrics=dict(metrics)), ensure_ascii=False, indent=2)+'\n')
        assert job['status']=='complete' and job['finish_reason']=='user_stopped', job
        media = store.get('media', job['media_id'])
        assert 0<media['duration']<initial['duration']
        assert media['start_epoch'] is None and media['uncertainty'] is None
        assert job['timestamp_mode'] == args.timestamp_mode
        assert media['capture_video_timestamp_mode'] == args.timestamp_mode
        video = store.asset(media['path'])
        assert fingerprint(video)==media['fingerprint']
        pts = []
        with av.open(str(video)) as container:
            for frame in container.decode(video=0):
                assert frame.pts is not None
                pts.append(float(frame.pts*frame.time_base))
        assert len(pts)>1 and all(b>=a for a,b in zip(pts,pts[1:]))
        sidecar = json.loads(store.asset(job['sidecar']).read_text())
        assert sidecar['finish_reason']=='user_stopped' and sidecar['recorded_duration']==media['duration']
        assert sidecar['stop_requested_at'] <= sidecar['finished_at']
        assert sidecar['timestamp_mode'] == args.timestamp_mode
        assert sidecar['video_timestamp_basis'] == media['capture_video_timestamp_basis']
        indexer.process(media['id'], 3, detector=blue_body_detector)
        assert store.get('media', media['id'])['status']=='complete'
        rgb, _ = read_frame(video, min(1, media['duration']/2))
        box = blue_body_detector(rgb)[0][0]
        Image.fromarray(rgb[box[1]:box[3],box[0]:box[2]]).save(store.root/'uploads/query.png')
        task = call('/api/task', dict(entry='photo', path='uploads/query.png', name='MJPEG中途停止回溯回归'))
        candidates = call('/api/search?task='+task['id'])
        assert candidates
        result = call('/api/report-bundle', dict(task_id=task['id'], include_media=True))
        archive_path = store.asset(result['path'])
        with zipfile.ZipFile(archive_path) as archive:
            names=archive.namelist()
            assert archive.testzip() is None and len(names)==len(set(names))
            assert all(not PurePosixPath(n).is_absolute() and '..' not in PurePosixPath(n).parts for n in names)
            manifest=json.loads(archive.read('manifest.json'))
            assert manifest['media'][0]['capture_video_timestamp_mode'] == args.timestamp_mode
            assert manifest['media'][0]['capture_video_timestamp_basis'] == sidecar['video_timestamp_basis']
            report=archive.read('report.md')
            assert sidecar['video_timestamp_basis'] in report.decode()
            assert hashlib.sha256(report).hexdigest()==manifest['bundled_report_sha256']
            for m in manifest['media']:
                assert hashlib.sha256(archive.read(m['included_in_bundle'])).hexdigest()==m['fingerprint']
            member='capture/'+media['id']+'.json'
            assert archive.read(member)==store.asset(job['sidecar']).read_bytes()
            refs=[unquote(r.strip('<>')) for r in re.findall(r'!?\[[^\]\n]*\]\(([^)\n]+)\)',report.decode()) if not r.startswith(('http://','https://','#'))]
            assert all(r in names for r in refs)
            archive.extractall(args.output/'extracted')
        shutil.copy2(archive_path, args.output/'evidence.zip')
        details=dict(success=True, fixture='paced synthetic multipart MJPEG; real ffmpeg; blue detector/HSV',
            timestamp_mode=args.timestamp_mode,
            ffmpeg_commands=used_commands, decoded_pts=pts,
            original_source_sha256=fingerprint(source), final_capture=job, media=media,
            full_decoded_frame_count=len(pts), pts_first=pts[0], pts_last=pts[-1],
            receiver_elapsed=job['finished_at']-job['started_at'], process_returncode=process.returncode,
            candidate_count=len(candidates), task_id=task['id'], bundle_sha256=fingerprint(archive_path),
            source_and_sidecar_sha_verified=True, all_report_links_exist=True, source_metrics=dict(metrics))
        (args.output/'result.json').write_text(json.dumps(details,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({k:details[k] for k in ('success','full_decoded_frame_count','receiver_elapsed','process_returncode','candidate_count')},ensure_ascii=False),flush=True)
    finally:
        stopped.set()
        manager.stop_all()
        for thread in list(manager.threads.values()): thread.join(timeout=5)
        for server in (api_server, source_server):
            if server:
                if server_threads: server.shutdown()
                server.server_close()
        for thread in server_threads: thread.join(timeout=5)
        if store: store.db.close()
        capture_module.command = original_command


if __name__=='__main__': main()
