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
SOURCE = "Zed Exampleton started Example Freight Co in 1998 and sold it."


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
    NOTE = "Open like Zed Exampleton does, with the sale of Example Freight Co."

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
    V = {"faults": ["Title copies Example Freight Co wording"], "fixes": ["shorter"],
         "score": 7, "closest": 2, "alternates": [1], "pick": "x", "weak": [3],
         "best": 1, "narrow": [2]}

    def test_selection_never_passes(self):
        for t in handover.BUILTIN.values():
            out = handover.plan_verdict(t, self.V, "own plan text", "Example Freight Co sale")
            for k in handover.SELECTION_KEYS:
                self.assertNotIn(k, out)

    def test_full_keeps_notes_unscrubbed_style_scrubs(self):
        full = handover.plan_verdict(handover.BUILTIN["full"], self.V, "own plan", "Example Freight Co")
        self.assertEqual(full["faults"], self.V["faults"])
        self.assertEqual(full["score"], 7)
        style = handover.plan_verdict(handover.BUILTIN["style"], self.V, "own plan", "Example Freight Co")
        self.assertEqual(style["faults"], [])

    def test_safe_verdict_uses_template(self):
        plan = {"formula": "f", "titles": ["T"], "promise": "p", "thumbnail": {}}
        a = pp.safe_verdict(self.V, plan, "Example Freight Co", "", "pkg")
        b = pp.safe_verdict(self.V, plan, "Example Freight Co", "", "pkg",
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

    def test_channel_routes_and_pages(self):
        conn = db.connect(self.cfg.db_path)
        cid = db.create_own_channel(conn, "Chan1")
        db.update_production(conn, self.pid, own_channel_id=cid)
        conn.close()
        self.client.post("/handover/save", data={
            "name": "Scoped", "parts": ["feedback"], "channels": [str(cid)]})
        self.assertEqual(handover.get(self.cfg, "scoped")["channels"], [cid])
        self.client.post(f"/handover/channel-default/{cid}",
                         data={"plan": "style", "script": "scoped"})
        self.assertEqual(handover.channel_defaults(self.cfg, cid),
                         {"script": "scoped"})
        page = self.client.get("/handover")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Channel defaults", page.data)
        studio_page = self.client.get(f"/studio/{self.pid}")
        self.assertEqual(studio_page.status_code, 200)
        self.assertIn(b"channel default", studio_page.data)
        # saving the production form unchanged keeps inheriting
        self.client.post(f"/studio/{self.pid}/handover",
                         data={"plan": "style", "script": "scoped"})
        conn = db.connect(self.cfg.db_path)
        prod = db.get_production(conn, self.pid)
        conn.close()
        self.assertFalse(prod["handover"])

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


class Instructions(Base):
    def setUp(self):
        super().setUp()
        self.client = create_app(self.cfg).test_client()

    def _prod(self):
        conn = db.connect(self.cfg.db_path)
        prod = db.get_production(conn, self.pid)
        conn.close()
        return prod

    def test_save_four_instructions_and_keep_others(self):
        self.client.post(f"/studio/{self.pid}/instructions", data={
            "plan": "calm tone", "plan_judge": "check promise",
            "script": "short", "script_judge": "ignore story names"})
        prod = self._prod()
        self.assertEqual(db.stage_extra(prod, "plan"), "calm tone")
        self.assertEqual(db.stage_extra(prod, "plan_judge"), "check promise")
        self.assertEqual(db.stage_extra(prod, "script"), "short")
        self.assertEqual(db.stage_extra(prod, "script_judge"), "ignore story names")
        self.client.post(f"/studio/{self.pid}/instructions", data={"plan": ""})
        prod = self._prod()
        self.assertEqual(db.stage_extra(prod, "plan"), "")
        self.assertEqual(db.stage_extra(prod, "script_judge"), "ignore story names")
        page = self.client.get(f"/studio/{self.pid}")
        self.assertIn(b"ignore story names", page.data)

    def test_plan_prompts_carry_the_instructions(self):
        ctx = {"direction": "use a calm tone", "judge_direction": "flag clickbait"}
        self.assertIn("use a calm tone", pp._writer_direction(ctx))
        self.assertIn("flag clickbait", pp._judge_direction(ctx))
        self.assertEqual(pp._writer_direction({}), "")
        self.assertEqual(pp._judge_direction({"judge_direction": "  "}), "")

    def test_script_judge_prompt_carries_the_instruction_before_the_reply_format(self):
        text = studio.rating_prompt("T", "general", SCRIPT, "brief", "style", 0.0,
                                    original=SOURCE,
                                    judge_direction="look for a cold open")
        self.assertIn("look for a cold open", text)
        self.assertLess(text.index("look for a cold open"),
                        text.index("Reply with ONLY a JSON object"))
        self.assertNotIn("INSTRUCTIONS FOR YOU, THE JUDGE", studio.rating_prompt(
            "T", "general", SCRIPT, "brief", "style", 0.0, original=SOURCE))

    def test_handover_page_previews(self):
        page = self.client.get("/handover")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"what the writer receives", page.data)
        pv = handover.preview(handover.BUILTIN["style"])
        self.assertEqual(pv["script"]["dropped"], 2)
        self.assertIn("ONLY", pv["judge_clause"])
        full = handover.preview(handover.BUILTIN["full"])
        self.assertEqual(full["script"]["dropped"], 0)
        self.assertNotIn("ONLY", full["judge_clause"])


