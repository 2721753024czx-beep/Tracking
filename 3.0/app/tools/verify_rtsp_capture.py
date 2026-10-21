"""Two real loopback RTSP/TCP streams through the production capture API.

MediaMTX and FFmpeg are controlled sources, not physical Wi-Fi cameras.
The blue detector + HSV only verify the pipeline, never model accuracy.
All temporary servers/processes are owned and closed by this script.
"""
import argparse
import hashlib
import io
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import av
from PIL import Image

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from tracking3.capture import CaptureManager, command, validate
from tracking3.engine import Encoder, Indexer
from tracking3.server import make_handler
from tracking3.store import Store, fingerprint
from tools.run_synthetic_workflow import blue_body_detector


def wait_for(predicate, description, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.1)
    raise AssertionError('Timed out: ' + description)


def decoded_info(path):
    with av.open(str(path)) as container:
        frames = list(container.decode(video=0))
        seconds = [float(f.pts * f.time_base) for f in frames]
        codec = container.streams.video[0].codec_context.name
    assert frames and all(b > a for a, b in zip(seconds, seconds[1:])), seconds
    return dict(frames=len(frames), first_pts_s=seconds[0], last_pts_s=seconds[-1],
                normalized_first_s=0, normalized_last_s=seconds[-1]-seconds[0],
                strictly_increasing_pts=True, codec=codec)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--mediamtx', type=Path,
                        default=APP.parent/'工具/mediamtx-v1.21.2/mediamtx')
    args = parser.parse_args()
    output = args.output.resolve()
    binary = args.mediamtx.resolve()
    if output.exists():
        raise SystemExit('输出目录已存在；请用新目录保留历史证据')
    if not binary.is_file():
        raise SystemExit('先准备官方 MediaMTX 可执行文件')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    api = ThreadingHTTPServer(('127.0.0.1', 0), lambda *_: None)
    output.mkdir(parents=True)
    processes, logs = [], []
    manager = CaptureManager()
    store = None
    api_thread = None
    run = dict(scope='controlled loopback RTSP/TCP; blue detector + HSV; not camera/model validation',
               mediamtx_version=subprocess.check_output([str(binary), '--version'], text=True).strip(),
               mediamtx_binary_sha256=fingerprint(binary), rtsp_port=port,
               api_port=api.server_port, captures=[], source_commands=[], cleanup={})
    try:
        config = output/'mediamtx-test.yml'
        config.write_text(f'''logLevel: info
logDestinations: [stdout]
rtsp: true
rtspTransports: [tcp]
rtspAddress: 127.0.0.1:{port}
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

        def spawn(argv, label):
            handle = (output/(label+'.log')).open('w')
            logs.append(handle)
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=handle,
                                    stderr=subprocess.STDOUT)
            processes.append((label, proc))
            return proc

        mtx = spawn([str(binary), str(config)], 'mediamtx')
        def listening():
            if mtx.poll() is not None:
                raise AssertionError('MediaMTX exited: ' + (output/'mediamtx.log').read_text())
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.2):
                    return True
            except OSError:
                return False
        wait_for(listening, 'RTSP listener', 10)
        assert mtx.poll() is None
        for label in ('cam_a', 'cam_b'):
            source = APP.parent/f'数据准备/synthetic-two-camera-v1/demo_{label}.mp4'
            url = f'rtsp://127.0.0.1:{port}/{label}'
            argv = ['ffmpeg','-hide_banner','-loglevel','warning','-nostdin','-re',
                    '-stream_loop','-1','-i',str(source),'-an','-c:v','libx264',
                    '-preset','ultrafast','-tune','zerolatency','-pix_fmt','yuv420p',
                    '-g','10','-bf','0','-f','rtsp','-rtsp_transport','tcp',url]
            proc = spawn(argv, label+'-publisher')
            run['source_commands'].append(dict(camera=label, pid=proc.pid, command=argv,
                                                input_sha256=fingerprint(source)))
        # Probe the actual RTSP streams rather than version-dependent log text.
        run['source_probes'] = []
        for label in ('cam_a','cam_b'):
            def ready():
                response = subprocess.run(['ffprobe','-v','error','-rtsp_transport','tcp',
                    '-analyzeduration','100000','-probesize','200000','-i',
                    f'rtsp://127.0.0.1:{port}/{label}',
                    '-show_entries','stream=codec_name','-of','json'],
                    capture_output=True,text=True,timeout=8)
                if response.returncode:
                    return None
                probe = json.loads(response.stdout)
                return probe if any(s.get('codec_name')=='h264' for s in probe.get('streams',[])) else None
            run['source_probes'].append(dict(camera=label,probe=wait_for(ready,'RTSP '+label,15)))
        assert all(p.poll() is None for _, p in processes)

        store = Store(output/'data')
        encoder = Encoder()
        indexer = Indexer(store, encoder, 'unused-controlled-detector.pt')
        api.RequestHandlerClass = make_handler(store, encoder, indexer, manager)
        api_thread = threading.Thread(target=api.serve_forever, daemon=True)
        api_thread.start()
        base = f'http://127.0.0.1:{api.server_port}'

        def call(route, data=None):
            request = Request(base+route, data=json.dumps(data).encode() if data else None,
                              headers={'Content-Type':'application/json'})
            with urlopen(request, timeout=15) as response:
                return json.load(response)

        configs = [dict(camera_id='rtsp_a_copy', url=f'rtsp://127.0.0.1:{port}/cam_a',
                        duration=8, codec='copy', timestamp_mode='source'),
                   dict(camera_id='rtsp_b_h264_stop', url=f'rtsp://127.0.0.1:{port}/cam_b',
                        duration=120, codec='h264', timestamp_mode='source')]
        jobs = [call('/api/capture-start', dict(c, location='受控双路RTSP模拟')) for c in configs]

        def both_live():
            with manager.lock:
                return len(manager.processes) == 2 and all(
                    p.poll() is None for p in manager.processes.values())

        wait_for(both_live, 'two actual recording processes', 10)
        with manager.lock:
            run['concurrent_process_evidence'] = dict(observed_at=time.time(),
                api_jobs=call('/api/captures'),
                recording_pids={k:p.pid for k,p in manager.processes.items()},
                all_recorders_alive=both_live())
        # B is explicitly stopped well before its 120-second request.
        time.sleep(6)
        stop = call('/api/capture-stop', dict(id=jobs[1]['id']))
        run['stop_response'] = stop

        def settled():
            current = call('/api/captures')
            return current if len(current)==2 and all(j['status'] in ('complete','failed') for j in current) else None

        completed = wait_for(settled, 'recordings finish', 35)
        by_id = {j['id']:j for j in completed}
        for initial, cfg in zip(jobs, configs):
            job = by_id[initial['id']]
            assert job['status']=='complete', job
            sidecar = json.loads(store.asset(job['sidecar']).read_text())
            assert sidecar['status']=='complete' and sidecar['media_id']==job['media_id']
            assert sidecar['timestamp_mode']=='source'
            media = store.get('media', job['media_id'])
            assert media['start_epoch'] is None and media['uncertainty'] is None
            assert media['fingerprint']==fingerprint(store.asset(media['path']))
            info = decoded_info(store.asset(media['path']))
            assert info['codec']=='h264'
            item = dict(initial=initial, final=job, sidecar=sidecar, media=media, decode=info,
                        receiver_command=command(validate(cfg), store.asset(job['path'])))
            run['captures'].append(item)
            if cfg['duration']==120:
                assert job['finish_reason']=='user_stopped' and 0 < media['duration'] < 120
            else:
                # Joining RTSP may miss frames before the next keyframe. The
                # file's raw start can be positive; don't invent a full 8s.
                assert job['finish_reason']=='duration_or_source_end', job
                assert 0 < media['duration'] <= 8.1, media
                assert 7.9 <= info['first_pts_s'] + media['duration'] <= 8.1, (media,info)
            indexer.process(media['id'], 3, detector=blue_body_detector)
            item['media'] = store.get('media',media['id'])

        missing = call('/api/capture-start', dict(camera_id='rtsp_missing',
            url=f'rtsp://127.0.0.1:{port}/missing', codec='copy', duration=2))
        def failed_job():
            j = next(j for j in call('/api/captures') if j['id']==missing['id'])
            return j if j['status'] in ('failed','complete') else None
        missing = wait_for(failed_job, 'unknown RTSP path', 15)
        assert missing['status']=='failed' and missing['media_id'] is None
        assert not (store.root/missing['path']).exists() and len(store.list('media'))==2
        run['missing_path'] = missing
        run['missing_path_sidecar'] = json.loads(store.asset(missing['sidecar']).read_text())

        query_media = run['captures'][0]['media']
        with av.open(str(store.asset(query_media['path']))) as container:
            origin = None
            for frame in container.decode(video=0):
                raw_pts = float(frame.pts * frame.time_base)
                if origin is None:
                    origin = raw_pts
                rgb = frame.to_ndarray(format='rgb24')
                found = blue_body_detector(rgb)
                if found:
                    box = found[0][0]
                    query_s = raw_pts-origin
                    break
            else:
                raise AssertionError('No blue fixture in captured A')
        Image.fromarray(rgb[box[1]:box[3],box[0]:box[2]]).save(store.root/'uploads/query.png')
        task = call('/api/task', dict(entry='photo', path='uploads/query.png', name='双路RTSP录制回溯'))
        candidates = call('/api/search?task='+task['id'])
        assert {'rtsp_a_copy','rtsp_b_h264_stop'} <= {o['camera_id'] for o in candidates}, candidates
        bundle = call('/api/report-bundle', dict(task_id=task['id'], include_media=True))
        package = store.asset(bundle['path'])
        with zipfile.ZipFile(package) as archive:
            assert archive.testzip() is None
            manifest = json.loads(archive.read('manifest.json'))
            snapshot = json.loads(archive.read('report-snapshot.json'))
            assert len(archive.namelist())==len(set(archive.namelist()))
            assert manifest['task_version']==task['version']==snapshot['task']['version']
            assert len(manifest['media'])==2
            for media in manifest['media']:
                member = media['included_in_bundle']
                assert member and hashlib.sha256(archive.read(member)).hexdigest()==media['fingerprint']
                sidecar = 'capture/'+media['id']+'.json'
                assert archive.read(sidecar)==store.asset(media['capture_sidecar']).read_bytes()
                assert hashlib.sha256(archive.read(sidecar)).hexdigest()==snapshot['sidecar_digests'][media['capture_sidecar']]
            for crop in manifest['included_crops']:
                with Image.open(io.BytesIO(archive.read(crop))) as img:
                    img.verify()
        shutil.copy2(package, output/'evidence.zip')
        run.update(success=True, task=task, query_source_s=query_s,
                   query_raw_pts_s=raw_pts, query_origin_s=origin, query_box=box,
                   candidates=candidates, bundle=bundle, bundle_sha256=fingerprint(package),
                   no_camera_time_invented=True, sidecar_and_video_sha_verified=True)
        print(json.dumps(dict(success=True, completed=2, missing_path_failed=True,
                             candidates=len(candidates)),ensure_ascii=False))
    except BaseException as exc:
        run['exception'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        manager.stop_all()
        for thread in list(manager.threads.values()):
            thread.join(timeout=10)
        if api_thread:
            api.shutdown()
            api_thread.join(timeout=5)
        api.server_close()
        for label, proc in reversed(processes):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            run['cleanup'][label] = dict(pid=proc.pid, returncode=proc.returncode, exited=proc.poll() is not None)
        for handle in logs:
            handle.close()
        if store:
            store.db.close()
        run['cleanup']['api_closed'] = True
        run['cleanup']['capture_threads_remaining'] = len(manager.threads)
        (output/'result.json').write_text(json.dumps(run,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':
    main()
