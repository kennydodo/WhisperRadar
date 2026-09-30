"""The Finished/History detail for the shots stage now includes a motion-code
breakdown (ZI/ZO/PL/PR/PU/PD/PV/ST counts and percentages), so a plan leaning
too heavily on one motion (the manifest brief caps ~40% per motion, ~10% for
ST) is visible at a glance instead of only discovered once the batch is
already rendering.

Run: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import autorun  # noqa: E402


def _shots(*motions):
    return [{"asset": f"S{i:02d}.png", "motion": m}
            for i, m in enumerate(motions, 1)]


class MotionBreakdownLineTests(unittest.TestCase):
    def test_counts_and_percentages_are_correct(self):
        shots = _shots("ZI", "ZI", "PL", "PR", "ST")
        line = autorun._motion_breakdown_line(shots)
        self.assertIn("ZI 2 (40%)", line)
        self.assertIn("PL 1 (20%)", line)
        self.assertIn("PR 1 (20%)", line)
        self.assertIn("ST 1 (20%)", line)

    def test_codes_appear_in_a_fixed_canonical_order(self):
        shots = _shots("ST", "PU", "ZI")
        line = autorun._motion_breakdown_line(shots)
        # canonical order is ZI, ZO, PL, PR, PU, PD, PV, ST
        self.assertLess(line.index("ZI"), line.index("PU"))
        self.assertLess(line.index("PU"), line.index("ST"))

    def test_a_code_with_zero_shots_is_omitted(self):
        shots = _shots("ZI", "ZI")
        line = autorun._motion_breakdown_line(shots)
        self.assertNotIn("PL", line)
        self.assertNotIn("PU", line)

    def test_an_unknown_or_missing_motion_still_counts_without_erroring(self):
        shots = [{"asset": "a.png", "motion": "XX"}, {"asset": "b.png"}]
        line = autorun._motion_breakdown_line(shots)
        self.assertIn("XX 1", line)
        self.assertIn("? 1", line)

    def test_an_empty_shotlist_returns_an_empty_string(self):
        self.assertEqual(autorun._motion_breakdown_line([]), "")


if __name__ == "__main__":
    unittest.main()
