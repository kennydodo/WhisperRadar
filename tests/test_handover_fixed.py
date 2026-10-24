"""handover.py: one fixed behaviour ("Style only"); the choice of templates is gone."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whisperradar import handover  # noqa: E402


class FixedHandoverTests(unittest.TestCase):
    def test_only_style_only_exists_whatever_a_production_stored(self):
        prod = {"handover": json.dumps({"script": "full"}), "own_channel_id": 1}
        for stage in ("plan", "script"):
            t = handover.for_production(None, prod, stage)
            self.assertEqual(t["id"], "style")
            self.assertTrue(t["scrub"] and t["guard"])
        self.assertEqual(handover.get(None, "full")["id"], "style")

    def test_instruction_is_the_productions_own_text(self):
        prod = {"stage_extras": json.dumps({"script": " be calm ", "plan_judge": ""})}
        self.assertEqual(handover.instruction(None, prod, "script"), "be calm")
        self.assertEqual(handover.instruction(None, prod, "plan_judge"), "")

    def test_all_parts_pass_and_originals_are_scrubbed(self):
        tpl = handover.get()
        must, fb, weak, _ = handover.script_notes(
            tpl, ["fix the ending"], ["slow the opening"], ["weak line"],
            "my script text", "")
        self.assertEqual((must, fb, weak), (["fix the ending"], ["slow the opening"], ["weak line"]))

    def test_the_retired_pieces_are_gone(self):
        for name in ("all_templates", "save_user_template", "channel_defaults",
                     "set_channel_default", "preview", "available_for"):
            self.assertFalse(hasattr(handover, name), name)


if __name__ == "__main__":
    unittest.main()
