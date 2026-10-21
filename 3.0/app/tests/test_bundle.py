import json
import re
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote
from unittest.mock import patch

from tracking3.bundle import create
from tracking3.store import Store, fingerprint
from tracking3.snapshots import save as save_snapshot, sidecar_path
from tracking3.integrity import check


class EvidenceBundle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        (self.store.root / "uploads").mkdir(exist_ok=True)
        (self.store.root / "crops").mkdir(exist_ok=True)
        (self.store.root / "uploads/a.mp4").write_bytes(b"video")
        (self.store.root / "crops/a.jpg").write_bytes(b"crop")
        media = {"id": "m", "path": "uploads/a.mp4", "name": "Camera A.mp4",
                 "camera_id": "A", "location": "lab", "status": "complete",
                 "fingerprint": fingerprint(self.store.root / "uploads/a.mp4"),
                 "capture_sidecar": "captures/c.json",
                 "capture_time_basis": "receiver-only"}
        self.store.put("media", media)
        (self.store.root / "captures").mkdir(exist_ok=True)
        (self.store.root / "captures/c.json").write_text('{"url_redacted":"rtsp://camera/live"}', encoding="utf-8")
        self.store.put("task", {"id": "t", "name": "bundle", "version": 3,
                                 "status": "active", "entry": "photo", "crop": "crops/a.jpg"})
        self.store.put("observation", {"id": "o", "media_id": "m", "crop": "crops/a.jpg",
                                       "owner_task": "t", "start": 0, "end": 1, "representative": 0})
        self.store.put("decision", {"id": "t:o", "task_id": "t", "observation_id": "o", "status": "confirmed"})
        (self.store.root / "reports").mkdir(exist_ok=True)
        self.write_report("[截图](../crops/a.jpg) [源录像](../uploads/a.mp4)\n")

    def write_report(self, body, legacy=False):
        task = self.store.get("task", "t")
        text = (f"# 回溯报告：{task['name']}\n\n任务：t；结果版本：{task['version']}\n\n"
                "## 导出时的查询条件\n\n```json\n" + json.dumps(task, ensure_ascii=False) + "\n```\n\n" + body)
        self.report_path = f"reports/source-{time.time_ns()}.md"
        if legacy:
            (self.store.root / self.report_path).write_text(text, encoding="utf-8")
        else:
            save_snapshot(self.store, self.report_path, text, task,
                          {m['id']: check(self.store, m) for m in self.store.list('media')})
        return self.store.asset(self.report_path)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_default_bundle_keeps_crops_and_declares_omitted_source(self):
        path = create(self.store, "t", self.report_path)
        with zipfile.ZipFile(self.store.asset(path)) as archive:
            names = set(archive.namelist())
            self.assertIn("report.md", names)
            self.assertIn("evidence/crops/a.jpg", names)
            self.assertIn("capture/m.json", names)
            self.assertNotIn("sources/m-Camera_A.mp4", names)
            self.assertIn("未打包", archive.read("report.md").decode())
            manifest = json.loads(archive.read("manifest.json"))
            self.assertFalse(manifest["include_media_requested"])

    def test_optional_media_bundle_includes_valid_source(self):
        path = create(self.store, "t", self.report_path, include_media=True)
        with zipfile.ZipFile(self.store.asset(path)) as archive:
            self.assertIn("sources/m-Camera_A.mp4", archive.namelist())
            self.assertIn("sources/m-Camera_A.mp4", archive.read("report.md").decode())

    def test_unreviewed_shared_candidate_is_portable_without_changing_decisions(self):
        (self.store.root / "crops/pending.jpg").write_bytes(b"pending-crop")
        self.store.put("observation", {"id": "pending", "media_id": "m",
                                       "crop": "crops/pending.jpg"})
        report = self.write_report("状态 pending；[截图](../crops/pending.jpg)\n")
        original = report.read_text(encoding="utf-8")
        before = self.store.decisions("t")
        path = create(self.store, "t", self.report_path)
        with zipfile.ZipFile(self.store.asset(path)) as archive:
            self.assertEqual(archive.read("evidence/crops/pending.jpg"), b"pending-crop")
            self.assertIn("[截图](evidence/crops/pending.jpg)", archive.read("report.md").decode())
            self.assertEqual(archive.namelist().count("evidence/crops/pending.jpg"), 1)
        self.assertEqual(before, self.store.decisions("t"))
        self.assertEqual(report.read_text(encoding="utf-8"), original)

    def test_old_report_keeps_its_refs_after_candidate_index_changes(self):
        (self.store.root / "crops/old.jpg").write_bytes(b"old-crop")
        (self.store.root / "crops/new.jpg").write_bytes(b"new-crop")
        self.store.put("observation", {"id": "current", "media_id": "m", "crop": "crops/new.jpg"})
        # Old candidate no longer exists in the index, but its report and crop do.
        self.write_report("[截图](../crops/old.jpg)")
        path = create(self.store, "t", self.report_path)
        with zipfile.ZipFile(self.store.asset(path)) as archive:
            self.assertEqual(archive.read("evidence/crops/old.jpg"), b"old-crop")
            self.assertNotIn("evidence/crops/new.jpg", archive.namelist())

    def test_missing_pending_crop_is_declared_without_a_broken_link(self):
        self.write_report("![候选](../crops/missing.jpg)")
        path = create(self.store, "t", self.report_path)
        with zipfile.ZipFile(self.store.asset(path)) as archive:
            report = archive.read("report.md").decode()
            self.assertNotIn("../crops/", report)
            self.assertIn("文件缺失或路径无效", report)
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["missing_crops"], ["crops/missing.jpg"])
            self.assertEqual(manifest["unavailable_crops"][0]["path"], "crops/missing.jpg")

    def test_traversal_and_symlinks_cannot_pack_files_outside_crops(self):
        with tempfile.TemporaryDirectory() as external:
            secret = Path(external) / "outside.jpg"
            secret.write_bytes(b"outside-crop-directory")
            (self.store.root / "crops/outside.jpg").symlink_to(secret)
            (self.store.root / "crops/video.jpg").symlink_to(self.store.root / "uploads/a.mp4")
            targets = ["../crops/../uploads/a.mp4", "../crops/%2e%2e/uploads/a.mp4",
                       "../crops/outside.jpg", "../crops/video.jpg"]
            self.write_report("\n".join(f"[截图]({target})" for target in targets))
            path = create(self.store, "t", self.report_path)
            with zipfile.ZipFile(self.store.asset(path)) as archive:
                self.assertEqual(set(json.loads(archive.read("manifest.json"))["included_crops"]),
                                 {"evidence/crops/a.jpg"})
                self.assertNotIn("../crops/", archive.read("report.md").decode())
                self.assertFalse(any(archive.read(name) == secret.read_bytes() for name in archive.namelist()))
                self.assertEqual(set(json.loads(archive.read("manifest.json"))["missing_crops"]),
                                 {"crops/../uploads/a.mp4", "crops/outside.jpg", "crops/video.jpg"})

    def test_images_spaces_and_exact_link_rewriting(self):
        relative = "crops/候选 图(1).jpg"
        (self.store.root / relative).write_bytes(b"named-crop")
        (self.store.root / "crops/a.jpg.extra").write_bytes(b"another-crop")
        original = (f"![候选](<../{relative}>)\n[另一图](../crops/a.jpg.extra)\n"
                    "纯文本 ../crops/a.jpg.extra\n[外部](https://example.com/a.jpg)\n")
        self.write_report(original)
        path = create(self.store, "t", self.report_path)
        with zipfile.ZipFile(self.store.asset(path)) as archive:
            report = archive.read("report.md").decode()
            self.assertIn(f"![候选]({quote('evidence/' + relative, safe='/')})", report)
            self.assertIn("[另一图](evidence/crops/a.jpg.extra)", report)
            self.assertIn("纯文本 ../crops/a.jpg.extra", report)
            self.assertIn("[外部](https://example.com/a.jpg)", report)
            for target in re.findall(r"\]\((evidence/[^)]+)\)", report):
                self.assertIn(unquote(target), archive.namelist())

    def test_old_report_preserves_task_media_and_historical_crop_set(self):
        old = self.store.asset(self.report_path).read_bytes()
        task = self.store.get('task', 't')
        task.update(version=4, name='new-task-name', cameras=['B'], crop='crops/new.jpg')
        self.store.put('task', task)
        media = self.store.get('media', 'm')
        media.update(camera_id='B', offset=99, time_version=2)
        self.store.put('media', media)
        (self.store.root / 'crops/new.jpg').write_bytes(b'new-crop')
        self.store.put('observation', {'id':'new', 'crop':'crops/new.jpg', 'owner_task':'t'})
        path = create(self.store, 't', self.report_path)
        with zipfile.ZipFile(self.store.asset(path)) as archive:
            task = json.loads(archive.read('task.json'))
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual((task['version'], manifest['task_version']), (3, 3))
            self.assertEqual(task['name'], 'bundle')
            self.assertEqual(manifest['media'][0]['camera_id'], 'A')
            self.assertNotIn('evidence/crops/new.jpg', archive.namelist())
            self.assertEqual(archive.read('report-source.md'), old)
            self.assertIn('-v3-bundle-', path)

    def test_report_changes_are_rejected_before_creating_a_zip(self):
        source = self.store.asset(self.report_path)
        source.write_text(source.read_text()+'\nchanged\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '已改变'):
            create(self.store, 't', self.report_path)
        self.assertEqual(list((self.store.root/'reports').glob('*.zip')), [])

    def test_sidecar_changes_or_removal_cannot_fall_back_to_current_state(self):
        sidecar = self.store.asset(sidecar_path(self.report_path))
        original = sidecar.read_text()
        snapshot = json.loads(original)
        snapshot['media'][0]['camera_id'] = 'changed'
        sidecar.write_text(json.dumps(snapshot), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '已改变'):
            create(self.store, 't', self.report_path)
        sidecar.unlink()
        with self.assertRaisesRegex(ValueError, '快照缺失'):
            create(self.store, 't', self.report_path)

    def test_wrong_task_report_pair_is_rejected(self):
        self.store.put('task', {'id':'other', 'version':3})
        with self.assertRaisesRegex(ValueError, '不属于'):
            create(self.store, 'other', self.report_path)

    def test_changed_crop_and_capture_sidecar_are_declared_not_replaced(self):
        (self.store.root/'crops/a.jpg').write_bytes(b'changed-crop')
        (self.store.root/'captures/c.json').write_text('{}', encoding='utf-8')
        path = create(self.store, 't', self.report_path)
        with zipfile.ZipFile(self.store.asset(path)) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(manifest['included_crops'], [])
            self.assertEqual(manifest['included_sidecars'], [])
            self.assertEqual(manifest['unavailable_sidecars'][0]['path'], 'captures/c.json')
            self.assertNotIn('[截图](', archive.read('report.md').decode())

    def test_reregistered_changed_video_still_fails_old_report_source_identity(self):
        source = self.store.root/'uploads/a.mp4'
        source.write_bytes(b'new-video')
        media = self.store.get('media', 'm')
        media['fingerprint'] = fingerprint(source)
        self.store.put('media', media)
        path = create(self.store, 't', self.report_path, include_media=True)
        with zipfile.ZipFile(self.store.asset(path)) as archive:
            entry = json.loads(archive.read('media-manifest.json'))[0]
            self.assertEqual(entry['source_integrity']['status'], 'changed')
            self.assertEqual(entry['source_integrity_at_report']['status'], 'valid')
            self.assertIsNone(entry['included_in_bundle'])

    def test_video_changes_during_export_abort_and_remove_partial_zip(self):
        def mutate_after_check(store, media):
            result = check(store, media)
            store.asset(media['path']).write_bytes(b'changed-after-check')
            return result
        with patch('tracking3.bundle.check', side_effect=mutate_after_check):
            with self.assertRaisesRegex(ValueError, '打包期间改变'):
                create(self.store, 't', self.report_path, include_media=True)
        self.assertEqual(list((self.store.root/'reports').glob('*.zip')), [])

    def test_legacy_report_recovers_only_its_task_and_source_facts(self):
        digest = self.store.get('media', 'm')['fingerprint']
        self.write_report('[截图](../crops/a.jpg) [源录像](../uploads/a.mp4)\n'
                          f'  素材 m；SHA256 {digest}；源引用 uploads/a.mp4；机位结构 未配置。\n', legacy=True)
        task = self.store.get('task', 't')
        task.update(version=4, name='new')
        self.store.put('task', task)
        media = self.store.get('media', 'm')
        media.update(camera_id='B', offset=88, fingerprint='f'*64)
        self.store.put('media', media)
        path = create(self.store, 't', self.report_path, include_media=True)
        with zipfile.ZipFile(self.store.asset(path)) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(manifest['task_version'], 3)
            self.assertEqual(manifest['metadata_basis'], 'legacy_report_source_only')
            self.assertIsNone(manifest['media'][0]['camera_id'])
            self.assertEqual(manifest['media'][0]['fingerprint'], digest)
            self.assertIn('sources/m-a.mp4', archive.namelist())
            self.assertTrue(json.loads(archive.read('report-snapshot.json'))['limitations'])

    def test_legacy_report_without_source_binding_has_no_dangling_video_link(self):
        self.write_report('[源录像](../uploads/a.mp4)', legacy=True)
        path = create(self.store, 't', self.report_path, include_media=True)
        with zipfile.ZipFile(self.store.asset(path)) as archive:
            self.assertNotIn('[源录像](', archive.read('report.md').decode())
            self.assertEqual(json.loads(archive.read('media-manifest.json')), [])

    def test_unbound_or_conflicting_legacy_report_is_rejected(self):
        self.write_report('body', legacy=True)
        report = self.store.asset(self.report_path)
        original = report.read_text()
        for text in ('[截图](../crops/a.jpg)', original.replace('结果版本：3','结果版本：4'),
                     original+'\n任务：t；结果版本：3\n'):
            with self.subTest(text=text):
                report.write_text(text, encoding='utf-8')
                with self.assertRaises(ValueError):
                    create(self.store, 't', self.report_path)


if __name__ == "__main__":
    unittest.main()