class TemplateInstructions(unittest.TestCase):
    def test_template_text_comes_before_production_text(self):
        import tempfile, json
        from types import SimpleNamespace
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            cfg = SimpleNamespace(db_path=str(Path(d) / "x.db"))
            tid = handover.save_user_template(
                cfg, "Strict", True, True, ["must_fix"],
                instructions={"script_judge": "ignore pacing", "plan": ""})
            self.assertEqual(handover.get(cfg, tid)["instructions"],
                             {"script_judge": "ignore pacing"})
            prod = {"handover": json.dumps({"script": tid}),
                    "stage_extras": json.dumps({"script_judge": "check hook"})}
            out = handover.instruction(cfg, prod, "script_judge")
            self.assertIn("ignore pacing", out)
            self.assertIn("check hook", out)
            self.assertLess(out.index("ignore pacing"), out.index("check hook"))
            self.assertEqual(handover.instruction(cfg, prod, "plan"), "")


class MaskNames(unittest.TestCase):
    SRC = "The story opens. Then Zed Exampleton sold Example Freight Co. Open the safe."

    def test_names_masked_points_kept(self):
        out = handover.mask_names(
            ["Open with the sale of Example Freight Co by Zed Exampleton."],
            "Maya counted coins.", self.SRC)
        self.assertEqual(len(out), 1)
        self.assertNotIn("Exampleton", out[0])
        self.assertNotIn("Freight", out[0])
        self.assertIn("[name]", out[0])
        self.assertTrue(out[0].startswith("Open with the sale"))

    def test_script_notes_uses_mask(self):
        tpl = {"parts": ["feedback"], "scrub": False, "mask_names": True}
        _m, fb, _w, dropped = handover.script_notes(
            tpl, [], ["Zed Exampleton should open it."], [], "Maya.", self.SRC)
        self.assertEqual(dropped, 0)
        self.assertNotIn("Exampleton", fb[0])

    def test_precedence_wording(self):
        import tempfile, json
        from types import SimpleNamespace
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            cfg = SimpleNamespace(db_path=str(Path(d) / "x.db"))
            tid = handover.save_user_template(cfg, "T", False, False, [],
                                              instructions={"script": "A"})
            prod = {"handover": json.dumps({"script": tid}),
                    "stage_extras": json.dumps({"script": "B"})}
            out = handover.instruction(cfg, prod, "script")
            self.assertIn("follow this production's own", out)
            self.assertLess(out.index("A"), out.index("B"))


class Rename(unittest.TestCase):
    def test_rename_keeps_id(self):
        import tempfile
        from types import SimpleNamespace
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            cfg = SimpleNamespace(db_path=str(Path(d) / "x.db"))
            tid = handover.save_user_template(cfg, "Old", True, True, [])
            self.assertTrue(handover.rename_user_template(cfg, tid, "New name"))
            self.assertEqual(handover.get(cfg, tid)["name"], "New name")
            with self.assertRaises(ValueError):
                handover.rename_user_template(cfg, tid, "Style")
            self.assertFalse(handover.rename_user_template(cfg, "nope", "X"))


class ChannelScope(unittest.TestCase):
    def setUp(self):
        import tempfile
        from types import SimpleNamespace
        from pathlib import Path
        from whisperradar import db
        self.d = tempfile.TemporaryDirectory()
        self.cfg = SimpleNamespace(db_path=Path(self.d.name) / "x.db")
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.a = db.create_own_channel(conn, "A")
        self.b = db.create_own_channel(conn, "B")
        pid = db.create_production(conn, "t")
        db.update_production(conn, pid, own_channel_id=self.a)
        self.prod = db.get_production(conn, pid)
        conn.close()
        self.db = db

    def tearDown(self):
        self.d.cleanup()

    def test_scoped_template_only_offered_to_its_channel(self):
        tid = handover.save_user_template(self.cfg, "Only A", True, True, [],
                                          channels=[self.a])
        free = handover.save_user_template(self.cfg, "Everywhere", True, True, [])
        ids_a = {t["id"] for t in handover.available_for(self.cfg, self.a)}
        ids_b = {t["id"] for t in handover.available_for(self.cfg, self.b)}
        ids_none = {t["id"] for t in handover.available_for(self.cfg, None)}
        self.assertIn(tid, ids_a)
        self.assertNotIn(tid, ids_b)
        self.assertNotIn(tid, ids_none)
        for ids in (ids_a, ids_b, ids_none):
            self.assertIn(free, ids)
            self.assertIn("style", ids)
            self.assertIn("full", ids)

    def test_channel_default_used_until_production_picks(self):
        tid = handover.save_user_template(self.cfg, "Only A", False, False, [],
                                          channels=[self.a])
        handover.set_channel_default(self.cfg, self.a, "", tid)
        self.assertEqual(handover.effective_id(self.cfg, self.prod, "script"),
                         (tid, "channel"))
        self.assertEqual(handover.effective_id(self.cfg, self.prod, "plan"),
                         ("style", "default"))
        mine = {"handover": '{"script": "style"}', "own_channel_id": self.a}
        self.assertEqual(handover.effective_id(self.cfg, mine, "script"),
                         ("style", "production"))

    def test_default_ignored_when_template_no_longer_offered(self):
        tid = handover.save_user_template(self.cfg, "Only A", False, False, [],
                                          channels=[self.a])
        handover.set_channel_default(self.cfg, self.a, "", tid)
        handover.set_template_channels(self.cfg, tid, [self.b])
        self.assertEqual(handover.effective_id(self.cfg, self.prod, "script")[0],
                         "style")

    def test_cannot_set_unavailable_default(self):
        tid = handover.save_user_template(self.cfg, "Only A", False, False, [],
                                          channels=[self.a])
        handover.set_channel_default(self.cfg, self.b, "", tid)
        self.assertEqual(handover.channel_defaults(self.cfg, self.b), {})
