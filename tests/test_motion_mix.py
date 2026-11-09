"""The enforced motion mix: text-run cap, minimum visual share, genre rules, exemption, judge line."""
import unittest

from whisperradar import explainer_scenes as es


def srt_time(s):
    return f"00:{int(s // 60):02d}:{int(s % 60):02d},000"


def build(layouts, each=30.0):
    cues = [{"index": i + 1, "start": srt_time(i * each), "end": srt_time((i + 1) * each), "text": "x"}
            for i in range(len(layouts))]
    resolved = {"scenes": [{"id": f"s{i + 1}", "layout": lay, "cue_range": (i + 1, i + 1)}
                           for i, lay in enumerate(layouts)]}
    return resolved, cues, each * len(layouts)


OPT_IN = {"motion_min_share": "50", "text_run_max": "3", "text_run_seconds": "75"}   # a channel's own limits


class MotionMix(unittest.TestCase):
    """No default limit: the narration decides. A channel may still opt in to limits."""

    def test_no_default_limit(self):
        for layouts in (["row"] * 12, ["hero", "stack", "compare"] * 4, ["row"] * 8 + ["pipeline"] * 4):
            r, c, t = build(layouts)
            self.assertEqual(es.motion_faults(r, c, t), [])
            self.assertEqual(es.motion_faults(r, c, t, {"scene_genre": "finance"}), [])
        self.assertEqual(es.motion_rule({}), {})
        self.assertEqual(es.motion_rule({"scene_genre": "finance"}), {})

    def test_opt_in_long_text_run_is_a_fault(self):
        r, c, t = build(["row"] * 8 + ["pipeline"] * 4)
        faults = es.motion_faults(r, c, t, OPT_IN)
        self.assertTrue(any("8 text-only scenes in a row" in f for f in faults))
        self.assertIn("s1", " ".join(faults))

    def test_opt_in_good_mix_passes(self):
        r, c, t = build(["row", "flow", "pipeline", "row", "levels", "compare", "bars", "hero"])
        self.assertEqual(es.motion_faults(r, c, t, OPT_IN), [])

    def test_opt_in_share_shortfall(self):
        # no run over the cap, but only 1 of 10 scenes (10%) shows a mechanism
        r, c, t = build(["row", "steps", "row", "pipeline", "row", "steps", "row", "hero", "row", "stack"])
        faults = es.motion_faults(r, c, t, OPT_IN)
        self.assertTrue(any("only 10%" in f for f in faults), faults)

    def test_opt_in_short_video_exempt(self):
        r, c, t = build(["row"] * 4, each=20.0)
        self.assertLess(t, es.MOTION_MIN_SECONDS)
        self.assertEqual(es.motion_faults(r, c, t, OPT_IN), [])

    def test_unknown_genre_with_opt_in(self):
        r, c, t = build(["row"] * 10)
        self.assertTrue(es.motion_faults(r, c, t, dict(OPT_IN, scene_genre="lifestyle")))

    def test_look_overrides(self):
        r, c, t = build(["row"] * 8 + ["pipeline"] * 4)
        self.assertEqual(es.motion_faults(r, c, t, {"text_run_max": 0, "text_run_seconds": 0,
                                                    "motion_min_share": "30"}), [])
        self.assertEqual(es.motion_rule({"motion_min_share": "0.4"})["min_share"], 0.4)

    def test_percent_from_the_channel_form(self):
        self.assertEqual(es.motion_rule({"motion_min_share": "35"})["min_share"], 0.35)
        self.assertEqual(es.motion_rule({"text_run_max": "4", "text_run_seconds": "0"})["text_run"], 4)
        r, c, t = build(["row", "pipeline", "row", "row", "row", "row", "pipeline", "row"])
        self.assertTrue(es.motion_faults(r, c, t, {"motion_min_share": "60", "text_run_max": "0", "text_run_seconds": "0"}))
        self.assertEqual(es.motion_faults(r, c, t, {"motion_min_share": "0", "text_run_max": "0",
                                                    "text_run_seconds": "0"}), [])

    def test_judge_sees_measured_mix_for_information(self):
        r, c, t = build(["row", "pipeline", "row", "row"])
        line = es.motion_summary(r, c)
        self.assertIn("25%", line)
        self.assertIn("longest text-only stretch 2 scenes", line)
        self.assertIn("the narration decides", line)

    def test_default_prompt_is_narration_led_with_no_quota(self):
        for secs in (60, 600):
            p = es.writer_prompt("x", secs)
            self.assertNotIn("MOTION MIX", p)
            self.assertNotIn("{motion_share}", p)
            self.assertIn("PICK THE PICTURE BY WHAT THE NARRATION SAYS", p)
            self.assertIn("there is no quota either way", p)
            self.assertIn("as much text as the narration needs is fine", p)
        long = es.writer_prompt("x", 600)
        for gone in ("slide deck", "Most of the video is motion scenes", "the short pauses between motion scenes",
                     "Avoid the same layout three times in a row", "pick a motion scene"):
            self.assertNotIn(gone, long)

    def test_opt_in_limits_reach_the_prompt_exactly(self):
        p = es.writer_prompt("x", 600, rule={"min_share": 0.5, "text_run": 3, "text_seconds": 75.0})
        self.assertIn("at least 50% of the narration time", p)
        self.assertIn("never more than 3 text-only scenes in a row", p)
        self.assertIn("never more than 75 seconds of text-only scenes", p)
        only_run = es.writer_prompt("x", 600, rule={"text_run": 5})
        self.assertIn("never more than 5 text-only scenes in a row", only_run)
        self.assertNotIn("at least 0%", only_run)
        self.assertNotIn("never more than 0", only_run)

    def test_prompt_no_longer_rations_motion(self):
        long = es.writer_prompt("x", 600)
        for gone in ("one for every 60-90 seconds", "At most one or two motion scenes in a row",
                     "Use at most one or two of these per video", "prefer rows, steps and compare"):
            self.assertNotIn(gone, long)
        for layout in ("flow:", "bars:", "line:", "split:", "grid:"):
            self.assertIn(layout, long)


