"""Serve an existing isolated UI fixture and local continuous MJPEG source.

Use prepare_connection_demo.py with a new output directory first. This is not
a recognition benchmark or a physical camera simulator with capture timestamps.
"""
import argparse
import io
import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import av

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from tracking3.capture import CaptureManager
from tracking3.engine import Encoder, Indexer
from tracking3.server import make_handler
from tracking3.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', required=True, type=Path)
    parser.add_argument('--port', type=int, default=8769)
    args = parser.parse_args()
    if not (args.data/'tracking.sqlite3').is_file():
        parser.error('须使用已准备好的独立UI测试数据目录')
    frames = []
    with av.open(str(args.data/'uploads/ui.mp4')) as container:
        for frame in container.decode(video=0):
            blob = io.BytesIO()
            frame.to_image().save(blob, format='JPEG')
            frames.append(blob.getvalue())
    assert frames
    stopped = threading.Event()
    class Stream(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
            self.end_headers()
            i = 0
            try:
                while not stopped.is_set():
                    blob = frames[i % len(frames)]
                    self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(blob)).encode()+b'\r\n\r\n'+blob+b'\r\n')
                    self.wfile.flush()
                    i += 1
                    stopped.wait(.05)
            except (BrokenPipeError, ConnectionResetError):
                pass
    source = ThreadingHTTPServer(('127.0.0.1', 0), Stream)
    store = Store(args.data)
    manager = CaptureManager()
    encoder = Encoder()
    indexer = Indexer(store, encoder, 'unused-UI-controlled-fixture.pt')
    server = None
    try:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(store, encoder, indexer, manager))
        for item in (source, server):
            threading.Thread(target=item.serve_forever, daemon=True).start()
        runtime = dict(pid=os.getpid(), workbench=f'http://127.0.0.1:{server.server_port}/',
                       mjpeg_source=f'http://127.0.0.1:{source.server_port}/stream',
                       scope='isolated synthetic UI fixture, not real identity GT')
        (args.data.parent/'runtime.json').write_text(json.dumps(runtime, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps(runtime, ensure_ascii=False), flush=True)
        def finish(*_): stopped.set()
        signal.signal(signal.SIGINT, finish)
        signal.signal(signal.SIGTERM, finish)
        stopped.wait()
    finally:
        stopped.set()
        for job in store.list('capture'):
            if job.get('status') == 'recording': manager.stop(store, job['id'])
        for thread in list(manager.threads.values()): thread.join(timeout=5)
        manager.stop_all()
        for item in (server, source):
            if item:
                if server: item.shutdown()
                item.server_close()
        store.db.close()


if __name__ == '__main__': main()
