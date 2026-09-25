"""The script gate: what passes, and the reason every rejection is logged.

Run: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import autorun  # noqa: E402


class ScriptGateTests(unittest.TestCase):
    def gate(self, **overrides):
        base = dict(words=1000, target_words=1000, overlap=0.05, score=9.5,
                    min_rating=9.0, max_overlap=0.12, hard_overlap=0.20)
        base.update(overrides)
        return autorun._script_gate(**base)

    def test_a_good_draft_passes_with_no_reasons(self):
        passed, why, too_long, too_short = self.gate()
        self.assertTrue(passed)
        self.assertEqual(why, [])
        self.assertFalse(too_long or too_short)

    def test_a_stub_is_rejected_with_a_length_reason(self):
        passed, why, _, too_short = self.gate(words=300)
        self.assertFalse(passed)
        self.assertTrue(too_short)
        self.assertTrue(any("well under" in r for r in why), why)

    def test_a_judge_that_cannot_rate_is_named(self):
        passed, why, _, _ = self.gate(score=None, judge_error="empty response")
        self.assertFalse(passed)
        self.assertTrue(any("judge could not rate" in r and "empty response" in r
                            for r in why), why)

    def test_high_overlap_names_the_hard_limit(self):
        passed, why, _, _ = self.gate(overlap=0.5)
        self.assertFalse(passed)
        self.assertTrue(any("hard" in r for r in why), why)

    def test_being_long_is_flagged_but_does_not_reject_on_its_own(self):
        passed, why, too_long, _ = self.gate(words=1300)
        self.assertTrue(too_long)
        self.assertTrue(passed)
        self.assertEqual(why, [])


if __name__ == "__main__":
    unittest.main()
