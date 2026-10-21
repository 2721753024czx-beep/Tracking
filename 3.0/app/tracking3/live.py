"""Bounded, memory-only RTSP previews shared by local browser viewers."""
import re
import subprocess
import threading
import time
from urllib.parse import quote, urlsplit, urlunsplit

from .capture import redact_url

MAX_JPEG = 4 * 1024 * 1024


def camera_key(value):
    value = str(value or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value):
        raise ValueError('摄像头编号限1–64位字母、数字、下划线或短横线')
    return value


def source_url(value):
    try:
        parsed = urlsplit(str(value).strip())
        if parsed.scheme.lower() != 'rtsp' or not parsed.hostname or not 1 <= (parsed.port or 554) <= 65535:
            raise ValueError()
        if parsed.username is not None or parsed.password is not None:
            raise ValueError()
        if parsed.fragment or any(c.isspace() for c in str(value)):
            raise ValueError()
    except ValueError:
        raise ValueError('请输入不带账号密码的RTSP地址，账号密码在单独字段填写') from None
    return urlunsplit(parsed)


def configuration(data):
    preview = source_url(data.get('preview_url', ''))
    record = source_url(data.get('record_url', ''))
    p, r = urlsplit(preview), urlsplit(record)
    if (p.hostname, p.port or 554) != (r.hostname, r.port or 554):
        raise ValueError('预览与录制地址须来自同一台摄像头和端口')
    username = str(data.get('username', ''))
    password = str(data.get('password', ''))
    if len(username) > 100 or len(password) > 512 or any(c in username + password for c in '\r\n\x00'):
        raise ValueError('设备账号或密码格式无效')
    details = camera_metadata(data)

    def authenticated(url):
        parsed = urlsplit(url)
        auth = f'{quote(username, safe="")}:{quote(password, safe="")}@' if username or password else ''
        return urlunsplit(parsed._replace(netloc=auth + parsed.netloc))

    return dict(camera_id=camera_key(data.get('camera_id')), **details,
                preview_url=preview, record_url=record, username=username, password=password,
                preview_input=authenticated(preview), record_input=authenticated(record))


def camera_metadata(data):
    values = {}
    for key, limit in (('label', 100), ('location', 200)):
        value = data.get(key, '')
        if not isinstance(value, str) or len(value.strip()) > limit or '\x00' in value:
            raise ValueError(f'{"显示名称" if key == "label" else "相对位置"}格式无效或过长')
        values[key] = value.strip()
    return values


def clean_error(message, config):
    result = str(message)
    for key in ('preview_input', 'record_input'):
        result = result.replace(config[key], redact_url(config[key]))
    if config['password']:
        for secret in (config['password'], quote(config['password'], safe='')):
            result = result.replace(secret, '***')
    result = re.sub(r'rtsp://[^\s/]*@', 'rtsp://***:***@', result)
    return result[-1200:]


def preview_command(config):
    return ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-rtsp_transport', 'tcp', '-timeout', '8000000', '-threads', '1',
            '-i', config['preview_input'], '-map', '0:v:0', '-an',
            '-vf', "fps=10,scale='min(960,iw)':-2", '-c:v', 'mjpeg',
            '-q:v', '5', '-threads', '1', '-f', 'image2pipe', 'pipe:1']


def jpeg_frames(chunks):
    """Parse image2pipe output without allowing a damaged stream to grow memory."""
    buffer = bytearray()
    for chunk in chunks:
        buffer.extend(chunk)
        while buffer:
            start = buffer.find(b'\xff\xd8')
            if start < 0:
                buffer[:] = buffer[-1:]
                break
            if start:
                del buffer[:start]
            end = buffer.find(b'\xff\xd9', 2)
            if end < 0:
                if len(buffer) > MAX_JPEG:
                    raise ValueError('预览帧超出大小限制')
                break
            if end + 2 > MAX_JPEG:
                raise ValueError('预览帧超出大小限制')
            yield bytes(buffer[:end + 2])
            del buffer[:end + 2]


