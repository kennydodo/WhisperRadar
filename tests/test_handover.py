"""Hand-over templates: what the judge passes to the writer (handover.py)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402,F401

from whisperradar import db, handover, plan as pp, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

SCRIPT = "Maya opened the old jar and counted the coins slowly."
SOURCE = "Dennis Hartwell started Hartwell Logistics in 1998 and sold it."


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.pid = db.create_production(conn, "P", "general", None, None)
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()


class TemplateStore(Base):
    def test_builtins_and_default(self):
        t = handover.all_templates(self.cfg)
        self.assertIn("style", t)
        self.assertIn("full", t)
        self.assertTrue(t["style"]["scrub"] and t["style"]["guard"])
        self.assertFalse(t["full"]["scrub"] or t["full"]["guard"])
        self.assertEqual(handover.get(self.cfg, None)["id"], "style")
        self.assertEqual(handover.get(self.cfg, "nope")["id"], "style")

    def test_save_load_delete_user_template(self):
        tid = handover.save_user_template(self.cfg, "My Mix", scrub=False,
                                          guard=True, parts=["feedback", "bogus"])
        self.assertEqual(tid, "my-mix")
        t = handover.get(self.cfg, "my-mix")
        self.assertEqual((t["name"], t["scrub"], t["guard"], t["parts"]),
                         ("My Mix", False, True, ["feedback"]))
        self.assertTrue(handover.delete_user_template(self.cfg, "my-mix"))
        self.assertEqual(handover.get(self.cfg, "my-mix")["id"], "style")

    def test_builtin_names_are_protected(self):
        with self.assertRaises(ValueError):
            handover.save_user_template(self.cfg, "full", True, True, [])

    def test_choice_per_production(self):
        conn = db.connect(self.cfg.db_path)
        db.update_production(conn, self.pid, handover=json.dumps({"script": "full"}))
        prod = db.get_production(conn, self.pid)
        conn.close()
        self.assertEqual(handover.for_production(self.cfg, prod, "script")["id"], "full")
        self.assertEqual(handover.for_production(self.cfg, prod, "plan")["id"], "style")


class ScriptNotes(unittest.TestCase):
    NOTE = "Open like Dennis Hartwell does, with the sale of Hartwell Logistics."

    def test_style_scrubs_original_details(self):
        must, fb, weak, gone = handover.script_notes(
            handover.BUILTIN["style"], [], [self.NOTE, "Slow the opening."],
            ["Maya opened the old jar"], SCRIPT, SOURCE)
        self.assertEqual(fb, ["Slow the opening."])
        self.assertEqual(gone, 1)

    def test_full_passes_everything_as_written(self):
        must, fb, weak, gone = handover.script_notes(
            handover.BUILTIN["full"], ["fix X"], [self.NOTE], ["w"], SCRIPT, SOURCE)
        self.assertEqual((must, fb, weak, gone), (["fix X"], [self.NOTE], ["w"], 0))

    def test_parts_can_be_left_out(self):
        tpl = {"scrub": False, "guard": True, "parts": ["must_fix"]}
        must, fb, weak, _ = handover.script_notes(tpl, ["m"], ["f"], ["w"], SCRIPT, SOURCE)
        self.assertEqual((must, fb, weak), (["m"], [], []))

    def test_retry_notes_order_matches_the_old_retry(self):
        rating = {"must_fix": ["m1"], "feedback": ["f1"], "weak_spans": ["w1"]}
        self.assertEqual(handover.retry_notes(handover.BUILTIN["full"], rating,
                                              SCRIPT, SOURCE), ["m1", "f1"])
        rating["feedback"] = []
        self.assertEqual(handover.retry_notes(handover.BUILTIN["full"], rating,
                                              SCRIPT, SOURCE), ["m1", "w1"])


class PlanVerdict(unittest.TestCase):
    V = {"faults": ["Title copies Hartwell Logistics wording"], "fixes": ["shorter"],
         "score": 7, "closest": 2, "alternates": [1], "pick": "x", "weak": [3],
         "best": 1, "narrow": [2]}

    def test_selection_never_passes(self):
        for t in handover.BUILTIN.values():
            out = handover.plan_verdict(t, self.V, "own plan text", "Hartwell Logistics sale")
            for k in handover.SELECTION_KEYS:
                self.assertNotIn(k, out)

    def test_full_keeps_notes_unscrubbed_style_scrubs(self):
        full = handover.plan_verdict(handover.BUILTIN["full"], self.V, "own plan", "Hartwell Logistics")
        self.assertEqual(full["faults"], self.V["faults"])
        self.assertEqual(full["score"], 7)
        style = handover.plan_verdict(handover.BUILTIN["style"], self.V, "own plan", "Hartwell Logistics")
        self.assertEqual(style["faults"], [])

    def test_safe_verdict_uses_template(self):
        plan = {"formula": "f", "titles": ["T"], "promise": "p", "thumbnail": {}}
        a = pp.safe_verdict(self.V, plan, "Hartwell Logistics", "", "pkg")
        b = pp.safe_verdict(self.V, plan, "Hartwell Logistics", "", "pkg",
                            handover.BUILTIN["full"])
        self.assertEqual(a["faults"], [])
        self.assertEqual(b["faults"], self.V["faults"])


class GuardInPrompt(unittest.TestCase):
    def prompt(self, guard):
        return studio.rating_prompt("T", "general", SCRIPT, "brief", "style", 0.0,
                                    original=SOURCE, guard=guard)

    def test_guard_text_switches(self):
        self.assertIn("They may contain ONLY", self.prompt(True))
        p = self.prompt(False)
        self.assertNotIn("They may contain ONLY", p)
        self.assertIn("You may refer to it", p)


class Routes(Base):
    def setUp(self):
        super().setUp()
        self.client = create_app(self.cfg).test_client()

    def test_manage_page_and_save_delete(self):
        self.assertEqual(self.client.get("/handover").status_code, 200)
        r = self.client.post("/handover/save", data={
            "name": "Mine", "parts": ["feedback", "must_fix"], "scrub": "on"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("mine", handover.all_templates(self.cfg))
        self.client.post("/handover/delete/mine")
        self.assertNotIn("mine", handover.all_templates(self.cfg))

    def test_choose_for_production(self):
        r = self.client.post(f"/studio/{self.pid}/handover",
                             data={"plan": "style", "script": "full"})
        self.assertEqual(r.status_code, 302)
        conn = db.connect(self.cfg.db_path)
        prod = db.get_production(conn, self.pid)
        conn.close()
        self.assertEqual(json.loads(prod["handover"]), {"script": "full"})
        page = self.client.get(f"/studio/{self.pid}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"What the judge passes to the writer", page.data)

    def test_unknown_template_is_ignored(self):
        self.client.post(f"/studio/{self.pid}/handover", data={"script": "zzz"})
        conn = db.connect(self.cfg.db_path)
        prod = db.get_production(conn, self.pid)
        conn.close()
        self.assertIsNone(prod["handover"])


if __name__ == "__main__":
    unittest.main()
