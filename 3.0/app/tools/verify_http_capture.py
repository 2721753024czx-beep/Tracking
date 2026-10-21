"""Real ffmpeg HTTP capture -> indexing -> query -> evidence bundle.

Finite synthetic MP4 over loopback is a transport fixture, not a live camera.
The deterministic blue detector and HSV encoder only test pipeline behavior.
"""
import argparse
import functools
import hashlib
import io
import json
import shutil
import sys
import threading
import time
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import av
from PIL import Image

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from tracking3.capture import CaptureManager
from tracking3.engine import Encoder, Indexer
from tracking3.media import read_frame
from tracking3.server import make_handler
from tracking3.store import Store, fingerprint
from tools.run_synthetic_workflow import blue_body_detector


class QuietSource(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--source', type=Path,
                        default=APP.parent/'数据准备/synthetic-two-camera-v1/demo_cam_a.mp4')
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('输出目录已存在，请指定新目录，保留历史证据')
    source = args.source.resolve()
    if not source.is_file():
        raise SystemExit('源录像不存在')
    # Bind before creating output, so a sandbox port rejection leaves no partial run.
    source_server = ThreadingHTTPServer(('127.0.0.1', 0),
        functools.partial(QuietSource, directory=str(source.parent)))
    api_server = None
    store = None
    manager = CaptureManager()
    threads = []
    results = []
    try:
        args.output.mkdir(parents=True, exist_ok=False)
        store = Store(args.output/'data')
        encoder = Encoder()
        indexer = Indexer(store, encoder, 'unused-controlled-detector.pt')
        api_server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(store, encoder, indexer, manager))
        for server in (source_server, api_server):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
        base = f'http://127.0.0.1:{api_server.server_port}'
        url = f'http://127.0.0.1:{source_server.server_port}/{source.name}'

        def call(route, data=None):
            request = Request(base+route, data=json.dumps(data).encode() if data else None,
                              headers={'Content-Type':'application/json'})
            with urlopen(request, timeout=15) as response:
                return json.load(response)

        def capture(camera, mode, address):
            job = call('/api/capture-start', dict(url=address, camera_id=camera,
                       duration=4, codec=mode, location='本地HTTP合成采集回归'))
            initial = dict(job)
            deadline = time.monotonic()+30
            while time.monotonic() < deadline:
                job = next(j for j in call('/api/captures') if j['id']==job['id'])
                if job['status'] in ('complete','failed'):
                    break
                time.sleep(.1)
            assert job['status'] in ('complete','failed'), job
            sidecar = json.loads(store.asset(job['sidecar']).read_text())
            assert sidecar['status'] == job['status']
            assert sidecar['media_id'] == job['media_id']
            assert sidecar['finished_at'] >= sidecar['started_at']
            results.append({'initial':initial, 'final':job, 'sidecar':sidecar})
            return job

        for mode in ('copy','h264'):
            job = capture('http_'+mode, mode, url)
            assert job['status']=='complete', job
            media = store.get('media', job['media_id'])
            assert media['status']=='unprocessed'
            assert media['fingerprint']==fingerprint(store.asset(media['path']))
            assert media['capture_id']==job['id'] and media['capture_sidecar']==job['sidecar']
            assert media['start_epoch'] is None and media['uncertainty'] is None
            assert 3.5 <= media['duration'] <= 4.5, media
            with av.open(str(store.asset(media['path']))) as container:
                decoded = sum(1 for _ in container.decode(video=0))
            assert decoded > 0
            indexer.process(media['id'], 3, detector=blue_body_detector)
            indexed = store.get('media', media['id'])
            assert indexed['status']=='complete'
            results[-1].update(media=indexed, decoded_frames=decoded)

        failed = capture('http_missing', 'h264', url+'/missing')
        assert failed['status']=='failed' and failed['media_id'] is None
        assert not (store.root/failed['path']).exists()
        assert len(store.list('media'))==2
        media = store.list('media')[0]
        rgb, _ = read_frame(store.asset(media['path']), 1.5)
        box = blue_body_detector(rgb)[0][0]
        Image.fromarray(rgb[box[1]:box[3],box[0]:box[2]]).save(store.root/'uploads/query.png')
        task = call('/api/task', dict(entry='photo', path='uploads/query.png', name='HTTP采集受控回归'))
        candidates = call('/api/search?task='+task['id'])
        assert {'http_copy','http_h264'} <= {o['camera_id'] for o in candidates}
        bundle = call('/api/report-bundle', dict(task_id=task['id'], include_media=True))
        package = store.asset(bundle['path'])
        with zipfile.ZipFile(package) as archive:
            assert archive.testzip() is None
            manifest = json.loads(archive.read('manifest.json'))
            snapshot = json.loads(archive.read('report-snapshot.json'))
            assert manifest['task_version']==task['version']==snapshot['task']['version']
            for m in manifest['media']:
                video_path = m['included_in_bundle']
                assert video_path
                assert hashlib.sha256(archive.read(video_path)).hexdigest()==m['fingerprint']
            for original in store.list('media'):
                member = 'capture/'+original['id']+'.json'
                assert archive.read(member)==store.asset(original['capture_sidecar']).read_bytes()
                assert hashlib.sha256(archive.read(member)).hexdigest()==snapshot['sidecar_digests'][original['capture_sidecar']]
            for crop in manifest['included_crops']:
                with Image.open(io.BytesIO(archive.read(crop))) as image:
                    image.verify()
        shutil.copy2(package, args.output/'evidence.zip')
        (args.output/'result.json').write_text(json.dumps(dict(
            fixture='finite synthetic MP4 via HTTP; blue detector + HSV',
            source_sha256=fingerprint(source), captures=results,
            task=task, candidate_count=len(candidates), bundle=bundle,
            bundle_sha256=fingerprint(package), sidecar_and_video_sha_verified=True,
            no_camera_time_invented=True), ensure_ascii=False, indent=2)+'\n')
        print(json.dumps(dict(success=True, captures=2, failed_capture=1,
            candidates=len(candidates), bundle=str(package)), ensure_ascii=False))
    finally:
        manager.stop_all()
        for thread in list(manager.threads.values()):
            thread.join(timeout=5)
        for server in (api_server, source_server):
            if server:
                if threads:
                    server.shutdown()
                server.server_close()
        for thread in threads:
            thread.join(timeout=5)
        if store:
            store.db.close()


if __name__=='__main__':
    main()
