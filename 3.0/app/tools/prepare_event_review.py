"""Prepare a source-only review pack for independent event annotation.

The output deliberately contains no detector or search result.  It samples
decoded source frames at fixed media times, creates contact sheets, and writes
the empty target-events schema needed by ``evaluate_events.py``.  It is a
review aid, not ground truth generation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import av
from PIL import Image, ImageDraw


EVENT_FIELDS = [
    "dataset", "sequence", "global_person_id", "camera_id", "source_path",
    "start_frame", "end_frame_inclusive", "start_media_s", "end_media_s",
    "keyframe", "bbox_xyxy", "visibility", "identity_evidence", "annotator",
    "annotation_version",
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _duration(path: Path) -> float | None:
    with av.open(str(path)) as container:
        if container.duration is not None:
            return float(container.duration) / float(av.time_base)
        stream = container.streams.video[0]
        if stream.duration is not None and stream.time_base is not None:
            return float(stream.duration * stream.time_base)
    return None


def _sample_frames(path: Path, requested: list[float]) -> list[dict[str, Any]]:
    """Decode each source once and return the first frame at/after each target."""
    rows: list[dict[str, Any]] = []
    if not requested:
        return rows
    target_index = 0
    origin: float | None = None
    previous_time: float | None = None
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        for frame_index, frame in enumerate(container.decode(stream)):
            if frame.pts is None or frame.time_base is None:
                raise ValueError(f"源帧缺少 PTS/time_base，不能推定媒体秒数：{path.name} 帧{frame_index}")
            raw_time = float(frame.pts * frame.time_base)
            if not math.isfinite(raw_time):
                raise ValueError(f"源帧时间无效：{path.name} 帧{frame_index}")
            if origin is None:
                origin = raw_time
            media_s = raw_time - origin
            if previous_time is not None and media_s < previous_time - 1e-6:
                raise ValueError(f"源 PTS 倒退，须先核对时间：{path.name} 帧{frame_index}")
            previous_time = media_s
            while target_index < len(requested) and media_s + 1e-6 >= requested[target_index]:
                image = frame.to_image().convert("RGB")
                image.thumbnail((352, 226))
                rows.append({
                    "requested_media_s": requested[target_index],
                    "actual_media_s": media_s,
                    "frame_index": frame_index,
                    "raw_pts": frame.pts,
                    "time_base": str(frame.time_base),
                    "origin_pts_s": origin,
                    "source_width": frame.width,
                    "source_height": frame.height,
                    "image": image,
                })
                target_index += 1
            if target_index >= len(requested):
                break
    return rows


def _contact_sheets(camera_id: str, prefix: str, samples: list[dict[str, Any]], output: Path,
                    columns: int, per_sheet: int) -> list[str]:
    tile_w, tile_h, label_h = 368, 242, 42
    names: list[str] = []
    for page_start in range(0, len(samples), per_sheet):
        page = samples[page_start:page_start + per_sheet]
        rows = max(1, math.ceil(len(page) / columns))
        sheet = Image.new("RGB", (tile_w * columns, (tile_h + label_h) * rows), "#d8d8d8")
        draw = ImageDraw.Draw(sheet)
        for offset, item in enumerate(page):
            image = item["image"].copy()
            image.thumbnail((tile_w - 16, tile_h - 16))
            x = (offset % columns) * tile_w
            y = (offset // columns) * (tile_h + label_h)
            sheet.paste(image, (x + (tile_w - image.width) // 2, y + 8))
            draw.text((x + 8, y + tile_h + 3),
                      f"{camera_id}  t={item['actual_media_s']:.3f}s  f={item['frame_index']}",
                      fill="black")
        name = f"{prefix}-contact-{page_start // per_sheet + 1:03d}.jpg"
        sheet.save(output / name, quality=90)
        names.append(name)
    return names


def _read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"media manifest 为空：{path}")
    return rows


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="生成独立事件人工复核包（不读取检测结果）")
    parser.add_argument("--input", required=True, help="含 media_manifest.csv 和视频的素材目录")
    parser.add_argument("--output", required=True, help="新的空输出目录")
    parser.add_argument("--sample-every-s", type=float, default=2.0)
    parser.add_argument("--duration-s", type=float, default=None,
                        help="可选：只复核每路前 N 秒；默认读取容器时长")
    parser.add_argument("--columns", type=int, default=4)
    parser.add_argument("--per-sheet", type=int, default=20)
    args = parser.parse_args(argv)
    if not math.isfinite(args.sample_every_s) or args.sample_every_s <= 0 or args.columns <= 0 or args.per_sheet <= 0:
        raise SystemExit("sample-every-s、columns、per-sheet 必须为正数")
    if args.duration_s is not None and (not math.isfinite(args.duration_s) or args.duration_s <= 0):
        raise SystemExit("duration-s 必须为有限正数")

    source_root = Path(args.input).resolve()
    output = Path(args.output).resolve()
    if not source_root.is_dir():
        raise SystemExit(f"输入目录不存在：{source_root}")
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"输出目录非空：{output}；请使用新的目录，避免覆盖标注证据")
    manifest_path = source_root / "media_manifest.csv"
    manifest = _read_manifest(manifest_path)
    prepared = []
    for row in manifest:
        source_rel = row.get("source_path", "").strip()
        source = (source_root / source_rel).resolve()
        if not source.is_file() or source_root not in source.parents:
            raise SystemExit(f"manifest source 不存在或越界：{source_rel}")
        duration = _duration(source)
        limit = args.duration_s if args.duration_s is not None else duration
        if limit is None or not math.isfinite(limit) or limit <= 0:
            raise SystemExit(f"无法获得正的媒体时长：{source}")
        if duration is not None and math.isfinite(duration):
            limit = min(limit, duration)
        actual_sha = sha256(source)
        expected_sha = (row.get("sha256", "") or "").strip().lower()
        if expected_sha and expected_sha != actual_sha:
            raise SystemExit(f"源 SHA 与 manifest 不一致，停止生成复核包：{source_rel}")
        prepared.append((row, source, duration, limit, actual_sha, expected_sha))

    output.mkdir(parents=True, exist_ok=True)
    review_points: list[dict[str, Any]] = []
    source_records: list[dict[str, Any]] = []
    for source_index, (row, source, duration, limit, actual_sha, expected_sha) in enumerate(prepared, start=1):
        source_rel = row.get("source_path", "").strip()
        requested = []
        current = 0.0
        while current < limit - 1e-6:
            requested.append(round(current, 6))
            current += args.sample_every_s
        samples = _sample_frames(source, requested)
        if not samples:
            raise ValueError(f"未解码出带时间依据的复核画面：{source_rel}")
        if sha256(source) != actual_sha:
            raise ValueError(f"复核抽帧过程中源文件发生变化：{source_rel}")
        camera_id = row.get("camera_id", "") or source.stem
        prefix = f"source-{source_index:03d}"
        sheets = _contact_sheets(camera_id, prefix, samples, output, args.columns, args.per_sheet)
        source_records.append({
            "source_index": source_index,
            "camera_id": camera_id,
            "source_path": source_rel,
            "duration_s": duration,
            "review_limit_s": limit,
            "sample_count": len(samples),
            "sha256": actual_sha,
            "manifest_sha256": expected_sha,
            "sha_status": "valid" if expected_sha else "unverified",
            "frame_index_base": 0,
            "media_time_basis": "decoded frame PTS minus first decoded frame PTS",
            "manifest_frame_index_base": row.get("frame_index_base", ""),
            "time_evidence": row.get("time_evidence", ""),
            "time_basis": row.get("time_basis", ""),
            "origin_pts_s": samples[0]["origin_pts_s"],
            "unresolved_requested_times": requested[len(samples):],
            "contact_sheets": sheets,
        })
        for item in samples:
            review_points.append({
                "source_index": source_index,
                "camera_id": camera_id,
                "source_path": source_rel,
                "requested_media_s": item["requested_media_s"],
                "actual_media_s": item["actual_media_s"],
                "frame_index": item["frame_index"],
                "frame_index_base": 0,
                "raw_pts": item["raw_pts"],
                "time_base": item["time_base"],
                "origin_pts_s": item["origin_pts_s"],
                "source_width": item["source_width"],
                "source_height": item["source_height"],
            })

    with (output / "review_points.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = list(review_points[0])
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(review_points)
    with (output / "target_events.template.csv").open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerow(EVENT_FIELDS)
    (output / "review-manifest.json").write_text(json.dumps({
        "purpose": "source-only manual event review; not ground truth",
        "input_manifest": str(manifest_path),
        "input_manifest_sha256": sha256(manifest_path),
        "sample_every_s": args.sample_every_s,
        "frame_index_base": 0,
        "annotation_status": "not_started",
        "sources": source_records,
        "review_points": len(review_points),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "README.md").write_text(
        "# 独立事件人工复核包\n\n"
        "本目录只由原始录像抽帧生成，不读取检测、跟踪或检索结果。接触表和 `review_points.csv` 用于人工定位可见事件；"
        "请独立观看原视频后填写 `target_events.csv`，不要把当前模型候选复制成真值。\n\n"
        "每条事件至少填写 `global_person_id`、`camera_id`、`source_path`、起止媒体秒数、可见性和身份依据；看不清时填写 `uncertain`，"
        "不要用猜测补齐时间或身份。完成后将填写后的文件另存为 `target_events.csv`，再运行 `tools/evaluate_events.py`。\n\n"
        "帧号从0开始；媒体秒数为实际帧PTS减首帧PTS，不是请求抽帧时间，也不是现实墙上时间。关键帧框坐标使用原图分辨率，不能用缩小接触表坐标。\n\n"
        "接触表每隔固定秒数抽一帧，期间可能漏掉短暂出现和遮挡变化；完成GT须连续观看原录像、细查起止边界。"
        "`review-manifest.json` 保存源SHA、PTS依据和未解码的请求点；源SHA不符时工具会停止生成。\n",
        encoding="utf-8")
    print(json.dumps({"output": str(output), "sources": len(source_records),
                      "review_points": len(review_points)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
