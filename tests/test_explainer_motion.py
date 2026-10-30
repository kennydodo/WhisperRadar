"""Motion scenes: parameters are validated, and the look comes from the channel theme."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import explainer_scenes as es, studio  # noqa: E402
from whisperradar.video.explainer import recipes, remotion_backend as rb, themes  # noqa: E402

SRT = "".join(f"{i}\n00:00:{(i - 1) * 5:02d},000 --> 00:00:{i * 5 - 1:02d},500\nWords number {i} are said here.\n\n"
              for i in range(1, 7))


def cues():
    return es._cue_objs(studio.parse_srt_cues(SRT))


def pipe(**kw):
    p = {"tasks": 8, "start_ms": 500, "run_ms": 4000, "stages": [
        {"label": "One", "workers": 8, "work": 1}, {"label": "Two", "workers": 4, "work": 1, "waits_for_all": True}]}
    p.update(kw)
    return p


class ParamTests(unittest.TestCase):
    def test_good_blocks_pass(self):
        self.assertEqual(rb.check_block({"component": "pipeline", "params": pipe()}, "S"), [])
        gen = {"rows": [{"label": "A", "mode": "sequential", "words": list("abcd")}], "start_ms": 0, "run_ms": 3000}
        self.assertEqual(rb.check_block({"component": "generation", "params": gen}, "S"), [])
        code = {"filename": "a.py", "lines": ["x = 1", "y = 2"], "start_ms": 0, "type_ms": 1000,
                "highlights": [{"line": 2, "start_ms": 1200, "note": "here"}]}
        self.assertEqual(rb.check_block({"component": "code", "params": code}, "S"), [])

    def test_bad_values_are_rejected(self):
        for bad in (pipe(tasks=2), pipe(stages=[{"label": "One", "workers": 8}]),
                    pipe(stages=[{"label": "A", "workers": 9}, {"label": "B", "workers": 1}]), pipe(run_ms=100)):
            self.assertTrue(rb.check_block({"component": "pipeline", "params": bad}, "S"))
        self.assertTrue(rb.check_block({"component": "generation", "params": {
            "rows": [{"label": "A", "mode": "weird", "words": list("abcd")}], "start_ms": 0, "run_ms": 3000}}, "S"))
        self.assertTrue(rb.check_block({"component": "code", "params": {
            "filename": "a.py", "lines": ["x", "y"], "start_ms": 0, "type_ms": 1000,
            "highlights": [{"line": 9, "start_ms": 0}]}}, "S"))

    def test_plan_builds_motion_scenes_anchored_to_words(self):
        plan = {"scenes": [{"id": "run", "start": "words number 1", "layout": "pipeline", "tasks": 8,
                            "motion": {"at": "words number 2"}, "stages": pipe()["stages"]}]}
        doc = recipes.build(plan, cues())
        blk = doc["scenes"][0]["remotion"]
        self.assertEqual(blk["component"], "pipeline")
        self.assertGreaterEqual(blk["params"]["start_ms"], 400)


class BeatTests(unittest.TestCase):
    """Each step of an animation can be tied to the narration words that cause it."""

    def build(self, scene):
        return recipes.build({"scenes": [scene]}, cues())["scenes"][0]["remotion"]["params"]

    def test_pipeline_stage_unlocks_on_its_words(self):
        p = self.build({"id": "run", "start": "words number 1", "layout": "pipeline", "tasks": 8,
                        "motion": {"at": "words number 1"},
                        "stages": [{"label": "One", "workers": 8}, {"label": "Two", "workers": 4, "waits_for_all": True,
                                                                     "at": "words number 3"}]})
        self.assertNotIn("at_ms", p["stages"][0])
        self.assertEqual(p["stages"][1]["at_ms"], 10000)

    def test_generation_beats_follow_the_narration(self):
        p = self.build({"id": "g", "start": "words number 1", "layout": "generation", "motion": {"at": "words number 1"},
                        "rows": [{"label": "A", "mode": "plan_then_fill", "words": list("abcdefgh"),
                                  "beats": [{"at": "words number 2"}, {"at": "words number 4"}, {"cue": 6}]}]})
        self.assertEqual(p["rows"][0]["beats_ms"], [5000, 15000, 25000])

    def test_a_beat_outside_the_scene_is_rejected(self):
        with self.assertRaises(Exception):
            self.build({"id": "g", "cue_range": [1, 2], "layout": "generation", "motion": {"cue": 1},
                        "rows": [{"label": "A", "mode": "sequential", "words": list("abcd"), "beats": [{"cue": 5}]}]})

    def test_code_highlight_is_timed_from_the_words(self):
        p = self.build({"id": "c", "start": "words number 1", "layout": "code", "filename": "a.py",
                        "motion": {"at": "words number 1"}, "lines": ["x = 1", "y = 2"],
                        "highlights": [{"line": 2, "at": "words number 4", "note": "this"}]})
        self.assertEqual(p["highlights"][0]["start_ms"], 15000 - p["start_ms"])


class FixesAfterReviewTests(unittest.TestCase):
    def build(self, scene):
        return recipes.build({"scenes": [scene]}, cues())["scenes"][0]["remotion"]["params"]

    def test_eight_stages_are_kept(self):
        stages = [{"label": f"S{i}", "workers": 8} for i in range(8)]
        self.assertEqual(rb.check_block({"component": "pipeline", "params": pipe(stages=stages)}, "S"), [])
        self.assertTrue(rb.check_block({"component": "pipeline", "params": pipe(stages=stages + [{"label": "S9", "workers": 1}])}, "S"))

    def test_blank_words_are_rejected(self):
        gen = {"rows": [{"label": "A", "mode": "plan_then_fill", "words": ["Team", "___", "Thursday", "bring"]}],
               "start_ms": 0, "run_ms": 3000}
        self.assertTrue(any("___" in e for e in rb.check_block({"component": "generation", "params": gen}, "S")))

    def test_steps_on_the_same_moment_are_rejected(self):
        with self.assertRaises(Exception) as cm:
            self.build({"id": "g", "start": "words number 1", "layout": "generation", "motion": {"cue": 1},
                        "rows": [{"label": "A", "mode": "plan_then_fill", "words": list("abcdefgh"),
                                  "beats": [{"cue": 2}, {"cue": 2}, {"cue": 4}]}]})
        self.assertIn("same moment", str(cm.exception))

    def test_the_footer_reaches_the_animation(self):
        p = self.build({"id": "g", "start": "words number 1", "layout": "generation", "motion": {"at": "words number 1"},
                        "footer": {"text": "Why it matters", "at": "words number 3"},
                        "rows": [{"label": "A", "mode": "sequential", "words": list("abcd")}]})
        self.assertEqual(p["footer"]["text"], "Why it matters")
        self.assertEqual(p["footer"]["start_ms"], 10000)

    def test_levels_scene_builds_with_timed_levels(self):
        p = self.build({"id": "l", "cue_range": [1, 6], "layout": "levels", "motion": {"cue": 1},
                        "levels": [{"label": "Broad", "cells": 2, "cue": 1}, {"label": "Finer", "cells": 6, "cue": 3}],
                        "decoder": {"label": "Decoder", "cue": 5}, "result": {"text": "Done", "cue": 6}})
        self.assertEqual([x["at_ms"] for x in p["levels"]], [0, 10000])
        self.assertEqual(p["result"]["text"], "Done")
        self.assertTrue(rb.check_block({"component": "levels", "params": dict(p, levels=[{"label": "x", "cells": 9, "at_ms": 0}])}, "S"))


class CountTests(unittest.TestCase):
    def check(self, narration, scene):
        srt = f"1\n00:00:00,000 --> 00:00:08,000\n{narration}\n\n"
        cs = studio.parse_srt_cues(srt)
        return es.count_faults(recipes.resolve_anchors({"scenes": [dict(scene, cue_range=[1, 1])]}, es._cue_objs(cs)), cs)

    def test_a_wrong_stage_count_is_a_fault(self):
        st = lambda n: [{"label": f"S{i}", "workers": 2} for i in range(n)]
        bad = self.check("Suppose a job has eight stages and each one waits.", {"id": "p", "layout": "pipeline", "stages": st(2)})
        self.assertTrue(bad and "eight stages" in bad[0] and "exactly 8" in bad[0])
        self.assertEqual(self.check("Suppose a job has eight stages.", {"id": "p", "layout": "pipeline", "stages": st(8)}), [])

    def test_a_count_the_component_cannot_draw_points_to_another_layout(self):
        lv = [{"label": "L", "cells": 2}, {"label": "M", "cells": 4}]
        bad = self.check("There are six levels in the hierarchy.", {"id": "l", "layout": "levels", "levels": lv})
        self.assertTrue(bad and "cannot draw" in bad[0])


class LookTests(unittest.TestCase):
    class Doc:
        pass

    def doc(self, **kw):
        d = self.Doc()
        d.theme = themes.THEMES["chalkboard"]()
        for k, v in kw.items():
            setattr(d.theme, k, v)
        return d

    def test_graphic_channel_gets_no_handwriting(self):
        self.assertFalse(rb._theme_props(self.doc(line_style="straight"))["hand"])
        self.assertTrue(rb._theme_props(self.doc())["hand"])

    def test_colours_and_fonts_follow_the_theme(self):
        d = self.doc(background="#101820", accent="#ff00aa", heading_font="Delius-Regular.ttf")
        tp = rb._theme_props(d)
        self.assertEqual(tp["look"]["bg"], "#101820")
        self.assertEqual(tp["look"]["accent"], "#ff00aa")
        self.assertEqual(tp["font_files"]["heading"], "Delius-Regular.ttf")
        keyed, run = rb._prepare_assets({"component": "code", "params": {}, "theme": tp}, "")
        self.assertIn("WRHeading", run["theme"]["fontData"])
        self.assertTrue(run["theme"]["fontData"]["WRHeading"].startswith("data:font/ttf;base64,"))
        self.assertNotIn("base64", str(keyed))                       # the cache key holds hashes, not font bytes

    def test_a_missing_font_is_skipped_not_an_error(self):
        d = self.doc(body_font="NoSuchFont.ttf")
        keyed, run = rb._prepare_assets({"component": "code", "params": {}, "theme": rb._theme_props(d)}, "")
        self.assertNotIn("body", run["theme"]["fonts"])

    def test_a_different_look_changes_the_cache_key(self):
        a = rb.fingerprint(rb._prepare_assets({"component": "code", "params": {}, "theme": rb._theme_props(self.doc())}, "")[0], 960, 540, "x")
        b = rb.fingerprint(rb._prepare_assets({"component": "code", "params": {}, "theme": rb._theme_props(self.doc(accent="#ff00aa"))}, "")[0], 960, 540, "x")
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
