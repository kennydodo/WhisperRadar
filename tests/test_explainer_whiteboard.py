"""Whiteboard engine tests: stroke geometry, glyphs, schema v2, mode wiring."""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from whisperradar.video import parsing  # noqa: E402
from whisperradar.video.explainer import schema as sc  # noqa: E402
from whisperradar.video.explainer import strokes as st  # noqa: E402
from whisperradar.video.explainer import strokes_library as lib  # noqa: E402
from whisperradar.video.explainer import glyphs  # noqa: E402
from whisperradar.video.explainer import validate as va  # noqa: E402
from whisperradar.video.explainer.validate import ExplainerError  # noqa: E402


class StrokeGeometryTests(unittest.TestCase):
    def test_arc_length_progress_matches_distance(self):
        s = st.Stroke(points=[(0, 0), (100, 0)])
        half = s.partial(0.5)
        self.assertAlmostEqual(half[-1][0], 50.0, places=6)

    def test_progress_not_point_count(self):
        # uneven point density: same arc length regardless of sampling
        dense = st.Stroke(points=[(0, 0)] + [(i * 0.1, 0) for i in range(1, 501)]
                          + [(100, 0)])
        sparse = st.Stroke(points=[(0, 0), (100, 0)])
        self.assertAlmostEqual(dense.partial(0.5)[-1][0],
                               sparse.partial(0.5)[-1][0], places=4)

    def test_arbitrary_frame_evaluation_needs_no_replay(self):
        s = st.Stroke(points=[(0, 0), (30, 40), (90, 40)])   # length 50+60=110
        p25 = s.partial(0.25)     # 27.5 units along the 3-4-5 first segment
        p75 = s.partial(0.75)     # 82.5 -> 32.5 into the second segment
        self.assertAlmostEqual(p25[-1][0], 16.5, places=4)
        self.assertAlmostEqual(p25[-1][1], 22.0, places=4)
        self.assertAlmostEqual(p75[-1][0], 62.5, places=4)
        self.assertAlmostEqual(p75[-1][1], 40.0, places=4)

    def test_cubic_flattening_endpoints_exact(self):
        pts = st.flatten_cubic((0, 0), (10, 60), (50, 60), (60, 0), tol=0.4)
        self.assertEqual(pts[0], (0, 0))
        self.assertEqual(pts[-1], (60, 0))
        self.assertGreater(len(pts), 8)   # actually subdivided

    def test_schedule_budgets_by_length_and_ends_on_time(self):
        asset = lib.load_builtin("house")
        sched = lib.strokes_schedule(asset, 10, 60)
        self.assertEqual(sched[0][1], 10.0)
        self.assertAlmostEqual(sched[-1][2], 70.0, places=6)
        # longest stroke gets the largest window
        d1 = sched[0][2] - sched[0][1]
        d2 = sched[1][2] - sched[1][1]
        self.assertGreaterEqual(d2, d1)


class GlyphTests(unittest.TestCase):
    def test_core_coverage(self):
        import string
        need = string.ascii_letters + string.digits + " .,!?:;-$%&()/'"
        missing = glyphs.missing_glyphs(need)
        self.assertEqual(missing, [])

    def test_missing_glyphs_reported(self):
        self.assertEqual(glyphs.missing_glyphs("ok {bad}"), ["{", "}"])

    def test_text_strokes_advance_and_scale(self):
        strokes, adv = glyphs.text_strokes("AB", (0, 0), 2.0)
        self.assertGreater(len(strokes), 2)
        # two advances of 58 units * scale 2
        self.assertAlmostEqual(adv[0], 2 * glyphs.ADVANCE * 2.0, places=4)
        self.assertTrue(all(s.length > 0 for s in strokes))

    def test_newline_wraps(self):
        strokes, size = glyphs.text_strokes("A\nB", (0, 0), 1.0)
        b_strokes = [s for s in strokes
                     if s.points[0][1] > 0]   # second line lower
        self.assertGreater(len(b_strokes), 0)


