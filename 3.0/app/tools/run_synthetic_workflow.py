"""Run an end-to-end offline workflow on the generated two-camera demo.

This uses a deterministic blue-body detector only to exercise the Tracking 3.0
data flow.  It is a pipeline regression fixture, never a model benchmark.
"""

import argparse
import csv
import json
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from tracking3.bundle import create as create_bundle
from tracking3.conditions import values as query_values
from tracking3.engine import Encoder, Indexer, search
from tracking3.media import crop, probe, read_frame
from tracking3.server import report
from tracking3.store import Store, fingerprint, uid
from tracking3.timing import metadata as time_metadata
from tools.evaluate_events import evaluate, load_ground_truth


def blue_body_detector(rgb):
    mask = (rgb[:, :, 2] > 105) & (rgb[:, :, 0] < 100) & (rgb[:, :, 1] < 160)
    ys, xs = np.where(mask)
    if len(xs) < 20:
        return []
    # The generated person's shirt is blue; pad to include the whole body crop.
    x1, x2 = max(0, int(xs.min()) - 3), min(rgb.shape[1], int(xs.max()) + 4)
    y1, y2 = max(0, int(ys.min()) - 22), min(rgb.shape[0], int(ys.max()) + 4)
    return [([x1, y1, x2, y2], 0.99, 1)]


def main():
    parser = argparse.ArgumentParser(description="运行双路合成素材的Tracking 3.0离线回归")
    parser.add_argument("--input", default="../数据准备/synthetic-two-camera-v1")
    parser.add_argument("--output", default="./data-synthetic-demo-v1")
    parser.add_argument("--sample-fps", type=float, default=5)
    args = parser.parse_args()
    source_root = Path(args.input).resolve()
    root = Path(args.output).resolve()
    if not source_root.is_dir():
        raise SystemExit(f"合成输入目录不存在：{source_root}")
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"输出目录非空：{root}；请指定新的 --output，避免覆盖既有回归证据")
    root.mkdir(parents=True, exist_ok=True)
    (root / "uploads").mkdir()
    (root / "crops").mkdir()

    store = Store(root)
    encoder = Encoder()
    indexer = Indexer(store, encoder, root / "missing-weights.pt")
    media = []
    try:
        with (source_root / "media_manifest.csv").open(encoding="utf-8") as handle:
            manifest = list(csv.DictReader(handle))
        for item in manifest:
            source = source_root / item["source_path"]
            relative = f"uploads/{source.name}"
            shutil.copy2(source, root / relative)
            record = dict(id=uid(), path=relative, name=source.name,
                          source_path=item["source_path"],
                          camera_id=item["camera_id"], location=item["location"],
                          fingerprint=fingerprint(root / relative), status="unprocessed",
                          processed_until=-1, time_version=1,
                          **time_metadata({}), **probe(root / relative))
            store.put("media", record)
            media.append(record)
        for record in media:
            indexer.process(record["id"], args.sample_fps, detector=blue_body_detector)
        media = [store.get("media", record["id"]) for record in media]

        query_media = media[0]
        rgb, at = read_frame(store.asset(query_media["path"]), 2.0)
        box = blue_body_detector(rgb)[0][0]
        query = crop(rgb, box)
        query_relative = "crops/query-synthetic.jpg"
        Image.fromarray(query).save(root / query_relative)
        task_id = uid()
        task = dict(id=task_id, name="合成双路照片入口回归", entry="photo",
                    crop=query_relative, encoder=encoder.version,
                    feature=encoder.extract(query), cameras=[], from_epoch=None,
                    to_epoch=None, status="active", version=1,
                    search_direction="after", reference_observations=[],
                    reference_review_required=False, anchor=None)
        task.update(query_values({}))
        store.put("task", task)
        store.log(task_id, "create", {k: v for k, v in task.items() if k != "feature"})
        candidates = search(store, encoder, task_id, 100)
        by_media = {m["id"]: m for m in media}
        for candidate in candidates:
            candidate["source_path"] = by_media[candidate["media_id"]]["source_path"]
        candidate_path = root / "candidates.json"
        candidate_path.write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
        events = load_ground_truth(source_root / "target_events.csv")
        metrics = evaluate(events, candidates, top_ks=(1, 5, 10), tolerance=0)
        report_path = report(store, task_id)
        bundle_path = create_bundle(store, task_id, report_path, include_media=False)
        result = {"scope": "synthetic pipeline regression; not a model benchmark",
                  "task_id": task_id, "media": media, "indexed_observations": len(store.list("observation")),
                  "candidate_count": len(candidates), "metrics": metrics,
                  "report": report_path, "bundle": bundle_path,
                  "candidates": "candidates.json"}
        (root / "workflow-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(root), "task_id": task_id,
                          "indexed_observations": result["indexed_observations"],
                          "candidate_count": result["candidate_count"],
                          "identity_hit_rate": metrics["top_k_hit_rate"],
                          "temporal_overlap_rate": metrics["temporal_top_k_overlap_rate"],
                          "metric_status": metrics["metric_status"],
                          "bundle": str(root / bundle_path)}, ensure_ascii=False, indent=2))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
