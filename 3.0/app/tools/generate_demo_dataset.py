"""Generate two small synthetic videos plus a truthful event annotation template.

The output is useful for checking import, time metadata, GT file handling and
evidence export before cameras arrive.  It is deliberately labeled synthetic;
it must not be used as a real ReID or deployment benchmark.
"""

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

import av
import numpy as np
from PIL import Image, ImageDraw


FPS = 10
WIDTH, HEIGHT = 320, 240
TARGET_W, TARGET_H = 32, 86


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_video(path: Path, camera_index: int, duration_s: float):
    container = av.open(str(path), "w")
    stream = container.add_stream("mpeg4", rate=FPS)
    stream.width, stream.height, stream.pix_fmt = WIDTH, HEIGHT, "yuv420p"
    boxes = []
    frames = int(duration_s * FPS)
    for index in range(frames):
        second = index / FPS
        image = Image.new("RGB", (WIDTH, HEIGHT), (38 + camera_index * 12, 48, 58))
        draw = ImageDraw.Draw(image)
        for x in range(0, WIDTH, 40):
            draw.line((x, 0, x, HEIGHT), fill=(55 + camera_index * 8, 66, 74), width=2)
        # A camera-dependent background person creates a non-target distractor.
        distractor_x = 215 + int(10 * np.sin(second * 2))
        draw.ellipse((distractor_x, 74, distractor_x + 22, 98), fill=(150, 120, 90))
        draw.rectangle((distractor_x - 2, 96, distractor_x + 24, 168), fill=(100, 80, 120))
        # The same synthetic target traverses both views with a small offset.
        x = 35 + int((second / max(duration_s, 1)) * 180)
        y = 84 + camera_index * 4
        box = [x, y, x + TARGET_W, y + TARGET_H]
        draw.ellipse((x + 7, y - 18, x + 25, y), fill=(194, 150, 112))
        draw.rectangle((x, y, x + TARGET_W, y + 45), fill=(35, 105, 175))
        draw.rectangle((x + 4, y + 45, x + 13, y + TARGET_H), fill=(45, 45, 55))
        draw.rectangle((x + 19, y + 45, x + 28, y + TARGET_H), fill=(45, 45, 55))
        boxes.append((second, box))
        frame = av.VideoFrame.from_ndarray(np.asarray(image), format="rgb24")
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return boxes


def main():
    parser = argparse.ArgumentParser(description="生成双路合成录像和独立事件GT模板")
    parser.add_argument("--output", default="../数据准备/synthetic-two-camera")
    parser.add_argument("--duration", type=float, default=8.0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists() and any(output.iterdir()) and not args.force:
        raise SystemExit(f"输出目录非空：{output}；需要覆盖请显式传 --force")
    output.mkdir(parents=True, exist_ok=True)
    records = []
    all_boxes = {}
    for index, camera_id in enumerate(("demo_cam_a", "demo_cam_b")):
        path = output / f"{camera_id}.mp4"
        boxes = write_video(path, index, args.duration)
        all_boxes[camera_id] = boxes
        records.append({
            "dataset": "tracking3-synthetic", "sequence": "two-camera-demo",
            "camera_id": camera_id, "source_path": path.name, "source_url": "",
            "revision": "generated-v1", "sha256": sha256(path), "license": "generated",
            "location": "synthetic lab", "width": WIDTH, "height": HEIGHT,
            "codec": "mpeg4", "fps": FPS, "time_basis": "file-relative synthetic",
            "frame_index_base": 0, "relative_start_s": 0, "offset_s": 0,
            "uncertainty_s": 0, "time_evidence": "generated frame clock; synthetic only",
            "known_gaps": "", "conversion_notes": "not a real-world benchmark",
        })
    manifest_fields = list(records[0])
    with (output / "media_manifest.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=manifest_fields)
        writer.writeheader(); writer.writerows(records)
    event_fields = ["dataset", "sequence", "global_person_id", "camera_id", "source_path",
                    "start_frame", "end_frame_inclusive", "start_media_s", "end_media_s",
                    "keyframe", "bbox_xyxy", "visibility", "identity_evidence", "annotator",
                    "annotation_version"]
    with (output / "target_events.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=event_fields)
        writer.writeheader()
        for camera_id in ("demo_cam_a", "demo_cam_b"):
            boxes = all_boxes[camera_id]
            start = next(item for item in boxes if item[0] >= 1.0)
            end = next(item for item in reversed(boxes) if item[0] <= 6.0)
            writer.writerow({
                "dataset": "tracking3-synthetic", "sequence": "two-camera-demo",
                "global_person_id": "demo_person_1", "camera_id": camera_id,
                "source_path": f"{camera_id}.mp4", "start_frame": int(start[0] * FPS),
                "end_frame_inclusive": int(end[0] * FPS), "start_media_s": start[0],
                "end_media_s": end[0], "keyframe": json.dumps(start[1]),
                "bbox_xyxy": json.dumps(start[1]), "visibility": "visible",
                "identity_evidence": "synthetic generator; not an independent human GT",
                "annotator": "generator", "annotation_version": "generated-v1",
            })
    (output / "README.md").write_text(
        "# Tracking 3.0 双路合成演示\n\n"
        "这是用于导入、时间字段、GT CSV 和证据包流程的合成素材。目标外观、时间和身份由脚本生成，不能作为 ReID、检测、遮挡或部署效果基准。\n\n"
        "文件：demo_cam_a.mp4、demo_cam_b.mp4、media_manifest.csv、target_events.csv。\n",
        encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
