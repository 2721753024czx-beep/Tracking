"""Exercise two controlled RTSP sources through live preview and capture APIs.

The sources are generated fixtures on loopback, never physical-camera evidence.
Only processes created here are terminated; real workspaces are not modified.
"""
import argparse
import hashlib
import io
import json
import socket
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, ProxyHandler, build_opener

from PIL import Image

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from tracking3.capture import CaptureManager
from tracking3.live import LiveManager
from tracking3.media import probe
from tracking3.server import make_handler
from tracking3.store import Store


def wait_for(fn, description, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(.15)
    raise AssertionError('Timed out: ' + description)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--hold-for-browser', action='store_true', help='测试通过后暂留受控服务，Control+C清理')
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    store = Store(out / 'workbench-data')
    capture, live = CaptureManager(), LiveManager()
    processes, handles = [], []
    server = None
    result = {'scope': 'controlled loopback RTSP fixtures, not physical cameras', 'checks': {}, 'cleanup': {}}
    opener = build_opener(ProxyHandler({}))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    try:
        config = out / 'mediamtx.yml'
        config.write_text(f'''logLevel: warn
rtspAddress: 127.0.0.1:{port}
rtspTransports: [tcp]
rtmp: false
hls: false
webrtc: false
srt: false
moq: false
api: false
metrics: false
pprof: false
playback: false
paths:
  cam_a:
  cam_b:
''')

        def spawn(command, name):
            handle = (out / (name + '.log')).open('w')
            handles.append(handle)
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT)
            processes.append((name, process))
            return process

        spawn([str(APP.parent / '工具/mediamtx-v1.21.2/mediamtx'), str(config)], 'source-server')
        def listening():
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.2): return True
            except OSError: return False
        wait_for(listening, 'RTSP server')
        for name, source in [('cam_a', 'testsrc2=size=320x240:rate=12'),
                             ('cam_b', 'smptebars=size=320x240:rate=12')]:
            spawn(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-re', '-f', 'lavfi', '-i', source,
                   '-an', '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
                   '-g', '12', '-threads', '1', '-f', 'rtsp', '-rtsp_transport', 'tcp',
                   f'rtsp://127.0.0.1:{port}/{name}'], name + '-publisher')
        time.sleep(1)
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(store, SimpleNamespace(version='test'),
                         SimpleNamespace(lock=threading.Lock()), capture, live))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        def api(path, data=None):
            request = Request(base + path, data=None if data is None else json.dumps(data).encode(),
                              headers={'Content-Type': 'application/json'})
            with opener.open(request, timeout=15) as response: return json.load(response)
        for name in ('cam_a', 'cam_b'):
            api('/api/live-connect', dict(camera_id=name, label='controlled ' + name,
                preview_url=f'rtsp://127.0.0.1:{port}/{name}', record_url=f'rtsp://127.0.0.1:{port}/{name}',
                username='canary', password='fixture-secret'))
        wait_for(lambda: all(item['status'] == 'live' for item in api('/api/live-status')['cameras']), 'two live previews')
        frames = {}
        for name in ('cam_a', 'cam_b'):
            with opener.open(base + '/api/live.mjpg?camera_id=' + name, timeout=15) as response:
                assert response.headers['Content-Type'].startswith('multipart/x-mixed-replace')
                assert response.readline().strip() == b'--frame'
                headers = {}
                while True:
                    line = response.readline().strip()
                    if not line: break
                    key, value = line.decode().split(':', 1); headers[key.lower()] = value.strip()
                jpeg = response.read(int(headers['content-length']))
                image = Image.open(io.BytesIO(jpeg)); image.load()
                assert image.size == (320, 240), image.size
                frames[name] = {'size': list(image.size), 'sha256': hashlib.sha256(jpeg).hexdigest()}
        assert frames['cam_a']['sha256'] != frames['cam_b']['sha256']
        result['checks']['two_independent_mjpeg_frames'] = frames
        jobs = [api('/api/live-record', {'camera_id': name, 'duration': 20, 'codec': 'h264'}) for name in ('cam_a', 'cam_b')]
        # Stop preview while recording: capture must continue independently.
        api('/api/live-disconnect', {'camera_id': 'cam_a'})
        time.sleep(5)
        for job in jobs: api('/api/capture-stop', {'id': job['id']})
        wait_for(lambda: all(job['status'] not in ('recording', 'stopping') for job in api('/api/captures')), 'capture finalization')
        recordings = api('/api/captures')
        for job in recordings:
            assert job['status'] == 'complete', job
            path = store.root / job['path']
            info = probe(path)
            assert info['duration'] > 0 and info['codec'] == 'h264', info
            checked = subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(path), '-f', 'null', '-'],
                                      capture_output=True, text=True, timeout=20)
            assert checked.returncode == 0, checked.stderr
            with opener.open(base + '/asset?path=' + job['path'], timeout=5) as response:
                assert response.headers['Content-Type'] == 'video/mp4'
                assert response.read(4)
            sidecar = json.loads((store.root / job['sidecar']).read_text())
            assert sidecar['finish_reason'] == 'user_stopped'
            assert 'fixture-secret' not in json.dumps(sidecar)
            job['verified_media'] = info
        public = json.dumps(api('/api/live-status'))
        assert 'fixture-secret' not in public and 'canary:' not in public
        assert not any('fixture-secret' in item['url_redacted'] for item in recordings)
        result['checks']['recording_after_preview_disconnect'] = True
        result['checks']['two_stopped_decodable_mp4s'] = recordings
        result['checks']['public_state_and_sidecars_exclude_credentials'] = True
        result['status'] = 'passed'
        if args.hold_for_browser:
            live.stop_all()
            print(json.dumps({'controlled_ui_url': base + '/live.html', 'rtsp_port': port,
                              'source_paths': ['cam_a', 'cam_b']}), flush=True)
            try:
                threading.Event().wait(600)
            except KeyboardInterrupt:
                pass
    except Exception as exc:
        result['status'] = 'failed'
        result['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        live.stop_all(); capture.stop_all()
        if server:
            server.shutdown(); server.server_close()
        for name, process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=3)
            result['cleanup'][name] = process.poll() is not None
        for handle in handles: handle.close()
        store.db.close()
        (out / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({'status': result['status'], 'checks': list(result['checks']), 'cleanup': result['cleanup']}))


if __name__ == '__main__':
    main()
