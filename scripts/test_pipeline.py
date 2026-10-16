#!/usr/bin/env python3
"""Synthetic forward tests only: no provider calls and no claim of live visual QA."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image

import tile_pipeline as pipeline
from compose import compose


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tile-pipeline-test-")
        self.root = Path(self.temp.name)
        self.counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, *args):
        parsed = pipeline.parser().parse_args([str(v) for v in args])
        with redirect_stdout(io.StringIO()):
            return parsed.func(parsed)

    def json_file(self, name, obj):
        path = self.root / name
        path.write_text(json.dumps(obj), encoding="utf-8")
        return path

    def plan(self, scale=2, size=(12, 8), native="16x12", approve=True):
        self.counter += 1
        tag = f"case_{self.counter}"
        source = self.root / (tag + ".png")
        Image.new("RGBA", size, (180, 90, 40, 128)).save(source)
        analysis = {"inspected": True, "coordinate_system": "exif_oriented_source_pixels",
                    "summary": "Synthetic flat-color alpha fixture, not a generated image", "style": "flat",
                    "palette": ["brown"], "materials": {"flat": "Keep the same flat brown color"},
                    "regions": [{"id": "background", "box": [0, 0, *size], "kind": "background",
                                 "material": "flat", "object": "fixture", "seam_avoid": False, "detail": "Flat synthetic fixture"}]}
        analysis_path = self.json_file(tag + "_analysis.json", analysis)
        run = self.root / tag
        plan = self.invoke("plan", "--image", source, "--scale", scale, "--native", native,
                           "--margin", 0, "--analysis", analysis_path, "--run", run)
        if approve:
            review = {"notes": "Synthetic fixture setup only; not a human/live visual review",
                      "checks": {key: True for key in ("image_content", "tile_extents", "critical_objects", "shared_prompts", "pilot_sequence")},
                      "warnings_acknowledged": plan["warnings"]}
            self.invoke("approve-plan", run, "--review-json", self.json_file(tag + "_plan_review.json", review))
        return run, plan

    def register(self, run, plan, tid, size=None, crop=None, note=None):
        tile = next(t for t in plan["tiles"] if t["id"] == tid)
        L, T, R, B = tile["target_box"]
        size = size or (R - L, B - T)
        image = self.root / f"mock_output_{self.counter}_{tid}_{len(list(self.root.glob('mock_output*')))}.png"
        Image.new("RGBA", size, (180, 90, 40, 128)).save(image)
        receipt = self.json_file(image.stem + "_receipt.json", {
            "tool": "image_gen__imagegen", "called_at": "SYNTHETIC-NO-TOOL-CALL",
            "output_path": str(image), "test_fixture": True,
        })
        args = ["register", run, "--tile", tid, "--image", image, "--receipt", receipt]
        if crop is not None:
            args += ["--crop", ",".join(map(str, crop))]
        if note is not None:
            args += ["--placement-note", note]
        return self.invoke(*args)

    def review(self, run, tid, omit=None):
        entry = pipeline.read(run / "state.json")["tiles"][tid]
        review = {"output": entry["output"], "notes": "Synthetic test assessment; no live visual claim",
                  "checks": {key: True for key in ("composition", "detail", "geometry", "color", "text", "borders", "alpha")}}
        if omit:
            del review["checks"][omit]
        self.invoke("review", run, "--tile", tid, "--review-json", self.json_file(f"{self.counter}_{tid}_review.json", review))

    def complete(self):
        run, plan = self.plan()
        for tid in plan["sequence"]:
            self.assertTrue(self.register(run, plan, tid))
            self.review(run, tid)
        with redirect_stdout(io.StringIO()):
            compose(run)
        seam_index = pipeline.read(run / "seam_reviews/index.json")
        review = {"notes": "Synthetic end-to-end test only, not actual generated-image acceptance",
                  "checks": {key: True for key in ("whole_image", "critical_objects", "all_seams", "text", "color", "alpha", "detail_comparison")},
                  "seams": {s["file"]: {"passed": True, "notes": "Synthetic flat-color seam"} for s in seam_index},
                  "limitations": ["Synthetic fixture; no ImageGen call or real visual assessment"]}
        return run, plan, review

    def test_integer_and_noninteger_budget_and_full_coverage(self):
        for scale in (2, 2.5, 10, 1.25):
            with self.subTest(scale=scale):
                run, plan = self.plan(scale=scale, size=(31, 23), native="40x32")
                self.assertEqual(plan["target_size"], [round(31 * scale), round(23 * scale)])
                width, height = plan["target_size"]
                coverage = np.zeros((height, width), np.uint16)
                for tile in plan["tiles"]:
                    L, T, R, B = tile["target_box"]
                    self.assertLessEqual(R-L, plan["usable_native"][0])
                    self.assertLessEqual(B-T, plan["usable_native"][1])
                    coverage[T:B, L:R] += 1
                self.assertTrue(np.all(coverage > 0))
                for axis, crop, overlap in zip(("x", "y"), plan["crop_size"], plan["minimum_overlap"]):
                    starts = plan["content_adjusted_starts"][axis]
                    self.assertTrue(all(b-a <= crop-overlap for a, b in zip(starts, starts[1:])))

    def test_axis_formula_many_sizes(self):
        for length in range(2, 60):
            for crop in range(2, min(length, 14)+1):
                overlap = max(1, crop // 4)
                starts = pipeline.axis_starts(length, crop, overlap)
                self.assertEqual(starts[0], 0)
                self.assertEqual(starts[-1]+crop, length)
                self.assertTrue(all(b-a <= crop-overlap for a, b in zip(starts, starts[1:])))

    def test_content_avoidance_shifts_inside_constraints(self):
        starts = [0, 8, 16, 24]
        regions = [{"box": [7, 0, 10, 10], "seam_avoid": True}]
        adjusted = pipeline.adapt_axis(starts, 12, 2, 36, 0, regions, np.zeros(36))
        self.assertNotEqual(adjusted, starts)
        self.assertEqual((adjusted[0], adjusted[-1]), (0, 24))
        self.assertTrue(all(b-a <= 10 for a, b in zip(adjusted, adjusted[1:])))

    def test_plan_cannot_reset_existing_run(self):
        run, _ = self.plan()
        state = (run / "state.json").read_bytes()
        with self.assertRaises(FileExistsError):
            self.invoke("plan", "--image", run / "source.png", "--scale", 2, "--native", "16x12", "--analysis", run / "analysis.json", "--run", run)
        self.assertEqual(state, (run / "state.json").read_bytes())

    def test_plan_review_blocks_registration(self):
        run, plan = self.plan(approve=False)
        with self.assertRaisesRegex(ValueError, "approve-plan"):
            self.register(run, plan, plan["pilot_ids"][0])

    def test_pilots_block_nonpilot_registration(self):
        run, plan = self.plan()
        tid = next(t for t in plan["sequence"] if t not in plan["pilot_ids"])
        with self.assertRaisesRegex(ValueError, "All pilots"):
            self.register(run, plan, tid)
        self.assertEqual(pipeline.read(run / "state.json")["tiles"][tid]["attempts"], [])

    def test_register_reads_actual_size_and_rejects_aspect(self):
        run, plan = self.plan()
        tid = plan["pilot_ids"][0]
        self.assertFalse(self.register(run, plan, tid, size=(2, 2)))
        self.assertEqual(pipeline.read(run / "state.json")["tiles"][tid]["native_size"], [2, 2])
        self.assertFalse(self.register(run, plan, tid, size=(40, 12)))
        entry = pipeline.read(run / "state.json")["tiles"][tid]
        self.assertEqual(entry["status"], "qa_failed")
        self.assertTrue(any("aspect" in v for v in entry["geometry_errors"]))
        self.assertEqual(len(entry["attempts"]), 2)

    def test_crop_requires_evidence_note(self):
        run, plan = self.plan()
        with self.assertRaisesRegex(ValueError, "evidence"):
            self.register(run, plan, plan["pilot_ids"][0], size=(20, 16), crop=[0, 0, 16, 12])

    def test_review_missing_check_fails(self):
        run, plan = self.plan()
        tid = plan["pilot_ids"][0]
        self.register(run, plan, tid)
        with self.assertRaisesRegex(ValueError, "alpha"):
            self.review(run, tid, omit="alpha")
        self.assertEqual(pipeline.read(run / "state.json")["tiles"][tid]["status"], "qa_failed")

    def test_visual_review_can_resume_without_regeneration(self):
        run, plan = self.plan()
        tid = plan["pilot_ids"][0]
        self.register(run, plan, tid)
        with self.assertRaises(ValueError):
            self.review(run, tid, omit="alpha")
        self.review(run, tid)
        entry = pipeline.read(run / "state.json")["tiles"][tid]
        self.assertEqual(entry["status"], "qa_passed")
        self.assertEqual(len(entry["attempts"]), 1)

    def test_failure_calls_count_toward_attempt_limit(self):
        run, plan = self.plan()
        tid = plan["pilot_ids"][0]
        receipt = self.json_file("tool_failure.json", {"tool": "image_gen__imagegen", "called_at": "SYNTHETIC-NO-TOOL-CALL", "error": "synthetic service failure"})
        for _ in range(plan["max_attempts"]):
            self.invoke("failure", run, "--tile", tid, "--receipt", receipt)
        entry = pipeline.read(run / "state.json")["tiles"][tid]
        self.assertEqual(len(entry["attempts"]), plan["max_attempts"])
        self.assertEqual(entry["status"], "tool_failed")
        with self.assertRaisesRegex(ValueError, "Attempt limit"):
            self.register(run, plan, tid)
        with self.assertRaisesRegex(ValueError, "Attempt limit"):
            self.invoke("failure", run, "--tile", tid, "--receipt", receipt)

    def test_successful_retry_preserves_failure_record(self):
        run, plan = self.plan()
        tid = plan["pilot_ids"][0]
        receipt = self.json_file("first_tool_failure.json", {"tool": "image_gen__imagegen", "called_at": "SYNTHETIC-NO-TOOL-CALL", "error": "synthetic timeout"})
        self.invoke("failure", run, "--tile", tid, "--receipt", receipt)
        self.register(run, plan, tid)
        self.review(run, tid)
        entry = pipeline.read(run / "state.json")["tiles"][tid]
        self.assertEqual([a["number"] for a in entry["attempts"]], [1, 2])
        self.assertEqual(entry["attempts"][0]["status"], "tool_failed")
        self.assertTrue((run / entry["attempts"][0]["receipt"]).is_file())
        self.assertEqual(entry["status"], "qa_passed")

    def test_finish_requires_every_seam(self):
        run, _, review = self.complete()
        review["seams"].pop(next(iter(review["seams"])))
        with self.assertRaisesRegex(ValueError, "Every seam"):
            self.invoke("finish", run, "--review-json", self.json_file("missing_review.json", review), "--out", self.root / "delivery")
        self.assertFalse((run / "final_qa.json").exists())

    def test_finish_requires_seam_file(self):
        run, _, review = self.complete()
        (run / next(iter(review["seams"]))).unlink()
        with self.assertRaises((ValueError, OSError)):
            self.invoke("finish", run, "--review-json", self.json_file("missing_file_review.json", review), "--out", self.root / "delivery")
        self.assertFalse((run / "final_qa.json").exists())

    def test_finish_rejects_shortened_seam_index_even_with_matching_review(self):
        run, _, review = self.complete()
        index = pipeline.read(run / "seam_reviews/index.json")
        removed = index.pop()
        del review["seams"][removed["file"]]
        (run / "seam_reviews/index.json").write_text(json.dumps(index), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Seam index is incomplete"):
            self.invoke("finish", run, "--review-json", self.json_file("shortened_index_review.json", review), "--out", self.root / "delivery")
        self.assertFalse((run / "final_qa.json").exists())

    def test_finish_rejects_stale_composite_after_valid_output_change(self):
        run, plan, review = self.complete()
        state = pipeline.read(run / "state.json")
        tid = plan["sequence"][0]
        entry = state["tiles"][tid]
        replacement = f"generated/{tid}/replacement.png"
        Image.new("RGBA", tuple(entry["native_size"]), (160, 80, 35, 128)).save(run / replacement)
        entry["output"] = replacement
        entry["visual_review"]["output"] = replacement
        entry["visual_review"]["notes"] = "Synthetic reviewed replacement; previous composite is deliberately stale"
        (run / "state.json").write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Composition report is stale"):
            self.invoke("finish", run, "--review-json", self.json_file("stale_composite_review.json", review), "--out", self.root / "delivery")
        self.assertFalse((run / "final_qa.json").exists())

    def test_failed_delivery_does_not_mark_accepted(self):
        run, _, review = self.complete()
        (run / "preview.jpg").unlink()
        with self.assertRaises((ValueError, OSError)):
            self.invoke("finish", run, "--review-json", self.json_file("failed_delivery_review.json", review), "--out", self.root / "delivery")
        self.assertFalse((run / "final_qa.json").exists())

    def test_synthetic_end_to_end(self):
        run, plan, review = self.complete()
        out = self.root / "delivery"
        self.invoke("finish", run, "--review-json", self.json_file("final_review.json", review), "--out", out)
        self.assertTrue((out / "generation_records.zip").is_file())
        self.assertTrue((out / "delivery_complete.json").is_file())
        self.assertTrue((run / "final_qa.json").is_file())
        with Image.open(out / "composite.png") as image:
            self.assertEqual(list(image.size), plan["target_size"])
            self.assertEqual(image.mode, "RGBA")
            self.assertTrue(image.info.get("icc_profile"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
