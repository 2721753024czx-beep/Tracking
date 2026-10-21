"""Evaluate event retrieval with independent, snapshot-bound identity review.

Temporal overlap is reported separately: the same camera/time may contain
other people or a false detection. Identity hit rates stay unavailable until
all selected GT and candidate identities have sufficient independent review.
Ground-truth IDs and reviews are consumed only here, never by search/ranking.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
from typing import Any

REVIEW_SCHEMA = "tracking3-candidate-identity-review-v1"
CONCLUSIVE = {"target", "other", "background"}
REVIEW_STATUSES = CONCLUSIVE | {"uncertain", "unreviewed"}


def _float(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _source_key(value: Any) -> str:
    raw = _text(value).replace("\\", "/")
    if not raw:
        return ""
    path = PurePosixPath(raw)
    return "" if ".." in path.parts or str(path) in (".", "/") else str(path)


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_ground_truth(path: str | Path) -> list[dict[str, Any]]:
    """Retain incomplete rows explicitly; do not turn them into model misses."""
    rows = []
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        for number, row in enumerate(csv.DictReader(handle), start=2):
            row["_row"] = number
            for key in ("global_person_id", "camera_id", "source_path"):
                row[key] = _text(row.get(key))
            for key in ("start_media_s", "end_media_s"):
                raw = _text(row.get(key))
                row[key] = _float(raw)
                if raw not in (None, "", "null") and row[key] is None:
                    raise ValueError(f"GT 第{number}行{key}不是有限秒数")
            if row["start_media_s"] is not None and row["end_media_s"] is not None:
                if row["end_media_s"] < row["start_media_s"]:
                    raise ValueError(f"GT 第{number}行结束时间早于开始时间")
            rows.append(row)
    return rows


def load_candidates(path: str | Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("candidates", data.get("results"))
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ValueError("候选文件必须是 JSON 对象数组或包含 candidates/results 数组的对象")
    return data


def _interval(start: Any, end: Any) -> tuple[float | None, float | None]:
    start, end = _float(start), _float(end)
    if start is None or end is None or start < 0 or end < start:
        return None, None
    return start, end


def _candidate_time(candidate: dict[str, Any]) -> tuple[float | None, float | None]:
    return _interval(candidate.get("start"), candidate.get("end"))


def _same_source(event: dict[str, Any], candidate: dict[str, Any],
                 strict: bool = False) -> tuple[bool, str]:
    gt_source, candidate_source = _source_key(event.get("source_path")), _source_key(candidate.get("source_path"))
    if not gt_source or not candidate_source:
        return (False, "source_unknown") if strict else (True, "camera_and_time_only")
    return gt_source == candidate_source, "explicit_source_and_camera_and_time"


def _matches(event: dict[str, Any], candidate: dict[str, Any], tolerance: float,
             strict: bool = False) -> bool:
    if not _text(event.get("camera_id")) or _text(candidate.get("camera_id")) != _text(event["camera_id"]):
        return False
    if not _same_source(event, candidate, strict)[0]:
        return False
    start, end = _candidate_time(candidate)
    gt_start, gt_end = _interval(event.get("start_media_s"), event.get("end_media_s"))
    if None in (start, end, gt_start, gt_end):
        return False
    return start <= gt_end + tolerance and end >= gt_start - tolerance


def _select_events(events, target_id=None):
    identities = {_text(e.get("global_person_id")) for e in events} - {""}
    target = _text(target_id)
    if not target and len(identities) > 1:
        raise ValueError("GT包含多个目标身份，请显式指定 --target-id")
    if not target and len(identities) == 1:
        target = next(iter(identities))
    # Missing-ID rows remain visible as incomplete GT; never silently discard.
    selected = [e for e in events if not target or _text(e.get("global_person_id")) in ("", target)]
    return selected, target


def _gt_ready(event):
    required = ("global_person_id", "camera_id", "identity_evidence", "annotator", "annotation_version")
    return all(_text(event.get(key)) for key in required) and bool(_source_key(event.get("source_path"))) \
        and _interval(event.get("start_media_s"), event.get("end_media_s"))[0] is not None \
        and _text(event.get("visibility")) in ("visible", "occluded")


def _candidate_ready(candidate):
    return bool(_text(candidate.get("camera_id"))) and bool(_source_key(candidate.get("source_path"))) \
        and _candidate_time(candidate)[0] is not None and _float(candidate.get("score")) is not None


def _validate_reviews(reviews, candidates, target):
    candidate_ids = [_text(c.get("id")) for c in candidates]
    if any(not ident for ident in candidate_ids) or len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("身份评估须有非空、唯一的候选 id")
    if not isinstance(reviews, list):
        raise ValueError("reviews 必须是数组")
    by_id = {}
    for review in reviews:
        if not isinstance(review, dict):
            raise ValueError("review 必须是对象")
        ident, status = _text(review.get("candidate_id")), _text(review.get("identity_status"))
        if ident not in candidate_ids or ident in by_id:
            raise ValueError("复核引用未知或重复的 candidate_id")
        if status not in REVIEW_STATUSES:
            raise ValueError(f"未知 identity_status：{status}")
        if status in CONCLUSIVE:
            if any(not _text(review.get(key)) for key in ("annotator", "evidence", "annotation_version")):
                raise ValueError("确定身份复核须填写 annotator/evidence/annotation_version")
            pid = _text(review.get("global_person_id"))
            if status == "target" and (not target or pid != target):
                raise ValueError("target 复核的 global_person_id 必须等于评估目标")
            if status == "target":
                intervals = review.get("verified_intervals")
                if not isinstance(intervals, list) or not intervals:
                    raise ValueError("target 复核须填写连续回放确认的 verified_intervals")
                candidate = next(c for c in candidates if _text(c.get("id")) == ident)
                c_start, c_end = _candidate_time(candidate)
                for interval in intervals:
                    if not isinstance(interval, dict):
                        raise ValueError("verified_intervals 须是时间区间对象数组")
                    start, end = _interval(interval.get("start_media_s"), interval.get("end_media_s"))
                    if start is None or c_start is None or start < c_start - 1e-6 or end > c_end + 1e-6:
                        raise ValueError("身份核对区间无效或超出候选媒体区间")
            if status in ("other", "background") and pid and pid == target:
                raise ValueError("other/background 复核不能同时声明为目标身份")
        by_id[ident] = review
    return by_id


def _identity_matches(event, candidate, review, tolerance):
    if not review or review.get("identity_status") != "target" \
            or _text(review.get("global_person_id")) != _text(event.get("global_person_id")):
        return False
    if not _matches(event, candidate, tolerance, strict=True):
        return False
    gt_start, gt_end = _interval(event.get("start_media_s"), event.get("end_media_s"))
    return any(start <= gt_end + tolerance and end >= gt_start - tolerance
               for start, end in (_interval(i.get("start_media_s"), i.get("end_media_s"))
                                  for i in review["verified_intervals"]))


def evaluate(events: list[dict[str, Any]], candidates: list[dict[str, Any]],
             top_ks: tuple[int, ...] = (1, 5, 10), tolerance: float = 2.0,
             exclude_status: tuple[str, ...] = (), identity_reviews=None,
             target_id: str | None = None) -> dict[str, Any]:
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("tolerance 必须为有限非负数")
    if not top_ks or any(isinstance(k, bool) or int(k) != k or k <= 0 for k in top_ks):
        raise ValueError("top-k 须为正整数")
    top_ks = tuple(sorted({int(k) for k in top_ks}))
    selected, target = _select_events(events, target_id)
    ranked = [c for c in candidates if _text(c.get("status")) not in exclude_status]
    def score_key(candidate):
        score = _float(candidate.get("score"))
        return score is None, -score if score is not None else 0
    ranked.sort(key=score_key)
    reviews = _validate_reviews(identity_reviews, candidates, target) if identity_reviews is not None else {}
    unreviewed = [c for c in ranked if _text(reviews.get(_text(c.get("id")), {}).get("identity_status")) not in CONCLUSIVE]
    bad_candidates = [c for c in ranked if not _candidate_ready(c)]
    bad_events = [e for e in selected if not _gt_ready(e)]
    ready = bool(selected) and identity_reviews is not None and not unreviewed and not bad_events and not bad_candidates
    if not selected:
        metric_status = "no_ground_truth"
    elif bad_events:
        metric_status = "incomplete_ground_truth"
    elif identity_reviews is None:
        metric_status = "identity_review_required"
    elif bad_candidates:
        metric_status = "candidate_metadata_incomplete"
    elif unreviewed:
        metric_status = "identity_review_incomplete"
    else:
        metric_status = "ready"

    per_event = []
    for event in selected:
        temporal_rank = next((i for i, c in enumerate(ranked, 1) if _matches(event, c, tolerance)), None)
        verified_rank = next((i for i, c in enumerate(ranked, 1)
                              if _identity_matches(event, c, reviews.get(_text(c.get("id"))), tolerance)), None)
        time_valid = bool(_text(event.get("camera_id"))) and _interval(
            event.get("start_media_s"), event.get("end_media_s"))[0] is not None
        per_event.append({
            "gt_row": event.get("_row"), "global_person_id": _text(event.get("global_person_id")),
            "camera_id": _text(event.get("camera_id")), "source_path": _text(event.get("source_path")),
            "gt_ready": bool(_gt_ready(event)), "rank": verified_rank if ready else None,
            "matched": (verified_rank is not None) if ready else None,
            "verified_rank_so_far": verified_rank, "temporal_rank": temporal_rank,
            "temporal_assessable": time_valid,
            "temporal_match_basis": next((_same_source(event, c)[1] for c in ranked
                                         if _matches(event, c, tolerance)), "no_match"),
        })
    temporal_events = [e for e in per_event if e["temporal_assessable"]]
    temporal_hits = {str(k): sum(e["temporal_rank"] is not None and e["temporal_rank"] <= k
                                 for e in temporal_events) for k in top_ks}
    verified_hits = {str(k): sum(e["verified_rank_so_far"] is not None and e["verified_rank_so_far"] <= k
                                for e in per_event if e["gt_ready"]) for k in top_ks}
    return {
        "schema_version": 2,
        "metric_scope": "identity-reviewed event hit; temporal overlap is diagnostic only; not MOT/IDF1",
        "metric_status": metric_status, "identity_evaluation_ready": ready, "target_id": target or None,
        "events_input_total": len(events), "events_total": len(selected),
        "events_gt_incomplete": len(bad_events), "candidates_total": len(ranked),
        "candidates_excluded": len(candidates) - len(ranked), "exclude_status": list(exclude_status),
        "candidates_identity_unreviewed": len(unreviewed),
        "identity_review_counts": {status: sum(
            _text(reviews.get(_text(c.get("id")), {}).get("identity_status")) == status
            or (status == "unreviewed" and _text(c.get("id")) not in reviews)
            for c in ranked) for status in sorted(REVIEW_STATUSES)},
        "candidates_metadata_incomplete": len(bad_candidates), "tolerance_s": tolerance,
        "top_k_hits": {key: value if ready else None for key, value in verified_hits.items()},
        "top_k_hit_rate": {key: value / len(selected) if ready else None for key, value in verified_hits.items()},
        "verified_hits_so_far": verified_hits,
        "events_without_match": sum(e["rank"] is None for e in per_event) if ready else None,
        "temporal_events_assessable": len(temporal_events),
        "temporal_events_unassessable": len(selected) - len(temporal_events),
        "temporal_top_k_hits": temporal_hits,
        "temporal_top_k_overlap_rate": {key: value / len(temporal_events) if temporal_events else None
                                      for key, value in temporal_hits.items()},
        "per_event": per_event,
    }


def load_identity_reviews(path, ground_truth_path, candidates_path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != REVIEW_SCHEMA:
        raise ValueError("身份复核文件 schema 无效")
    for key, source in (("ground_truth_sha256", ground_truth_path), ("candidates_sha256", candidates_path)):
        if data.get(key) != file_sha256(source):
            raise ValueError(f"身份复核文件绑定的 {key} 不一致；须重新复核当前快照")
    if not isinstance(data.get("reviews"), list):
        raise ValueError("身份复核文件缺少 reviews 数组")
    return data["reviews"], _text(data.get("target_id"))


def review_template(events, candidates, ground_truth_path, candidates_path, target_id=None):
    _, target = _select_events(events, target_id)
    ids = [_text(c.get("id")) for c in candidates]
    if any(not ident for ident in ids) or len(set(ids)) != len(ids):
        raise ValueError("复核模板须有非空、唯一的候选 id")
    return {
        "schema": REVIEW_SCHEMA, "ground_truth_sha256": file_sha256(ground_truth_path),
        "candidates_sha256": file_sha256(candidates_path), "target_id": target,
        "instructions": "仅独立连续回放后填写 target/other/background；不确定保留 uncertain/unreviewed。target须填写目标global_person_id、verified_intervals及annotator/evidence/annotation_version；核对区间必须在候选内。禁止从score或当前应用status复制身份结论。",
        "reviews": [{"candidate_id": _text(c["id"]), "identity_status": "unreviewed",
                     "global_person_id": "", "annotator": "", "evidence": "", "annotation_version": "",
                     "verified_intervals": [],
                     "locator": {key: c.get(key) for key in ("camera_id", "source_path", "media_id", "media_path",
                                                           "start", "end", "representative", "box", "crop")}}
                    for c in candidates],
    }


def markdown(result):
    lines = ["# 事件级检索评估", "", f"状态：{result['metric_status']}。",
             f"目标：{result['target_id'] or '未登记'}；GT事件：{result['events_total']}；候选：{result['candidates_total']}；容差：±{result['tolerance_s']}s。",
             f"未完成身份复核候选：{result['candidates_identity_unreviewed']}；GT不完整：{result['events_gt_incomplete']}。",
             "", "| k | 身份命中 | 身份命中率 | 时间交集数（诊断） | 时间交集率（诊断） |",
             "|---|---:|---:|---:|---:|"]
    for key, hit in result["top_k_hits"].items():
        rate, temporal = result["top_k_hit_rate"][key], result["temporal_top_k_overlap_rate"][key]
        lines.append(f"| {key} | {hit if hit is not None else '—'} | {'—' if rate is None else f'{rate:.3f}'} | "
                     f"{result['temporal_top_k_hits'][key]} | {'—' if temporal is None else f'{temporal:.3f}'} |")
    lines += ["", "同时间的其他人和背景误框可能形成时间交集；该列不能作为目标命中率。身份复核不完整时不发布身份命中率。"
              "无真值或真值时间未知也不计作模型漏检；仍须检查未处理覆盖、源映射和标注范围。",
              "", "| GT行 | person | camera | 身份排名 | 时间交集排名 | GT完整 |",
              "|---:|---|---|---:|---:|---|"]
    for event in result["per_event"]:
        lines.append(f"| {event['gt_row']} | {event['global_person_id']} | {event['camera_id']} | "
                     f"{event['rank'] or '—'} | {event['temporal_rank'] or '—'} | {event['gt_ready']} |")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="评估身份复核后的事件命中，另列时间交集诊断")
    parser.add_argument("--ground-truth", required=True)
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--target-id", help="GT含多个身份时必须选择本次查询目标")
    parser.add_argument("--identity-reviews", help="绑定当前GT/候选SHA的独立身份复核JSON")
    parser.add_argument("--write-review-template", help="生成未填写复核模板；拒绝覆盖现有文件")
    parser.add_argument("--top-k", default="1,5,10")
    parser.add_argument("--tolerance-s", type=float, default=2.0)
    parser.add_argument("--exclude-status", default="")
    parser.add_argument("--out", help="JSON+Markdown证据文件")
    args = parser.parse_args()
    events, candidates = load_ground_truth(args.ground_truth), load_candidates(args.candidates)
    reviews, target = None, args.target_id
    if args.identity_reviews:
        reviews, review_target = load_identity_reviews(args.identity_reviews, args.ground_truth, args.candidates)
        if target and target != review_target:
            raise ValueError("--target-id 与复核文件的 target_id 不一致")
        target = review_target or target
    result = evaluate(events, candidates, top_ks=tuple(int(k) for k in args.top_k.split(",")),
                      tolerance=args.tolerance_s,
                      exclude_status=tuple(s.strip() for s in args.exclude_status.split(",") if s.strip()),
                      identity_reviews=reviews, target_id=target)
    result["inputs"] = {"ground_truth_sha256": file_sha256(args.ground_truth),
                        "candidates_sha256": file_sha256(args.candidates),
                        "identity_reviews_sha256": file_sha256(args.identity_reviews) if args.identity_reviews else None}
    if args.write_review_template:
        output = Path(args.write_review_template)
        output.parent.mkdir(parents=True, exist_ok=True)
        template = review_template(events, candidates, args.ground_truth, args.candidates, target)
        with output.open("x", encoding="utf-8") as handle:
            json.dump(template, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    evidence = json.dumps(result, ensure_ascii=False, indent=2) + "\n\n" + markdown(result)
    if args.out:
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(evidence, encoding="utf-8")
    print(evidence)


if __name__ == "__main__":
    main()
