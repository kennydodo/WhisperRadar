"""The creator's additional direction and the research notes must reach the
judge as well as the writer, and the direction must outrank the notes on
structure / number of points."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import studio  # noqa: E402

DIRECTION = "match the number of points from the title [10 Habits]"


class JudgeDirectionTests(unittest.TestCase):
    def test_judge_prompt_carries_direction_and_notes(self):
        p = studio.rating_prompt("T", "g", "SCRIPT TEXT", "NOTE FACT ABC",
                                 "style", 0.02, extra_direction=DIRECTION)
        self.assertIn(DIRECTION, p)
        self.assertIn("do NOT penalise", p)
        self.assertIn("NOTE FACT ABC", p)
        self.assertLess(p.index(DIRECTION), p.index("SCRIPT TEXT"))

    def test_judge_prompt_without_direction_is_unchanged(self):
        p = studio.rating_prompt("T", "g", "S", "facts", "style", 0.02)
        self.assertNotIn("ADDITIONAL DIRECTION", p)

    def test_rate_script_passes_direction_to_the_judge(self):
        seen = []

        def fake(cfg, prompt, **kw):
            seen.append(prompt)
            return '{"score": 9.1, "criteria": {}, "feedback": [], "weak_spans": []}'

        with mock.patch.object(studio, "llm_generate", fake):
            r = studio.rate_script(object(), "T", "g", "script", "facts",
                                   "style", None, extra_direction=DIRECTION)
        self.assertEqual(r["score"], 9.1)
        self.assertIn(DIRECTION, seen[0])

    def test_writer_direction_outranks_the_notes(self):
        p = studio.script_prompt("T", "g", "facts", "style",
                                 extra_direction=DIRECTION)
        self.assertIn(DIRECTION, p)
        self.assertIn("the direction wins", p)
        self.assertIn("never invent facts", p)


if __name__ == "__main__":
    unittest.main()
