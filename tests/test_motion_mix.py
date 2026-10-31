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


class MotionMix(unittest.TestCase):
    def test_long_text_run_is_a_fault(self):
        r, c, t = build(["row"] * 8 + ["pipeline"] * 4)
        faults = es.motion_faults(r, c, t)
        self.assertTrue(any("8 text-only scenes in a row" in f for f in faults))
        self.assertIn("s1", " ".join(faults))

    def test_good_mix_passes(self):
        r, c, t = build(["row", "steps", "pipeline", "row", "levels", "compare", "pipeline", "hero"])
        self.assertEqual(es.motion_faults(r, c, t), [])

    def test_share_shortfall(self):
        # no run over the cap, but only 1 of 10 scenes (10%) shows a mechanism
        r, c, t = build(["row", "steps", "row", "pipeline", "row", "steps", "row", "hero", "row", "stack"])
        faults = es.motion_faults(r, c, t)
        self.assertTrue(any("only 10%" in f for f in faults), faults)

    def test_short_video_exempt(self):
        r, c, t = build(["row"] * 6, each=20.0)
        self.assertLess(t, es.MOTION_MIN_SECONDS)
        self.assertEqual(es.motion_faults(r, c, t), [])

    def test_unknown_genre_has_no_rule(self):
        r, c, t = build(["row"] * 10)
        self.assertEqual(es.motion_faults(r, c, t, {"scene_genre": "finance"}), [])

    def test_look_overrides(self):
        r, c, t = build(["row"] * 8 + ["pipeline"] * 4)
        self.assertEqual(es.motion_faults(r, c, t, {"text_run_max": 0, "text_run_seconds": 0}), [])
        self.assertEqual(es.motion_rule({"motion_min_share": "0.4"})["min_share"], 0.4)

    def test_percent_from_the_channel_form(self):
        self.assertEqual(es.motion_rule({"motion_min_share": "35"})["min_share"], 0.35)
        self.assertEqual(es.motion_rule({"text_run_max": "4", "text_run_seconds": "0"})["text_run"], 4)
        r, c, t = build(["row", "pipeline", "row", "row", "row", "row", "pipeline", "row"])
        self.assertTrue(es.motion_faults(r, c, t, {"motion_min_share": "60", "text_run_max": "0", "text_run_seconds": "0"}))
        self.assertEqual(es.motion_faults(r, c, t, {"motion_min_share": "0"}), [])

    def test_judge_sees_measured_mix(self):
        r, c, t = build(["row", "pipeline", "row", "row"])
        line = es.motion_summary(r, c)
        self.assertIn("25%", line)
        self.assertIn("longest text-only stretch 2 scenes", line)

    def test_writer_prompt_states_numbers(self):
        long = es.writer_prompt("x", 600)
        self.assertIn("at least 20% of the narration time", long)
        self.assertNotIn("{motion_share}", long)
        self.assertNotIn("MOTION MIX", es.writer_prompt("x", 100))


if __name__ == "__main__":
    unittest.main()
