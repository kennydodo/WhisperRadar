import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_external_prompts import Base  # noqa: E402
from tests.test_webstages import Fake, verdict, words  # noqa: E402
from whisperradar import autorun, db, external_prompts as ep, studio, webstages  # noqa: E402


class RequiredLengthTests(unittest.TestCase):
    def test_range_is_five_percent_either_side(self):
        self.assertEqual(studio.script_length_range(5000), (4750, 5250))

    def test_default_target_stays_a_flexible_guide(self):
        p = studio.script_prompt("T", "g", "facts", target_words=5000)
        self.assertIn("flexible guide", p)
        self.assertNotIn("LENGTH IS REQUIRED", p)
        self.assertIn("FLEXIBLE LENGTH", p)

    def test_typed_target_is_required_in_the_writer_prompt(self):
        p = studio.script_prompt("T", "g", "facts", target_words=5000,
                                 target_required=True)
        self.assertIn("LENGTH IS REQUIRED", p)
        self.assertIn("between 4750 and 5250 words", p)
        self.assertNotIn("flexible guide", p)
        self.assertNotIn("FLEXIBLE LENGTH", p)      # no rule contradicting it

    def test_gate_rejects_a_draft_under_the_minimum(self):
        passed, reasons, too_long, too_short = autorun._script_gate(
            3900, 5000, 0.0, 9.5, 7.5, 0.3, 0.5, None, min_words=4750)
        self.assertFalse(passed)
        self.assertTrue(too_short)
        self.assertFalse(too_long)
        self.assertTrue(any("under the required minimum of 4750" in r
                            for r in reasons))

    def test_gate_accepts_a_draft_inside_the_range(self):
        passed, reasons, _, too_short = autorun._script_gate(
            4900, 5000, 0.0, 9.5, 7.5, 0.3, 0.5, None, min_words=4750)
        self.assertTrue(passed)
        self.assertFalse(too_short)

    def test_gate_ignores_length_without_a_minimum(self):
        passed, _, _, too_short = autorun._script_gate(
            3900, 5000, 0.0, 9.5, 7.5, 0.3, 0.5, None)
        self.assertTrue(passed)
        self.assertFalse(too_short)

    def test_writer_feedback_says_how_many_words_are_missing(self):
        text = webstages._length_delta(3900, 5000, 4750)
        self.assertIn("850 under the required minimum of 4750", text)
        self.assertIn("never repetition or padding", text)
        flexible = webstages._length_delta(3900, 5000)
        self.assertIn("flexible guide", flexible)


class TypedTargetReachesThePromptTests(Base):
    def _type_target(self, words):
        conn = db.connect(self.cfg.db_path)
        try:
            db.update_production(conn, self.pid, stage_extras=json.dumps(
                {"script_words": str(words)}))
            conn.commit()
        finally:
            conn.close()

    def test_target_typed_in_the_box_is_required(self):
        self._type_target(5000)
        text = ep.script_writer_prompt(self.cfg, self.pid)
        self.assertIn("between 4750 and 5250 words", text)
        self.assertNotIn("flexible guide", text)

    def test_no_typed_target_keeps_the_flexible_guide(self):
        text = ep.script_writer_prompt(self.cfg, self.pid)
        self.assertIn("flexible guide", text)
        self.assertNotIn("LENGTH IS REQUIRED", text)


class ScriptLoopLengthAndTakeawayTests(Base):
    """run_script wiring, with a scripted fake transport (no browser)."""

    def _type_target(self, n):
        conn = db.connect(self.cfg.db_path)
        try:
            db.update_production(conn, self.pid, stage_extras=json.dumps(
                {"script_words": str(n)}))
            conn.commit()
        finally:
            conn.close()

    def run_loop(self, t, rounds=3):
        with mock.patch.object(ep, "_target_words", return_value=100):
            return webstages.run_script(self.cfg, self.pid, t, "zai",
                                        "deepseek", rounds=rounds,
                                        log=lambda m: None)

    def test_typed_target_rejects_a_short_draft_then_accepts_one_in_range(self):
        self._type_target(100)                      # floor 95
        t = Fake(zai=[words(90, "a"), words(100, "b")],
                 deepseek=[verdict(9), verdict(9)])
        out = self.run_loop(t)
        self.assertTrue(out.startswith("b0"))
        self.assertIn("5 under the required minimum of 95", t.calls[2]["prompt"])

    def test_no_typed_target_keeps_the_old_way(self):
        t = Fake(zai=[words(90, "a")], deepseek=[verdict(9)])
        out = self.run_loop(t)                      # length is not enforced
        self.assertTrue(out.startswith("a0"))

    def test_failed_takeaway_blocks_a_high_scoring_script(self):
        failing = json.dumps({
            "score": 9, "criteria": {}, "feedback": [], "weak_spans": [],
            "checklist": [{"item": "takeaway", "pass": False,
                           "quote": "And that is all."}]})
        passing = json.dumps({
            "score": 9, "criteria": {}, "feedback": [], "weak_spans": [],
            "checklist": [{"item": "takeaway", "pass": True,
                           "quote": "Check the date."}]})
        t = Fake(zai=[words(100, "a"), words(100, "b")],
                 deepseek=[failing, passing])
        out = self.run_loop(t)
        self.assertTrue(out.startswith("b0"))       # round 1 was not accepted
        self.assertIn("Takeaway", t.calls[2]["prompt"])


if __name__ == "__main__":
    unittest.main()