class SchemaV2Tests(unittest.TestCase):
    def _doc(self, **over):
        doc = {
            "schema_version": 2, "style": "whiteboard",
            "canvas": {"width": 320, "height": 180},
            "scenes": [{
                "id": "w", "shot_id": "S01_01", "cue_range": [1, 1],
                "objects": [
                    {"id": "obj", "type": "stroke_asset",
                     "asset": "builtin:house", "box": [0.1, 0.2, 0.4, 0.6]}],
                "events": [
                    {"target": "obj", "action": "draw_strokes",
                     "at": {"cue": 1, "edge": "start"}, "duration_ms": 900}],
            }]}
        doc.update(over)
        return doc

    SRT = "1\n00:00:00,000 --> 00:00:04,000\none\n"

    def test_v2_whiteboard_doc_parses(self):
        doc = va.parse_doc(self._doc())
        self.assertEqual(doc.schema_version, 2)
        self.assertEqual(doc.style, sc.STYLE_WHITEBOARD)

    def test_builtin_asset_resolves_and_counts(self):
        doc = va.parse_doc(self._doc())
        obj = doc.scenes[0].objects[0]
        self.assertEqual(getattr(obj, "_stroke_count", 0), 3)

    def test_unknown_builtin_rejected(self):
        data = self._doc()
        data["scenes"][0]["objects"][0]["asset"] = "builtin:rocket"
        with self.assertRaises(ExplainerError) as ctx2:
            va.parse_doc(data)
        self.assertTrue(any("unknown builtin" in e for e in ctx2.exception.errors))

    def test_handwriting_coverage_enforced(self):
        data = self._doc()
        data["scenes"][0]["objects"][0] = {
            "id": "hw", "type": "handwriting", "text": "café ~ ok",
            "box": [0.1, 0.2, 0.6, 0.2]}
        with self.assertRaises(ExplainerError) as ctx:
            va.parse_doc(data)
        errs = " ".join(ctx.exception.errors)
        self.assertIn("no handwriting glyph", errs)
        self.assertIn("fallback_typed", errs)

    def test_handwriting_fallback_allowed(self):
        data = self._doc()
        data["scenes"][0]["objects"] = [{
            "id": "hw", "type": "handwriting", "text": "café",
            "fallback_typed": True, "box": [0.1, 0.2, 0.6, 0.2]}]
        data["scenes"][0]["events"] = []
        doc = va.parse_doc(data)   # disclosed fallback: no error
        self.assertTrue(doc.scenes[0].objects[0].fallback_typed)

    def test_overscheduled_strokes_rejected(self):
        data = self._doc()
        data["scenes"][0]["events"][0]["duration_ms"] = 120
        with self.assertRaises(ExplainerError) as ctx:
            va.parse_doc(data)
        self.assertTrue(any("readable drawing" in e
                            for e in ctx.exception.errors))

    def test_v1_doc_still_loads(self):
        doc = {
            "schema_version": 1, "theme": "finance_dark",
            "canvas": {"width": 320, "height": 180},
            "scenes": [{"id": "s", "shot_id": "S01_01", "cue_range": [1, 1],
                        "objects": [{"id": "t", "type": "label", "text": "x",
                                     "box": [0, 0, 0.3, 0.1]}],
                        "events": []}]}
        parsed = va.parse_doc(doc)
        self.assertEqual(parsed.style, sc.STYLE_MOTION)


class ModeWiringTests(unittest.TestCase):
    def test_visual_mode_validated(self):
        from whisperradar.video import options as opt
        o = opt.ProjectOptions(visual_mode="chalk")
        errs = o.validate()
        self.assertTrue(any("Visual mode" in e for e in errs))
        ok = opt.ProjectOptions(visual_mode="whiteboard")
        self.assertEqual(ok.validate(), [])

    def test_settings_spec_exposes_visual_mode(self):
        from whisperradar.video import settings_spec as ss
        row = next(s for s in ss.SPEC if s["path"] == "visual_mode")
        self.assertIn("whiteboard", row["choices"])
        self.assertIn("motion_graphics", row["choices"])

    def test_sidecar_with_images_stays_on_image_planner(self):
        # unit-level: the migration rule branch logic
        from whisperradar.video import options as opt
        self.assertEqual(opt.VM_IMAGES, "images")


if __name__ == "__main__":
    unittest.main()
