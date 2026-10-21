"""Persist report metadata and bind it to the immutable Markdown artifact."""

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from .store import fingerprint
from .index_trace import snapshot as trace_snapshot

FORMAT = "tracking3-report-snapshot-v2"
REPORT_LINK = re.compile(r"(?P<image>!?)\[(?P<label>[^\]\n]*)\]\((?P<target><[^>\n]*>|[^)\n]*)\)")
_MARKER = re.compile(r"^<!-- tracking3-report-snapshot-sha256: ([a-f0-9]{64}) -->$", re.M)


def report_crop(match):
    target = unquote(match["target"].removeprefix("<").removesuffix(">"))
    return target[3:] if target.startswith("../crops/") else None


def crop_source(store, relative):
    if not isinstance(relative, str) or "\\" in relative or "\x00" in relative:
        raise ValueError("截图路径无效")
    parts = relative.split("/")
    if len(parts) < 2 or parts[0] != "crops" or any(p in ("", ".", "..") for p in parts):
        raise ValueError("截图路径必须位于 crops 目录")
    source = store.asset(relative)
    if not source.is_relative_to(store.root / "crops"):
        raise ValueError("截图文件已指向 crops 目录之外")
    return source


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _canonical(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _public(item):
    return {k: v for k, v in item.items() if k != "feature"}


def sidecar_path(report_path):
    path = PurePosixPath(report_path)
    if not path.parts or path.is_absolute() or ".." in path.parts or path.parts[0] != "reports" or path.suffix != ".md":
        raise ValueError("报告路径必须为 reports 内的 Markdown 文件")
    return str(path.with_suffix(".snapshot.json"))


def _task_in_markdown(text):
    headers = re.findall(r"^任务：([^；\n]+)；结果版本：(\d+)$", text, re.M)
    sections = re.findall(r"^## 导出时的查询条件\n\s*```json\n(.*?)\n```", text, re.M | re.S)
    if len(headers) != 1 or len(sections) != 1:
        raise ValueError("报告缺少唯一任务快照；请重新导出，不能用当前任务冒充旧版本")
    try:
        task = json.loads(sections[0])
    except json.JSONDecodeError as exc:
        raise ValueError("报告任务快照JSON无效") from exc
    if not isinstance(task, dict) or not re.fullmatch(r"[A-Za-z0-9_-]+", str(task.get("id", ""))):
        raise ValueError("报告任务标识无效")
    if type(task.get("version")) is not int or task["version"] < 0:
        raise ValueError("报告任务版本无效")
    if headers[0] != (task["id"], str(task["version"])):
        raise ValueError("报告标题与任务快照版本不一致")
    return _public(task)


def save(store, report_path, text, task, source_integrity):
    """Called while the report generator owns Store.lock; never reads later state."""
    public_task = _public(task)
    if _task_in_markdown(text) != public_task:
        raise ValueError("报告与待保存任务快照不一致")
    decisions = store.decisions(task["id"])
    decision_ids = {d["observation_id"] for d in decisions}
    referenced_crops = {report_crop(m) for m in REPORT_LINK.finditer(text)} - {None}
    observations = [_public(o) for o in store.list("observation")
                    if store.observation_owner(o) in (None, task['id']) and
                    (store.observation_owner(o) == task['id'] or o['id'] in decision_ids
                     or o.get('crop') in referenced_crops)]
    crops = {public_task.get("crop")}
    crops.update(o.get("crop") for o in observations)
    crops.update(report_crop(m) for m in REPORT_LINK.finditer(text))
    crops.discard(None)
    crop_digests = {}
    for relative in sorted(crops):
        try:
            crop_digests[relative] = fingerprint(crop_source(store, relative))
        except (ValueError, OSError):
            crop_digests[relative] = None
    media = store.list("media")
    try:
        trace = trace_snapshot(store, {m['id'] for m in media})
        by_observation = {}
        for frame in trace['frames']:
            for detection in frame['detections']:
                by_observation.setdefault(detection['observation_id'], []).append(frame['id'])
        diagnostics = dict(format='tracking3-report-tracking-diagnostics-v1',
            status='frozen_report_samples', scope='all_report_media', samples=trace,
            observations=[dict(observation_id=o['id'], media_id=o.get('media_id'),
                sample_ids=by_observation.get(o['id'], []),
                status='recorded_samples_only' if by_observation.get(o['id']) else
                    ('manual_without_automatic_trace' if o.get('source')=='manual'
                     else 'not_recorded_legacy_or_unindexed')) for o in observations],
            trajectory_identity_certified=False)
    except (ValueError, KeyError, TypeError) as exc:
        # A report must still expose historical decisions when diagnostics are
        # corrupt or refer to a replaced source. Never substitute newer samples.
        diagnostics = dict(format='tracking3-report-tracking-diagnostics-v1',
            status='unavailable_at_report', scope='all_report_media',
            reason=str(exc), samples=None, observations=None,
            trajectory_identity_certified=False)
    sidecar_digests = {}
    for item in media:
        path = item.get("capture_sidecar")
        if path:
            try:
                sidecar_digests[path] = fingerprint(store.asset(path))
            except (ValueError, OSError):
                sidecar_digests[path] = None
    payload = {"format": FORMAT, "task": public_task, "media": media,
               "decisions": decisions, "observations": observations,
               "source_integrity_at_report": source_integrity, "crop_digests": crop_digests,
               "sidecar_digests": sidecar_digests,
               "tracking_diagnostics": diagnostics,
               "metadata_basis": "report_snapshot"}
    digest = _sha(_canonical(payload))
    report_bytes = (text + f"\n\n<!-- tracking3-report-snapshot-sha256: {digest} -->\n").encode("utf-8")
    snapshot = dict(payload, metadata_sha256=digest, report_sha256=_sha(report_bytes))
    sidecar = store.root / sidecar_path(report_path)
    with sidecar.open("x", encoding="utf-8") as file:
        json.dump(snapshot, file, ensure_ascii=False, indent=2, allow_nan=False)
    try:
        with (store.root / report_path).open("xb") as file:
            file.write(report_bytes)
    except BaseException:
        sidecar.unlink()
        raise
    return report_path


def load(store, task_id, report_path, report_bytes):
    """Read bound metadata, or explicitly recover the facts an old report has."""
    text = report_bytes.decode("utf-8")
    task = _task_in_markdown(text)
    if task["id"] != task_id:
        raise ValueError("报告不属于指定任务")
    relative = sidecar_path(report_path)
    path = store.root / relative
    markers = _MARKER.findall(text)
    if path.exists() or path.is_symlink():
        try:
            snapshot = json.loads(store.asset(relative).read_text(encoding="utf-8"))
            payload = {k: v for k, v in snapshot.items() if k not in ("metadata_sha256", "report_sha256")}
            digest = _sha(_canonical(payload))
            if (snapshot.get("format") not in (FORMAT, 'tracking3-report-snapshot-v1') or snapshot.get("task") != task
                    or snapshot.get("report_sha256") != _sha(report_bytes)
                    or snapshot.get("metadata_sha256") != digest or markers != [digest]):
                raise ValueError("报告或元数据快照已改变，不能混用或回退到当前任务")
            return snapshot
        except (OSError, TypeError, AttributeError, json.JSONDecodeError) as exc:
            raise ValueError("报告快照无法核验") from exc
    if markers:
        raise ValueError("报告绑定的元数据快照缺失，请恢复原快照")

    # Legacy reports contain a task JSON block and source ID/path/SHA lines,
    # but no complete media/time snapshot. Never fill those gaps with today's DB.
    media = []
    for ident, digest, source in re.findall(
            r"^  素材 ([^；\n]+)；SHA256 ([^；\n]+)；源引用 ([^；\n]+)；机位结构 .*?$", text, re.M):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", ident):
            raise ValueError("旧报告素材标识无效")
        media.append({"id": ident, "path": "" if source == "未登记" else source,
                      "fingerprint": digest.lower() if re.fullmatch(r"[a-fA-F0-9]{64}", digest) else None,
                      "name": Path(source).name, "metadata_basis": "legacy_report_source_only"})
    if len({m["id"] for m in media}) != len(media):
        raise ValueError("旧报告素材标识重复，不能可靠恢复源引用")
    return {"format": "tracking3-legacy-report-recovery-v1", "task": task, "media": media,
            "metadata_basis": "legacy_report_source_only", "report_sha256": _sha(report_bytes),
            "crop_digests": None, "decisions": [], "observations": [],
            "source_integrity_at_report": {},
            "limitations": ["旧报告没有完整媒体时间/场所快照或截图指纹，未用当前数据库补造历史元数据。"]}
