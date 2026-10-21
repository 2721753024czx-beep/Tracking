import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tracking3.evaluation_export import export
from tracking3.engine import search
from tracking3.store import Store, fingerprint


class EvaluationExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/'data')
        self.output = Path(self.tmp.name)/'export'
        for name in ('query.jpg','obs.jpg'):
            (self.store.root/'crops'/name).write_bytes(name.encode())
        (self.store.root/'uploads/a.mp4').write_bytes(b'controlled-source')
        self.media = dict(id='m',path='uploads/a.mp4',camera_id='a',location='fixture',
                          fingerprint=fingerprint(self.store.root/'uploads/a.mp4'),
                          status='complete',duration=120,sample_fps=3)
        self.store.put('media',self.media)
        self.task = dict(id='t',version=1,entry='photo',crop='crops/query.jpg',
                         encoder='cached-fixture',feature=[1.,0.],status='paused',cameras=[])
        self.store.put('task',self.task)
        for i in range(120):
            self.store.put('observation',dict(id=f'o{i:03}',media_id='m',camera_id='a',
                encoder='cached-fixture',feature=[1.,0.],start=float(i),end=float(i),
                crop='crops/obs.jpg',representative=float(i),box=[0,0,1,1],source='detected'))

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def rows(self):
        return (self.store.db.execute('SELECT * FROM documents ORDER BY kind,id').fetchall(),
                self.store.db.execute('SELECT * FROM audit ORDER BY id').fetchall())

    def test_default_all_and_limit_are_explicit_and_database_unchanged(self):
        before = [list(map(tuple,r)) for r in self.rows()]
        result = export(self.store.root,'t',self.output,expected_task_version=1)
        self.assertEqual(result['candidates'],120)
        self.assertFalse(result['truncated'])
        snapshot = json.loads((self.output/'snapshot.json').read_text())
        self.assertNotIn('feature',snapshot['task'])
        self.assertFalse(snapshot['identity_reviewed'])
        candidates = json.loads((self.output/'candidates.json').read_text())
        self.assertTrue(all(c['source_path']=='uploads/a.mp4' for c in candidates))
        artifact = json.loads((self.output/'artifact.json').read_text())
        for name,digest in artifact['files'].items():
            self.assertEqual(fingerprint(self.output/name),digest)
        self.assertEqual([list(map(tuple,r)) for r in self.rows()],before)
        limited = export(self.store.root,'t',Path(self.tmp.name)/'limited',limit=7)
        self.assertEqual(limited['candidates'],7)
        self.assertEqual(limited['scope_candidates'],120)
        self.assertTrue(limited['truncated'])

    def test_expected_version_and_existing_output_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'任务版本'):
            export(self.store.root,'t',self.output,expected_task_version=2)
        self.assertFalse(self.output.exists())
        self.output.mkdir()
        (self.output/'keep').write_text('untouched')
        with self.assertRaisesRegex(ValueError,'已存在'):
            export(self.store.root,'t',self.output)
        self.assertEqual((self.output/'keep').read_text(),'untouched')

    def test_concurrent_task_and_index_change_do_not_mix_sqlite_versions(self):
        def writer_during_search(store,encoder,ident,limit):
            # The writable connection commits in WAL while export owns a read
            # transaction. Its subsequent queries must still see old rows.
            task = self.store.get('task','t')
            task.update(version=2,cameras=['other'])
            self.store.put('task',task)
            obs = self.store.get('observation','o000')
            obs['feature'] = [0.,1.]
            self.store.put('observation',obs)
            return search(store,encoder,ident,limit)
        with patch('tracking3.evaluation_export.search',side_effect=writer_during_search):
            result = export(self.store.root,'t',self.output)
        self.assertEqual(self.store.get('task','t')['version'],2)
        self.assertEqual(result['task_version'],1)
        candidates = json.loads((self.output/'candidates.json').read_text())
        self.assertEqual(len(candidates),120)
        self.assertEqual(candidates[0]['id'],'o000')
        self.assertEqual(candidates[0]['score'],1)
        self.assertEqual(json.loads((self.output/'snapshot.json').read_text())['task']['cameras'],[])

    def test_source_changed_during_search_is_rejected_without_output(self):
        def change_source(*args,**kwargs):
            ranked = search(*args,**kwargs)
            (self.store.root/'uploads/a.mp4').write_bytes(b'changed')
            return ranked
        with patch('tracking3.evaluation_export.search',side_effect=change_source):
            with self.assertRaisesRegex(ValueError,'内容已改变'):
                export(self.store.root,'t',self.output)
        self.assertFalse(self.output.exists())

    def test_crop_change_after_files_written_aborts_and_cleans_own_output(self):
        query_checks = 0
        def late_change(path):
            nonlocal query_checks
            if path.name=='query.jpg':
                query_checks += 1
                if query_checks==2:
                    path.write_bytes(b'changed-late')
            return fingerprint(path)
        with patch('tracking3.evaluation_export.fingerprint',side_effect=late_change):
            with self.assertRaisesRegex(ValueError,'截图在导出期间改变'):
                export(self.store.root,'t',self.output)
        self.assertFalse(self.output.exists())

    def test_processing_is_not_recovered_or_mutated_by_readonly_export(self):
        self.media['status']='processing'
        self.store.put('media',self.media)
        before = [list(map(tuple,r)) for r in self.rows()]
        with self.assertRaisesRegex(ValueError,'正在处理'):
            export(self.store.root,'t',self.output)
        self.assertEqual(self.store.get('media','m')['status'],'processing')
        self.assertEqual([list(map(tuple,r)) for r in self.rows()],before)
        self.assertFalse(self.output.exists())

    def test_other_tasks_query_seed_is_not_exported(self):
        ident='b'*32
        (self.store.root/f'crops/query-{ident}.jpg').write_bytes(b'other-query')
        other = self.store.get('observation','o000')
        other.update(id='foreign-seed',source='manual',crop=f'crops/query-{ident}.jpg')
        self.store.put('observation',other)
        result = export(self.store.root,'t',self.output)
        self.assertEqual(result['candidates'],120)
        self.assertNotIn('foreign-seed',{c['id'] for c in json.loads((self.output/'candidates.json').read_text())})
