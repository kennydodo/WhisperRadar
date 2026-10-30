"""Recipes: a meaning-level plan.json becomes a valid, well-timed explainer.json."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from whisperradar.video import parsing  # noqa: E402
from whisperradar.video.explainer import cli, recipes  # noqa: E402
from whisperradar.video.explainer import validate as va  # noqa: E402
from whisperradar.video.explainer.validate import ExplainerError  # noqa: E402

SRT = "".join(f"{i}\n00:00:{(i - 1) * 6:02d},000 --> 00:00:{i * 6:02d},000\nline {i}\n\n"
              for i in range(1, 5))


def _cues():
    return parsing.parse_srt(SRT)


def _plan(**scene):
    base = {"id": "s1", "cue_range": [1, 4], "layout": "row", "title": "A title",
            "items": [{"label": "One", "icon": "coins"}, {"label": "Two", "icon": "sprout"},
                      {"label": "Three", "icon": "clock"}]}
    base.update(scene)
    return {"theme": "chalkboard", "scenes": [base]}


def _errs(plan):
    try:
        recipes.build(plan, _cues())
    except ExplainerError as e:
        return " ".join(e.errors)
    return ""


class LayoutTests(unittest.TestCase):
    def test_every_layout_builds_a_valid_document(self):
        plans = {
            "row": _plan(),
            "steps": _plan(layout="steps"),
            "timeline": _plan(layout="timeline"),
            "stack": _plan(layout="stack"),
            "hero": _plan(layout="hero", items=[{"label": "Big idea", "icon": "lightbulb"}]),
            "compare": _plan(layout="compare", items=[], sides=[
                {"header": "A", "points": ["x", "y"]}, {"header": "B", "points": ["z"]}]),
        }
        for name, plan in plans.items():
            doc = recipes.build(plan, _cues())
            parsed = va.parse_doc(doc)                      # same rules as a hand-written file
            self.assertTrue(parsed.scenes[0].objects, name)

    def test_nothing_is_placed_outside_the_canvas_or_over_the_subtitles(self):
        for layout in ("row", "steps", "timeline", "stack"):
            doc = recipes.build(_plan(layout=layout, footer={"text": "Done and done"}), _cues())
            for o in doc["scenes"][0]["objects"]:
                if "box" in o:
                    x, y, w, h = o["box"]
                    self.assertGreaterEqual(x, -0.001, (layout, o["id"]))
                    self.assertLessEqual(x + w, 1.001, (layout, o["id"]))
                    self.assertLessEqual(y + h, 0.9, (layout, o["id"], o["box"]))

    def test_build_is_deterministic(self):
        self.assertEqual(json.dumps(recipes.build(_plan(), _cues())),
                         json.dumps(recipes.build(_plan(), _cues())))

    def test_items_without_a_cue_are_spread_over_the_scene(self):
        doc = recipes.build(_plan(), _cues())
        cues = {e["target"]: e["at"]["cue"] for e in doc["scenes"][0]["events"]}
        self.assertEqual(cues["s1_ic0"], 1)
        self.assertGreater(cues["s1_ic2"], cues["s1_ic0"])


class ScheduleTests(unittest.TestCase):
    def test_the_pen_never_overlaps_itself_and_ends_inside_the_scene(self):
        doc = recipes.build(_plan(footer={"text": "end"}), _cues())
        cue_start = {c.index: c.start_seconds * 1000 for c in _cues()}
        spans = sorted((cue_start[e["at"]["cue"]] + e["offset_ms"],
                        cue_start[e["at"]["cue"]] + e["offset_ms"] + e["duration_ms"])
                       for e in doc["scenes"][0]["events"] if e["action"] == "draw_strokes")
        for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
            self.assertLessEqual(a1, b0 + 1)
        self.assertLessEqual(spans[-1][1], 24000)

    def test_too_much_for_the_time_is_a_readable_error(self):
        short = "1\n00:00:00,000 --> 00:00:02,000\nhi\n"
        plan = _plan(cue_range=[1, 1])
        with self.assertRaises(ExplainerError) as ctx:
            recipes.build(plan, parsing.parse_srt(short))
        self.assertIn("too much for the time", " ".join(ctx.exception.errors))

    def test_focus_dims_the_previous_item(self):
        doc = recipes.build(_plan(focus=True), _cues())
        dims = [e for e in doc["scenes"][0]["events"] if e["action"] == "dim"]
        self.assertTrue(dims)
        self.assertTrue(all(d["target"].startswith("s1_") for d in dims))
        self.assertFalse([d for d in dims if "title" in d["target"]])


class InputErrorTests(unittest.TestCase):
    def test_bad_inputs_are_named(self):
        self.assertIn("layout must be one of", _errs(_plan(layout="pie")))
        self.assertIn("needs items", _errs(_plan(items=[])))
        self.assertIn("fits", _errs(_plan(items=[{"label": str(i), "icon": "coins"} for i in range(8)])))
        self.assertIn("unknown lucide icon", _errs(_plan(items=[{"label": "x", "icon": "piggybank"}])))
        self.assertIn("outside the scene", _errs(_plan(items=[{"label": "x", "icon": "coins", "cue": 9}])))
        self.assertIn("exactly two sides", _errs(_plan(layout="compare", items=[])))

    def test_cli_build_writes_a_validated_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "subtitles.srt"), "w", encoding="utf-8").write(SRT)
            json.dump(_plan(), open(os.path.join(tmp, "plan.json"), "w", encoding="utf-8"))
            self.assertEqual(cli.cmd_build(tmp), 0)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "explainer.json")))


if __name__ == "__main__":
    unittest.main()