class GenrePacks(unittest.TestCase):
    def test_finance_sees_only_general_layouts(self):
        p = es.writer_prompt("x", 600, genre="finance")
        for layout in ("flow:", "bars:", "line:", "split:", "grid:", "counter:"):
            self.assertIn(layout, p)
        for layout in ("pipeline:", "generation:", "memory:", "precision:", "fit_chart", '"layout": "code"'):
            self.assertNotIn(layout, p)
        self.assertIn("The subject is money", p)

    def test_tech_keeps_its_specialised_layouts(self):
        p = es.writer_prompt("x", 600, genre="tech")
        for layout in ("pipeline:", "generation:", "memory:", "flow:", "bars:"):
            self.assertIn(layout, p)

    def test_genre_of(self):
        self.assertEqual(es.genre_of({"scene_genre": "finance"}), "finance")
        self.assertEqual(es.genre_of({}), "tech")
        self.assertEqual(es.genre_of({"scene_genre": "cooking"}), "tech")


class NumberCheck(unittest.TestCase):
    def _scene(self, said, **sc):
        cues = [{"index": 1, "start": srt_time(0), "end": srt_time(20), "text": said}]
        return {"scenes": [dict({"id": "n", "cue_range": (1, 1)}, **sc)]}, cues

    def test_said_numbers_pass(self):
        r, c = self._scene("Method A takes 21 seconds, method B takes 1 second and yours half a second.",
                           layout="bars", items=[{"label": "A", "value": 21}, {"label": "B", "value": 1},
                                                 {"label": "You", "value": 0.5}])
        self.assertEqual(es.number_faults(r, c), [])

    def test_scaled_and_grouped_numbers(self):
        r, c = self._scene("Rent is $1,200 of a 3 thousand dollar salary, and 1.5 million people do it.",
                           layout="split", total=3000, items=[{"label": "Rent", "value": 1200},
                                                              {"label": "People", "value": 1500000}])
        self.assertEqual(es.number_faults(r, c), [])

    def test_invented_number_is_a_fault_unless_illustrative(self):
        r, c = self._scene("Method A is much slower.", layout="bars",
                           items=[{"label": "A", "value": 21}, {"label": "B", "value": 1}])
        f = es.number_faults(r, c)
        self.assertTrue(f and "21 (A)" in f[0] and "illustrative" in f[0])
        r["scenes"][0]["illustrative"] = True
        self.assertEqual(es.number_faults(r, c), [])

    def test_line_values_checked(self):
        r, c = self._scene("It keeps rising.", layout="line", x=["a", "b"], series=[{"values": [1, 9]}])
        self.assertTrue(es.number_faults(r, c))


if __name__ == "__main__":
    unittest.main()
