"""The History panel's per-attempt checklist lines: a plain "rating 6.9
(min 7.5)" made the reader re-derive pass/fail from the threshold every
time. _script_criteria_line and _shots_criteria_line state it directly as
a checkmark or cross per criterion.

Run: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import autorun  # noqa: E402

CHECK = "✓"
CROSS = "✗"


class ScriptCriteriaLineTests(unittest.TestCase):
    def test_all_criteria_passing_are_all_checked(self):
        line = autorun._script_criteria_line(
            score=8.0, min_rating=7.5, overlap=0.05, max_overlap=0.12,
            hard_overlap=0.20, words=2000, target_words=2000,
            too_short=False)
        self.assertEqual(line.count(CHECK), 3)
        self.assertEqual(line.count(CROSS), 0)

    def test_a_failing_rating_is_crossed_but_others_stay_checked(self):
        line = autorun._script_criteria_line(
            score=6.9, min_rating=7.5, overlap=0.05, max_overlap=0.12,
            hard_overlap=0.20, words=2000, target_words=2000,
            too_short=False)
        self.assertIn(f"{CROSS} rating", line)
        self.assertIn(f"{CHECK} overlap", line)
        self.assertIn(f"{CHECK} length", line)

    def test_a_too_short_draft_crosses_length_only(self):
        line = autorun._script_criteria_line(
            score=9.0, min_rating=7.5, overlap=0.05, max_overlap=0.12,
            hard_overlap=0.20, words=1000, target_words=2000,
            too_short=True)
        self.assertIn(f"{CHECK} rating", line)
        self.assertIn(f"{CHECK} overlap", line)
        self.assertIn(f"{CROSS} length", line)

    def test_overlap_over_the_hard_limit_is_crossed(self):
        line = autorun._script_criteria_line(
            score=9.0, min_rating=7.5, overlap=0.25, max_overlap=0.12,
            hard_overlap=0.20, words=2000, target_words=2000,
            too_short=False)
        self.assertIn(f"{CROSS} overlap", line)

    def test_a_missing_score_crosses_rating_and_shows_n_a(self):
        line = autorun._script_criteria_line(
            score=None, min_rating=7.5, overlap=0.05, max_overlap=0.12,
            hard_overlap=0.20, words=2000, target_words=2000,
            too_short=False)
        self.assertIn(f"{CROSS} rating n/a", line)


class ShotsCriteriaLineTests(unittest.TestCase):
    def test_zero_faults_and_ratio_at_or_above_min_are_both_checked(self):
        line = autorun._shots_criteria_line(
            faults=[], matched=100, total=107, ratio=100 / 107,
            min_align=0.90)
        self.assertEqual(line.count(CHECK), 2)
        self.assertEqual(line.count(CROSS), 0)

    def test_a_fault_crosses_faults_even_if_ratio_passes(self):
        line = autorun._shots_criteria_line(
            faults=["cue 5 has no shot"], matched=100, total=107,
            ratio=100 / 107, min_align=0.90)
        self.assertIn(f"{CROSS} faults 1", line)
        self.assertIn(f"{CHECK} detail", line)

    def test_ratio_below_min_is_crossed(self):
        line = autorun._shots_criteria_line(
            faults=[], matched=60, total=107, ratio=60 / 107, min_align=0.90)
        self.assertIn(f"{CROSS} detail", line)


if __name__ == "__main__":
    unittest.main()