class Preview:
    def __init__(self, config, popen=subprocess.Popen):
        self.config = config
        self.popen = popen
        self.condition = threading.Condition(threading.RLock())
        self.cancelled = threading.Event()
        self.process = None
        self.frame = None
        self.frames = 0
        self.started_at = time.time()
        self.last_frame_at = None
        self.last_touch = time.monotonic()
        self.watchers = 0
        self.status = 'connecting'
        self.error = None
        self.camera_id = config['camera_id']
        self.label = config['label']
        self.location = config['location']
        self.preview_url = config['preview_url']
        self.record_url = config['record_url']
        self.thread = threading.Thread(target=self._run, daemon=True, name=f'preview-{self.camera_id}')
        self.thread.start()
        self.watchdog = threading.Thread(target=self._watch, daemon=True)
        self.watchdog.start()

    def public(self, touch=False):
        with self.condition:
            if touch:
                self.last_touch = time.monotonic()
            age = None if self.last_frame_at is None else max(0, time.time() - self.last_frame_at)
            status = 'stale' if self.status == 'live' and age is not None and age > 5 else self.status
            return dict(camera_id=self.camera_id, label=self.label, location=self.location,
                        preview_url=self.preview_url, record_url=self.record_url,
                        status=status, frames=self.frames, started_at=self.started_at,
                        last_frame_at=self.last_frame_at, frame_age=age, error=self.error)

    def wait_frame(self, previous=0, timeout=12):
        with self.condition:
            self.last_touch = time.monotonic()
            self.condition.wait_for(lambda: self.frames > previous or self.status in ('failed', 'stopped'), timeout)
            if self.status in ('failed', 'stopped'):
                raise ValueError(self.error or '预览已断开')
            if self.frames <= previous or self.frame is None:
                raise ValueError('等待摄像头画面超时，请检查设备和账号密码')
            return self.frames, self.frame

    def stop(self):
        self.cancelled.set()
        with self.condition:
            process = self.process
            self.status = 'stopped'
            self.condition.notify_all()
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            except ProcessLookupError:
                pass
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=4)

    def _watch(self):
        while not self.cancelled.wait(2):
            with self.condition:
                done = self.status in ('failed', 'stopped')
                idle = time.monotonic() - self.last_touch > 90 and self.watchers == 0
            if done:
                return
            if idle:
                self.stop()
                return

    def _run(self):
        config = self.config  # Only this worker and the live manager hold credentials.
        errors = bytearray()
        error_thread = None
        process = None
        try:
            process = self.popen(preview_command(config), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            with self.condition:
                self.process = process
                if self.cancelled.is_set():
                    process.terminate()

            def collect_errors():
                while True:
                    part = process.stderr.read1(4096)
                    if not part:
                        break
                    errors.extend(part)
                    if len(errors) > 8192:
                        del errors[:-8192]
            error_thread = threading.Thread(target=collect_errors, daemon=True)
            error_thread.start()

            def chunks():
                while not self.cancelled.is_set():
                    part = process.stdout.read1(65536)
                    if not part:
                        break
                    yield part
            for frame in jpeg_frames(chunks()):
                with self.condition:
                    if self.cancelled.is_set():
                        break
                    self.frame = frame
                    self.frames += 1
                    self.last_frame_at = time.time()
                    self.status = 'live'
                    self.condition.notify_all()
            if self.cancelled.is_set():
                return
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=3)
            error_thread.join(timeout=2)
            detail = clean_error(errors.decode('utf-8', errors='replace'), config).strip()
            raise ValueError(detail or '摄像头预览连接已结束，请重新连接')
        except Exception as exc:
            with self.condition:
                if not self.cancelled.is_set():
                    self.status = 'failed'
                    self.error = clean_error(str(exc), config)
                    self.condition.notify_all()
        finally:
            if process and process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            if error_thread:
                error_thread.join(timeout=2)
            if process:
                process.stdout.close()
                process.stderr.close()
            with self.condition:
                self.config = None
                self.process = None
                self.condition.notify_all()


class LiveManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.sessions = {}

    def get(self, camera_id):
        with self.lock:
            try:
                return self.sessions[camera_key(camera_id)]
            except KeyError:
                raise ValueError('请先连接这台摄像头的预览') from None

    def list(self, touch=False):
        with self.lock:
            return [session.public(touch) for session in self.sessions.values()]

    def metadata(self, store):
        return store.list('live_camera')

    def update_metadata(self, store, data):
        ident = camera_key(data.get('camera_id'))
        details = camera_metadata(data)
        with self.lock:
            # Persist only this explicit whitelist, never connection credentials.
            saved = store.put('live_camera', dict(id=ident, **details, updated_at=time.time()))
            session = self.sessions.get(ident)
            if session:
                with session.condition:
                    session.label, session.location = details['label'], details['location']
                    if session.config is not None:
                        session.config.update(details)
            return saved

    def start(self, data, store):
        with self.lock:
            ident = camera_key(data.get('camera_id'))
            saved = next((item for item in self.metadata(store) if item['id'] == ident), {})
            config = configuration({**saved, **data})
            old = self.sessions.get(config['camera_id'])
            if old and old.public()['status'] not in ('failed', 'stopped'):
                raise ValueError('此摄像头已有预览，请先断开后再修改连接')
            active = [s for s in self.sessions.values() if s.public()['status'] not in ('failed', 'stopped')]
            if len(active) >= 4:
                raise ValueError('当前最多同时预览4台摄像头')
            for job in store.list('capture'):
                if job.get('camera_id') == config['camera_id'] and job.get('status') in ('recording', 'stopping'):
                    if job.get('url_redacted') != redact_url(config['record_input']):
                        raise ValueError('此编号正在录制其他来源，请先停止录制再修改地址')
            self.update_metadata(store, config)
            session = Preview(config)
            # Keep stopped history bounded too; credentials are never retained in it.
            if config['camera_id'] not in self.sessions and len(self.sessions) >= 16:
                for key, previous in list(self.sessions.items()):
                    if previous.public()['status'] in ('failed', 'stopped'):
                        del self.sessions[key]
                        break
            self.sessions[config['camera_id']] = session
            return session.public()

    def stop(self, camera_id):
        session = self.get(camera_id)
        session.stop()
        return session.public()

    def record(self, store, capture, data):
        session = self.get(data.get('camera_id'))
        with session.condition:
            if session.status != 'live' or session.config is None or time.time() - session.last_frame_at > 8:
                raise ValueError('请先确认实时预览正常，再开始录制')
            config = dict(session.config)
        return capture.start(store, dict(url=config['record_input'], camera_id=config['camera_id'],
                                         location=config['location'] or config['label'],
                                         duration=data.get('duration', 600), codec=data.get('codec', 'h264'),
                                         timestamp_mode='source'))

    def stop_all(self):
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            session.stop()
