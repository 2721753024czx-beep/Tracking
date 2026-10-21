"""Create a portable evidence bundle for one report version."""

from __future__ import annotations

import json
import hashlib
import re
import time
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from .integrity import check
from .snapshots import REPORT_LINK as _REPORT_LINK, report_crop as _report_crop, crop_source as _crop_source, load as load_snapshot


def _safe_name(value: str, fallback: str) -> str:
    name = Path(value or fallback).name
    name = re.sub(r"[^0-9A-Za-z._-]+", "_", name).strip("._")
    return name or fallback


def _write_text(archive: zipfile.ZipFile, path: str, text: str) -> None:
    archive.writestr(path, text.encode("utf-8"))


def create(store, task_id: str, report_path: str, include_media: bool = False) -> str:
    """Create a ZIP with report, metadata, referenced crops and optional valid videos.

    The report is rewritten to point at paths inside the archive. Invalid or
    omitted source videos remain listed in ``sources/README.md`` instead of
    being silently presented as portable evidence.
    """
    report_file = store.asset(report_path)
    report_bytes = report_file.read_bytes()
    report_text = report_bytes.decode("utf-8")
    snapshot = load_snapshot(store, task_id, report_path, report_bytes)
    task = snapshot["task"]
    media_items = snapshot["media"]
    diagnostics = snapshot.get('tracking_diagnostics') or dict(
        format='tracking3-report-tracking-diagnostics-v1', status='not_recorded_at_report',
        reason='该报告生成时未冻结轨迹采样；没有使用当前数据库补造历史证据。',
        samples=None, observations=None, trajectory_identity_certified=False)
    diagnostic_files = []
    samples = diagnostics.get('samples')
    if samples is not None:
        sample_bytes = (json.dumps(samples, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode()
        diagnostic_files.append(('tracking-samples.json', sample_bytes))
    diagnostic_public = {k:v for k,v in diagnostics.items() if k!='samples'}
    diagnostic_public.update(metadata_basis=snapshot['metadata_basis'],
        source_report_sha256=snapshot['report_sha256'], task_id=task_id,
        task_version=task.get('version'), samples_file='tracking-samples.json' if samples is not None else None,
        samples_sha256=hashlib.sha256(sample_bytes).hexdigest() if samples is not None else None)
    diagnostic_files.append(('tracking-diagnostics.json',
        (json.dumps(diagnostic_public, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode()))
    media_manifest = []
    source_lines = [
        "# 源录像清单", "", "证据包默认不复制源录像；源录像是否可核验、原始路径和 SHA256 见下表。",
        "元数据依据：" + snapshot["metadata_basis"] + "。源核验状态为本次打包时对报告所绑定指纹的检查。",
        ""
    ]
    source_arcs: dict[str, str] = {}
    sidecar_files = []
    unavailable_sidecars = []
    for media in media_items:
        integrity = check(store, media)
        arc = None
        if include_media and integrity["valid"]:
            arc = f"sources/{media['id']}-{_safe_name(media.get('name'), 'video.mp4')}"
            source_arcs[media.get("path")] = arc
        media_manifest.append({
            "id": media.get("id"), "name": media.get("name"), "camera_id": media.get("camera_id"),
            "location": media.get("location"), "relative_path": media.get("path"),
            "fingerprint": media.get("fingerprint"), "status": media.get("status"),
            "source_integrity": integrity, "included_in_bundle": arc,
            "source_integrity_at_report": snapshot.get("source_integrity_at_report", {}).get(media["id"]),
            "metadata_basis": snapshot["metadata_basis"],
            "capture_sidecar": media.get("capture_sidecar"),
            "capture_time_basis": media.get("capture_time_basis"),
            "capture_video_timestamp_mode": media.get("capture_video_timestamp_mode"),
            "capture_video_timestamp_basis": media.get("capture_video_timestamp_basis"),
            "capture_wallclock_started_at": media.get("capture_wallclock_started_at"),
            "capture_wallclock_finished_at": media.get("capture_wallclock_finished_at"),
        })
        sidecar = media.get("capture_sidecar")
        if sidecar:
            try:
                sidecar_source = store.asset(sidecar).read_bytes()
                expected = snapshot.get("sidecar_digests", {}).get(sidecar)
                if not expected or hashlib.sha256(sidecar_source).hexdigest() != expected:
                    raise ValueError("取流sidecar与报告生成时的指纹不一致或当时无法核验")
            except (ValueError, OSError) as exc:
                sidecar_source = None
                unavailable_sidecars.append({"path": sidecar, "reason": str(exc)})
            if sidecar_source is not None:
                sidecar_files.append((f"capture/{media['id']}.json", sidecar_source))
        source_lines.append(
            f"### {media.get('id')}\n\n"
            f"- 机位：{media.get('camera_id') or '未登记（报告未保留）'}；名称：{media.get('name')}；状态：{integrity['status']}\n"
            f"- SHA256：`{media.get('fingerprint') or '未登记'}`；"
            f"{'已复制到 `' + arc + '`' if arc else '未复制'}。\n"
        )

    # Use this report's references, not a new search with possibly newer query
    # conditions. Pending shared observations have no task owner or decision.
    crop_paths = {task.get("crop")}
    crop_paths.update(_report_crop(match) for match in _REPORT_LINK.finditer(report_text))
    for observation in snapshot.get("observations", []):
        crop_paths.add(observation.get("crop"))
    crop_paths.discard(None)
    included = []
    missing = []
    unavailable = []
    crop_arcs = {}
    for relative in sorted(crop_paths):
        try:
            source = _crop_source(store, relative)
            content = source.read_bytes()
            digests = snapshot.get("crop_digests")
            if digests is not None:
                expected = digests.get(relative)
                if not expected or hashlib.sha256(content).hexdigest() != expected:
                    raise ValueError("截图与报告生成时的指纹不一致或当时无法核验")
        except (ValueError, OSError) as exc:
            missing.append(relative)
            unavailable.append({"path": relative, "reason": str(exc)})
            continue
        arc = "evidence/" + str(PurePosixPath(relative))
        crop_arcs[relative] = arc
        included.append((arc, content))

    def rewrite_crop(match):
        relative = _report_crop(match)
        if relative is None:
            return match[0]
        arc = crop_arcs.get(relative)
        if arc:
            return f"{match['image']}[{match['label']}]({quote(arc, safe='/')})"
        return f"{match['label'] or '截图'}（文件缺失或路径无效，详见 manifest.json）"

    report_text = _REPORT_LINK.sub(rewrite_crop, report_text)
    for media in media_items:
        relative = media.get("path")
        if not relative:
            continue
        marker = f"[源录像](../{relative})"
        if relative in source_arcs:
            report_text = report_text.replace(marker, f"[源录像]({source_arcs[relative]})")
        else:
            report_text = report_text.replace(
                marker, f"源录像（未打包；详见 sources/README.md#{media.get('id')}）"
            )
    # A legacy report may reference a source without a recoverable ID/SHA row.
    # Preserve this uncertainty explicitly, rather than leaving an outside link.
    report_text = re.sub(r"\[源录像\]\(\.\./[^)\n]+\)",
                         "源录像（未打包；报告缺少绑定源信息，详见 report-snapshot.json）", report_text)

    bundle_relative = f"reports/{task_id}-v{task.get('version', 0)}-bundle-{time.time_ns()}.zip"
    bundle_path = store.root / bundle_relative
    manifest = {
        "format": "tracking3-evidence-bundle-v3",
        "task_id": task_id,
        "task_version": task.get("version"),
        "report_source": report_path,
        "include_media_requested": bool(include_media),
        "source_report_sha256": snapshot["report_sha256"],
        "bundled_report_sha256": hashlib.sha256(report_text.encode("utf-8")).hexdigest(),
        "metadata_basis": snapshot["metadata_basis"],
        "files": {"report": "report.md", "original_report": "report-source.md", "task": "task.json", "media": "media-manifest.json", "snapshot": "report-snapshot.json"},
        "tracking_diagnostics_status": diagnostics['status'],
        "copied_diagnostic_sha256": {arc:hashlib.sha256(content).hexdigest() for arc,content in diagnostic_files},
        "included_crops": [arc for arc, _ in included],
        "included_sidecars": [arc for arc, _ in sidecar_files],
        "unavailable_sidecars": unavailable_sidecars,
        "copied_crop_sha256": {arc: hashlib.sha256(content).hexdigest() for arc, content in included},
        "missing_crops": missing,
        "unavailable_crops": unavailable,
        "media": media_manifest,
    }
    manifest['files']['tracking_diagnostics']='tracking-diagnostics.json'
    manifest['files']['tracking_samples']='tracking-samples.json' if samples is not None else None
    task_public = {k: v for k, v in task.items() if k != "feature"}
    archive = zipfile.ZipFile(bundle_path, "x", compression=zipfile.ZIP_DEFLATED)
    try:
        with archive:
            _write_text(archive, "report.md", report_text)
            archive.writestr("report-source.md", report_bytes)
            _write_text(archive, "task.json", json.dumps(task_public, ensure_ascii=False, indent=2))
            _write_text(archive, "media-manifest.json", json.dumps(media_manifest, ensure_ascii=False, indent=2))
            _write_text(archive, "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            _write_text(archive, "report-snapshot.json", json.dumps(snapshot, ensure_ascii=False, indent=2))
            _write_text(archive, "sources/README.md", "\n".join(source_lines) + "\n")
            _write_text(archive, 'diagnostics/README.md',
                '# 采样轨迹核对\n\ntracking-diagnostics.json绑定本报告的任务版本、原报告SHA和采样文件SHA。\n'
                'tracking-samples.json（若存在）是报告生成时冻结的实际采样，涵盖报告所列全部素材，'
                '包括当前候选之外的检测及零检测采样；候选对应sample_ids见诊断文件，观察元数据见报告快照。\n'
                '用media_id在media-manifest.json查included_in_bundle与源SHA；源未打包时须另提供同SHA原件。\n'
                '按frame_index（从0开始）解码原录像，用raw_pts/time_base/origin_pts_s核对时间；'
                '检测框取该点detections，不用代表框替代。detector_sample保留原检测及跟踪返回索引。\n'
                '单帧线索不证明连续身份；未采样区间不插值认证，零检测不证明无人。'
                'manual_without_automatic_trace没有自动轨迹，不继承被纠正片段的采样。\n'
                '旧报告未记录或采样失效时不从当前数据库补造；缺失状态和原因见诊断文件。\n')
            for arc, content in diagnostic_files:
                archive.writestr(arc, content)
            for arc, content in included:
                archive.writestr(arc, content)
            for arc, content in sidecar_files:
                archive.writestr(arc, content)
            for relative, arc in source_arcs.items():
                media = next(m for m in media_items if m["path"] == relative)
                digest = hashlib.sha256()
                with store.asset(relative).open("rb") as source, archive.open(arc, "w", force_zip64=True) as target:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        target.write(chunk)
                if digest.hexdigest() != media["fingerprint"]:
                    raise ValueError("源录像在打包期间改变，请重试或恢复报告绑定原件")
    except BaseException:
        bundle_path.unlink(missing_ok=True)
        raise
    return bundle_relative
