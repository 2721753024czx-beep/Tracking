import contextlib
import csv
import io
import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from tools.generate_demo_dataset import main as generate_demo
from tools.prepare_event_review import _sample_frames, main as prepare_review


class EventReviewPack(unittest.TestCase):
    def make_source(self, root):
        source = root / "source"
        with patch("sys.argv", ["generate_demo_dataset", "--output", str(source)]), contextlib.redirect_stdout(io.StringIO()):
            generate_demo()
        return source

    def prepare(self, source, output, *extra):
        with contextlib.redirect_stdout(io.StringIO()):
            prepare_review(["--input", str(source), "--output", str(output), *extra])

    def test_decoded_pts_source_hash_and_empty_gt_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_source(root)
            output = root / "review"
            self.prepare(source, output, "--sample-every-s", "2")
            result = json.loads((output / "review-manifest.json").read_text())
            self.assertEqual(result["annotation_status"], "not_started")
            self.assertEqual(result["review_points"], 8)
            self.assertEqual([s["sha_status"] for s in result["sources"]], ["valid", "valid"])
            with (output / "review_points.csv").open(newline="", encoding="utf-8") as handle:
                points = list(csv.DictReader(handle))
            self.assertEqual([int(p["frame_index"]) for p in points[:4]], [0, 20, 40, 60])
            for point in points:
                reconstructed = int(point["raw_pts"]) * float(Fraction(point["time_base"])) - float(point["origin_pts_s"])
                self.assertAlmostEqual(float(point["actual_media_s"]), reconstructed)
                self.assertEqual(point["frame_index_base"], "0")
            with (output / "target_events.template.csv").open(newline="", encoding="utf-8") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])
            with Image.open(output / "source-001-contact-001.jpg") as image:
                self.assertEqual(image.width, 1472)
            with self.assertRaises(SystemExit):
                self.prepare(source, output)
            self.assertEqual((output / "review-manifest.json").read_text(), json.dumps(result, ensure_ascii=False, indent=2) + "\n")

    def test_hash_mismatch_refuses_before_producing_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_source(root)
            manifest = source / "media_manifest.csv"
            with manifest.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                fields, rows = reader.fieldnames, list(reader)
            rows[0]["sha256"] = "0" * 64
            with manifest.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader(); writer.writerows(rows)
            output = root / "review"
            with self.assertRaisesRegex(SystemExit, "SHA"):
                self.prepare(source, output)
            self.assertFalse(output.exists())

    def test_missing_pts_does_not_invent_frame_time(self):
        container = SimpleNamespace(streams=SimpleNamespace(video=[object()]),
                                    decode=lambda stream: iter([SimpleNamespace(pts=None, time_base=None)]))
        with patch("tools.prepare_event_review.av.open") as opened:
            opened.return_value.__enter__.return_value = container
            with self.assertRaisesRegex(ValueError, "PTS/time_base"):
                _sample_frames(Path("untimed.mp4"), [0.0])

    def test_nonfinite_sampling_is_rejected(self):
        for argument, value in [("--sample-every-s", "nan"), ("--sample-every-s", "inf"),
                                ("--duration-s", "nan"), ("--duration-s", "-1")]:
            with self.subTest(argument=argument, value=value), self.assertRaises(SystemExit):
                prepare_review(["--input", "absent", "--output", "absent-output", argument, value])


if __name__ == "__main__":
    unittest.main()
