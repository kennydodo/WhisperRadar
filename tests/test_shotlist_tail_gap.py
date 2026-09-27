"""Cheap-continuation fix for a plan that stops cleanly, short of the end.

A shotlist reply can be syntactically complete (every bracket closed) and
still cover only PART of the narration - e.g. 73 shots for cues 1-221 of 484,
no error, just nothing planned for the rest. Before this fix every attempt
re-planned from scratch and truncated at roughly the same point every time.
shotlist_tail_gap finds that case, shotlist_tail_continuation_prompt asks for
just the missing tail, and merge_shotlist_continuation appends it.

Run: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import studio  # noqa: E402


def _shots(*ranges):
    return [{"cues": f"{a}-{b}" if b != a else str(a), "asset": f"S01_{i+1:02d}.png"}
            for i, (a, b) in enumerate(ranges)]


class ShotlistTailGapTests(unittest.TestCase):
    def test_clean_stop_before_the_end_returns_the_last_covered_cue(self):
        data = {"shots": _shots((1, 50), (51, 221))}
        self.assertEqual(studio.shotlist_tail_gap(data, 484), 221)

    def test_a_plan_that_reaches_the_final_cue_returns_none(self):
        data = {"shots": _shots((1, 50), (51, 100))}
        self.assertIsNone(studio.shotlist_tail_gap(data, 100))

    def test_a_plan_that_overshoots_the_final_cue_returns_none(self):
        data = {"shots": _shots((1, 50), (51, 120))}
        self.assertIsNone(studio.shotlist_tail_gap(data, 100))

    def test_an_internal_gap_is_not_a_clean_tail_stop(self):
        # cues 21-30 are missing entirely - a real structural fault, not
        # something a tail continuation (which only appends at the end) fixes
        data = {"shots": _shots((1, 20), (31, 60))}
        self.assertIsNone(studio.shotlist_tail_gap(data, 100))

    def test_an_overlap_is_not_a_clean_tail_stop(self):
        data = {"shots": _shots((1, 30), (25, 60))}
        self.assertIsNone(studio.shotlist_tail_gap(data, 100))

    def test_out_of_order_shots_are_not_a_clean_tail_stop(self):
        data = {"shots": _shots((51, 80), (1, 50))}
        self.assertIsNone(studio.shotlist_tail_gap(data, 100))

    def test_not_starting_at_cue_one_is_not_a_clean_tail_stop(self):
        data = {"shots": _shots((5, 60))}
        self.assertIsNone(studio.shotlist_tail_gap(data, 100))

    def test_a_malformed_cue_range_returns_none(self):
        data = {"shots": [{"cues": "not-a-range", "asset": "x.png"}]}
        self.assertIsNone(studio.shotlist_tail_gap(data, 100))

    def test_no_shots_returns_none(self):
        self.assertIsNone(studio.shotlist_tail_gap({"shots": []}, 100))


class ShotlistTailContinuationPromptTests(unittest.TestCase):
    def test_names_the_missing_cue_range_and_asks_for_a_fragment_only(self):
        prompt = studio.shotlist_tail_continuation_prompt("BASE PROMPT", 221, 484)
        self.assertIn("BASE PROMPT", prompt)
        self.assertIn("cues 1-221", prompt)
        self.assertIn("222-484", prompt)
        self.assertIn('"shots"', prompt)
        self.assertIn('"images"', prompt)


class MergeShotlistContinuationTests(unittest.TestCase):
    def test_appends_new_shots_and_images_without_touching_originals(self):
        data = {"shots": [{"cues": "1-50", "asset": "a.png"}],
                "images": [{"file": "a.png", "prompt": "p1"}],
                "source": "kept as-is"}
        addition = {"shots": [{"cues": "51-100", "asset": "b.png"}],
                    "images": [{"file": "b.png", "prompt": "p2"}]}
        merged = studio.merge_shotlist_continuation(data, addition)
        self.assertEqual(len(merged["shots"]), 2)
        self.assertEqual(len(merged["images"]), 2)
        self.assertEqual(merged["shots"][1]["asset"], "b.png")
        self.assertEqual(merged["source"], "kept as-is")
        # the original dicts are untouched (no aliasing surprises)
        self.assertEqual(len(data["shots"]), 1)


if __name__ == "__main__":
    unittest.main()
