import json
import unittest

from test_external_prompts import Base

from whisperradar import autorun, db, external_prompts as ep, studio, webstages


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


if __name__ == "__main__":
    unittest.main()
