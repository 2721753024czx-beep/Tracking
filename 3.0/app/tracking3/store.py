import hashlib
import json
import sqlite3
import re
import threading
import time
import uuid
from pathlib import Path


def uid():
    return uuid.uuid4().hex


class Store:
    """One small local DB; serialized writes, transactionally persisted decisions."""

    @classmethod
    def open_readonly(cls, root):
        """Read existing metadata without startup recovery or schema mutations.

        Report files can still be written under reports; database writes fail.
        """
        store = cls.__new__(cls)
        store.root = Path(root).resolve()
        store.lock = threading.RLock()
        store.db = sqlite3.connect((store.root / 'tracking.sqlite3').as_uri() + '?mode=ro',
                                   uri=True, check_same_thread=False)
        store.db.row_factory = sqlite3.Row
        return store

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ('uploads', 'crops', 'reports'):
            (self.root / name).mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / 'tracking.sqlite3', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS documents (
          kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
          PRIMARY KEY(kind,id));
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY, at REAL, task_id TEXT, action TEXT, body TEXT);
        ''')
        # Interrupted jobs restart at a file boundary. Never report them as complete.
        for item in self.list('media'):
            if item.get('status') == 'processing':
                item['status'] = 'paused'
                item['error'] = '上次处理中断；恢复时重新处理该文件。'
                self.put('media', item)

    def write(self, kind, item):
        """Write within a caller-owned lock and transaction; never commit early."""
        self.db.execute('INSERT OR REPLACE INTO documents VALUES (?,?,?)',
                        (kind, item['id'], json.dumps(item, ensure_ascii=False)))

    def audit(self, task_id, action, data):
        self.db.execute('INSERT INTO audit(at,task_id,action,body) VALUES(?,?,?,?)',
                        (time.time(), task_id, action, json.dumps(data, ensure_ascii=False)))

    def put(self, kind, item):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO documents VALUES (?,?,?)',
                            (kind, item['id'], json.dumps(item, ensure_ascii=False)))
        return item

    def get(self, kind, ident):
        with self.lock:
            row = self.db.execute('SELECT body FROM documents WHERE kind=? AND id=?',
                                  (kind, ident)).fetchone()
        if not row:
            raise ValueError('记录不存在')
        return json.loads(row[0])

    def batch(self, entries):
        with self.lock, self.db:
            self.db.executemany('INSERT OR REPLACE INTO documents VALUES (?,?,?)',
                [(kind, item['id'], json.dumps(item, ensure_ascii=False)) for kind, item in entries])

    def list(self, kind):
        with self.lock:
            rows = self.db.execute('SELECT body FROM documents WHERE kind=? ORDER BY rowid',
                                   (kind,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def observation_owner(self, observation):
        """Recover old query-seed ownership without rewriting historical records."""
        if observation.get('owner_task'):
            return observation['owner_task']
        if observation.get('source') == 'manual':
            match = re.fullmatch(r'crops/query-([0-9a-f]{32})\.jpg', observation.get('crop', ''))
            if match:
                return match.group(1)
        return None

    def remove_where(self, kind, field, value):
        with self.lock, self.db:
            for obj in self.list(kind):
                if obj.get(field) == value:
                    self.db.execute('DELETE FROM documents WHERE kind=? AND id=?', (kind, obj['id']))

    def asset(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError('文件不存在或路径无效')
        return path

    def log(self, task_id, action, data):
        with self.lock, self.db:
            self.db.execute('INSERT INTO audit(at,task_id,action,body) VALUES(?,?,?,?)',
                            (time.time(), task_id, action, json.dumps(data, ensure_ascii=False)))

    def decisions(self, task_id):
        return [d for d in self.list('decision') if d['task_id'] == task_id]

    def decide(self, task_id, observation_id, status, expected_task_version=None):
        if status not in ('pending', 'confirmed', 'rejected'):
            raise ValueError('无效核对状态')
        from .connections import for_task, invalidate
        key = f'{task_id}:{observation_id}'
        with self.lock, self.db:
            task = self.get('task', task_id)
            observation = self.get('observation', observation_id)
            if self.observation_owner(observation) not in (None, task_id):
                raise ValueError('人工片段不属于此任务')
            if status == 'confirmed' and observation_id in task.get('superseded_observations', []):
                raise ValueError('该观察已有纠正版，请确认新片段')
            if status == 'confirmed':
                media = self.get('media', observation['media_id'])
                if media.get('path'):
                    from .integrity import require
                    require(self, media)
            if expected_task_version is not None and expected_task_version != task['version']:
                raise ValueError('任务版本已变化，请刷新影响范围后再操作')
            self.get('observation', observation_id)
            from .tasks import require_active
            require_active(task)
            try:
                old = self.get('decision', key)['status']
            except ValueError:
                old = 'pending'
            self.write('decision', dict(id=key, task_id=task_id, observation_id=observation_id,
                                      status=status, at=time.time(), operator='local'))
            if observation_id in task.get('reference_observations', []) and status != 'confirmed':
                task['reference_review_required'] = True
            roots = [c['id'] for c in for_task(self, task_id)
                     if observation_id in (c['from_observation'], c['to_observation'])]
            changed = invalidate(self, task_id, roots, '观察身份已修改，需重新核对连接') if old != status else []
            task['version'] += 1
            self.write('task', task)
            self.audit(task_id, 'decision', {'observation_id': observation_id, 'old': old,
                                            'new': status, 'affected': changed})
            return changed


def fingerprint(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()
