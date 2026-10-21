import json
import tempfile
import unittest
from pathlib import Path

from tools.evaluate_events import (
    evaluate, load_candidates, load_ground_truth, load_identity_reviews, review_template,
)


def event(**changes):
    value = {"_row": 2, "global_person_id": "p1", "camera_id": "cam-a", "source_path": "a.mp4",
             "start_media_s": 10.0, "end_media_s": 12.0, "visibility": "visible",
             "identity_evidence": "independent source replay", "annotator": "fixture-reviewer",
             "annotation_version": "controlled-v1"}
    value.update(changes)
    return value


def candidate(ident="c1", **changes):
    value = {"id": ident, "camera_id": "cam-a", "source_path": "a.mp4", "start": 10.0, "end": 12.0,
             "score": .8, "status": "pending"}
    value.update(changes)
    return value


def review(c, status="target", **changes):
    value = {"candidate_id": c["id"], "identity_status": status,
             "global_person_id": "p1" if status == "target" else "", "annotator": "fixture-reviewer",
             "evidence": "controlled test label, not real footage GT", "annotation_version": "controlled-v1",
             "verified_intervals": [{"start_media_s": c["start"], "end_media_s": c["end"]}] if status == "target" else []}
    value.update(changes)
    return value


class EventEvaluation(unittest.TestCase):
    def test_time_overlap_and_application_confirmation_do_not_prove_identity(self):
        c = candidate(status="confirmed", global_person_id="p1")
        result = evaluate([event()], [c], top_ks=(1,), tolerance=0)
        self.assertEqual(result["temporal_top_k_hits"], {"1": 1})
        self.assertEqual(result["top_k_hit_rate"], {"1": None})
        self.assertIsNone(result["events_without_match"])
        self.assertEqual(result["metric_status"], "identity_review_required")

    def test_same_time_wrong_person_is_not_rank_one_hit(self):
        wrong, right = candidate("wrong", score=.9), candidate("right", score=.8)
        result = evaluate([event()], [wrong, right], top_ks=(1, 2), tolerance=0,
                          identity_reviews=[review(wrong, "other"), review(right)])
        self.assertEqual(result["metric_status"], "ready")
        self.assertEqual(result["temporal_top_k_hits"], {"1": 1, "2": 1})
        self.assertEqual(result["top_k_hits"], {"1": 0, "2": 1})
        self.assertEqual(result["per_event"][0]["rank"], 2)

    def test_background_overlap_is_not_target_hit(self):
        c = candidate()
        result = evaluate([event()], [c], top_ks=(1,), tolerance=0,
                          identity_reviews=[review(c, "background")])
        self.assertEqual(result["temporal_top_k_overlap_rate"], {"1": 1.0})
        self.assertEqual(result["top_k_hit_rate"], {"1": 0.0})
        self.assertEqual(result["events_without_match"], 1)

    def test_camera_and_time_remain_diagnostic_when_source_unknown(self):
        c = candidate(source_path=None, media_path="uploads/random.mp4")
        result = evaluate([event()], [c], top_ks=(1,), tolerance=0,
                          identity_reviews=[review(c, "other")])
        self.assertEqual(result["temporal_top_k_hits"], {"1": 1})
        self.assertEqual(result["per_event"][0]["temporal_match_basis"], "camera_and_time_only")
        self.assertEqual(result["metric_status"], "candidate_metadata_incomplete")
        self.assertIsNone(result["top_k_hit_rate"]["1"])

    def test_camera_mismatch_is_not_temporal_or_identity_hit(self):
        c = candidate(camera_id="cam-b")
        result = evaluate([event()], [c], top_ks=(1,), tolerance=0,
                          identity_reviews=[review(c)])
        self.assertEqual(result["temporal_top_k_hits"], {"1": 0})
        self.assertEqual(result["top_k_hits"], {"1": 0})

    def test_same_basename_in_different_sessions_does_not_match(self):
        c = candidate(source_path="session-b/a.mp4")
        result = evaluate([event(source_path="session-a/a.mp4")], [c], top_ks=(1,), tolerance=0,
                          identity_reviews=[review(c)])
        self.assertEqual(result["top_k_hits"], {"1": 0})
        self.assertEqual(result["temporal_top_k_hits"], {"1": 0})

    def test_partial_review_does_not_publish_rate(self):
        a, b = candidate("a"), candidate("b", score=.7)
        result = evaluate([event()], [a, b], top_ks=(1,), tolerance=0,
                          identity_reviews=[review(a)])
        self.assertEqual(result["metric_status"], "identity_review_incomplete")
        self.assertIsNone(result["top_k_hit_rate"]["1"])
        self.assertEqual(result["verified_hits_so_far"], {"1": 1})
        self.assertIsNone(result["per_event"][0]["matched"])

    def test_unknown_gt_time_is_unassessable_not_a_model_miss(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gt.csv").write_text("global_person_id,camera_id,source_path,start_media_s,end_media_s\np1,cam-a,a.mp4,,\n")
            (root / "candidates.json").write_text(json.dumps([candidate()]))
            result = evaluate(load_ground_truth(root / "gt.csv"), load_candidates(root / "candidates.json"), top_ks=(1,))
            self.assertEqual(result["metric_status"], "incomplete_ground_truth")
            self.assertEqual(result["temporal_events_unassessable"], 1)
            self.assertIsNone(result["temporal_top_k_overlap_rate"]["1"])
            self.assertIsNone(result["top_k_hit_rate"]["1"])
            self.assertIsNone(result["events_without_match"])

    def test_uncertain_gt_and_candidate_identities_remain_unavailable(self):
        c = candidate()
        result = evaluate([event(visibility="uncertain")], [c], top_ks=(1,),
                          identity_reviews=[review(c)])
        self.assertEqual(result["metric_status"], "incomplete_ground_truth")
        result = evaluate([event()], [c], top_ks=(1,), identity_reviews=[review(c, "uncertain")])
        self.assertEqual(result["metric_status"], "identity_review_incomplete")
        self.assertIsNone(result["top_k_hits"]["1"])

    def test_missing_or_invalid_interval_end_is_not_invented(self):
        c = candidate()
        result = evaluate([event(end_media_s=None)], [c], top_ks=(1,), identity_reviews=[review(c)])
        self.assertEqual(result["metric_status"], "incomplete_ground_truth")
        self.assertEqual(result["temporal_events_unassessable"], 1)
        c = candidate(end="invalid")
        result = evaluate([event()], [c], top_ks=(1,), identity_reviews=[review(c, "other")])
        self.assertEqual(result["metric_status"], "candidate_metadata_incomplete")
        self.assertIsNone(result["top_k_hit_rate"]["1"])

    def test_gt_bad_numbers_raise_instead_of_becoming_point_event(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gt.csv"
            for value in ("invalid", "nan", "inf"):
                path.write_text(f"camera_id,start_media_s,end_media_s\ncam-a,10,{value}\n")
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "有限秒数"):
                    load_ground_truth(path)

    def test_multiple_gt_identities_require_selected_query_target(self):
        c = candidate()
        with self.assertRaisesRegex(ValueError, "target-id"):
            evaluate([event(), event(global_person_id="p2")], [c])
        result = evaluate([event(), event(global_person_id="p2")], [c], top_ks=(1,),
                          target_id="p1", identity_reviews=[review(c)])
        self.assertEqual(result["events_input_total"], 2)
        self.assertEqual(result["events_total"], 1)
        self.assertEqual(result["top_k_hit_rate"]["1"], 1.0)

    def test_score_zero_sorts_above_negative_score(self):
        wrong, right = candidate("wrong", score=-.1), candidate("right", score=0)
        result = evaluate([event()], [wrong, right], top_ks=(1,), tolerance=0,
                          identity_reviews=[review(wrong, "other"), review(right)])
        self.assertEqual(result["top_k_hits"]["1"], 1)

    def test_identity_hit_uses_verified_segment_not_entire_merged_track(self):
        c = candidate(start=0.0, end=12.0)
        r = review(c, verified_intervals=[{"start_media_s": 0, "end_media_s": 1}])
        result = evaluate([event()], [c], top_ks=(1,), tolerance=0, identity_reviews=[r])
        self.assertEqual(result["temporal_top_k_hits"]["1"], 1)
        self.assertEqual(result["top_k_hits"]["1"], 0)
        with self.assertRaisesRegex(ValueError, "区间"):
            evaluate([event()], [c], identity_reviews=[review(c, verified_intervals=[{"start_media_s": 20, "end_media_s": 21}])])

    def test_unknown_duplicate_and_evidenceless_review_rejected(self):
        c = candidate()
        invalid_lists = [[review(c), review(c)], [review(candidate("absent"))],
                         [review(c, annotator="")], [review(c, global_person_id="p2")],
                         [review(c, identity_status="guess")], [review(c, verified_intervals=[])]]
        for reviews in invalid_lists:
            with self.subTest(reviews=reviews), self.assertRaises(ValueError):
                evaluate([event()], [c], identity_reviews=reviews)
        with self.assertRaises(ValueError):
            evaluate([event()], [c, c], identity_reviews=[review(c)])

    def test_review_file_is_bound_to_candidate_and_gt_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gt, candidates, path = root / "gt.csv", root / "candidates.json", root / "reviews.json"
            gt.write_text("global_person_id\np1\n")
            candidates.write_text(json.dumps([candidate()]))
            template = review_template([event()], [candidate()], gt, candidates)
            self.assertEqual(template["reviews"][0]["identity_status"], "unreviewed")
            template["reviews"] = [review(candidate())]
            path.write_text(json.dumps(template))
            reviews, target = load_identity_reviews(path, gt, candidates)
            self.assertEqual(target, "p1")
            self.assertEqual(len(reviews), 1)
            for changed in (candidates, gt):
                original = changed.read_text()
                changed.write_text(original + "\n")
                with self.assertRaisesRegex(ValueError, "不一致"):
                    load_identity_reviews(path, gt, candidates)
                changed.write_text(original)

    def test_nonfinite_tolerance_fractional_k_and_empty_gt(self):
        for tolerance in (float("nan"), float("inf"), -1):
            with self.subTest(tolerance=tolerance), self.assertRaises(ValueError):
                evaluate([event()], [candidate()], tolerance=tolerance)
        for top_ks in ((0,), (1.5,), (), (True,)):
            with self.subTest(top_ks=top_ks), self.assertRaises(ValueError):
                evaluate([event()], [candidate()], top_ks=top_ks)
        result = evaluate([], [candidate()], top_ks=(1,))
        self.assertEqual(result["metric_status"], "no_ground_truth")
        self.assertIsNone(result["top_k_hit_rate"]["1"])

    def test_excluded_candidate_is_explicit_in_report(self):
        wrong, right = candidate("wrong", score=.9, status="rejected"), candidate("right")
        result = evaluate([event()], [wrong, right], top_ks=(1,), exclude_status=("rejected",),
                          identity_reviews=[review(right)])
        self.assertEqual(result["candidates_excluded"], 1)
        self.assertEqual(result["exclude_status"], ["rejected"])
        self.assertEqual(result["top_k_hit_rate"]["1"], 1.0)


if __name__ == "__main__":
    unittest.main()
