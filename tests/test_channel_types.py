"""Channel types: explainer channels skip the image pipeline, normal channels keep it,
switching loses nothing, and opening choices work for every type."""
import json
import tempfile
import unittest
from pathlib import Path

from tests import wr_tmp
from whisperradar import autorun, channel_types as ct, db, external_prompts as ep, handover, openings, studio
from whisperradar.config import load_config
from whisperradar.video import settings_spec as ss
from whisperradar.webapp import create_app


class RegistryTests(unittest.TestCase):
    def test_types_and_legacy_reading(self):
        self.assertEqual(ct.keys()[:2], ["standard", "explainer"])
        self.assertEqual(ct.type_of({}).key, "standard")                          # nothing set: a normal channel
        self.assertEqual(ct.type_of({"visual_mode": "whiteboard"}).key, "explainer")   # old setting read as a type
        self.assertEqual(ct.type_of({"channel_type": "standard", "visual_mode": "whiteboard"}).key, "standard")
        self.assertTrue(ct.uses_images({"visual_mode": "images"}))
        self.assertFalse(ct.uses_images({"channel_type": "explainer"}))
        self.assertTrue(ct.plans_scenes({"visual_mode": "motion_graphics"}))

    def test_format_and_normalize(self):
        self.assertEqual(ct.format_of({"channel_type": "explainer"}), "whiteboard")
        self.assertEqual(ct.format_of({"channel_type": "explainer", "explainer.format": "motion_graphics"}),
                         "motion_graphics")
        n = ct.normalize({"channel_type": "explainer", "explainer.format": "motion_graphics", "visual_mode": "images"})
        self.assertEqual(n["visual_mode"], "motion_graphics")
        n = ct.normalize({"channel_type": "standard", "visual_mode": "whiteboard"})
        self.assertEqual(n["visual_mode"], "images")
        self.assertEqual(ct.normalize({"visual_mode": "whiteboard"}), {"visual_mode": "whiteboard"})

    def test_a_new_type_can_be_registered(self):
        t = ct.ChannelType("shorts", "Shorts", image_pipeline=True)
        try:
            ct.register(t)
            self.assertEqual(ct.type_of({"channel_type": "shorts"}).key, "shorts")
            self.assertIn("shorts", ct.keys())
        finally:
            ct.TYPES.pop("shorts", None)

    def test_apply_choice_keeps_other_values(self):
        v = ct.apply_choice({"explainer.theme": "chalkboard", "timing.min_image_seconds": 2.0}, "explainer", "motion_graphics")
        self.assertEqual(v["visual_mode"], "motion_graphics")
        self.assertEqual(v["explainer.theme"], "chalkboard")
        v = ct.apply_choice(v, "standard", "")
        self.assertEqual((v["channel_type"], v["visual_mode"], v["explainer.format"]), ("standard", "images", "motion_graphics"))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        self.bible_dir = Path(self.tmp.name) / "seed"
        (self.bible_dir / "refs").mkdir(parents=True)
        (self.bible_dir / "refs" / "maya.png").write_bytes(b"png")
        self.oc = db.create_own_channel(self.conn, "Chan")
        db.update_own_channel(self.conn, self.oc, bible="MAYA: woman, scarf", style="soft watercolour",
                              writing_style="Short plain sentences.", bible_dir=str(self.bible_dir))
        self.pid = db.create_production(self.conn, "How much VRAM", "AI")
        db.update_production(self.conn, self.pid, own_channel_id=self.oc)

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def set_type(self, key, **more):
        vals = ss.load_channel(self.conn, self.oc)
        vals = ct.apply_choice(vals, key, more.get("fmt", ""))
        ss.save_channel(self.conn, self.oc, vals)

    def prod(self):
        return db.get_production(self.conn, self.pid)

    def seed(self):
        return studio.seed_production(self.cfg, self.conn, self.prod())

    def pdir(self):
        return studio.prod_dir(self.cfg, self.pid)


class ExplainerWorkflowTests(Base):
    def test_an_explainer_gets_no_visual_style_bible_or_references(self):
        self.set_type("explainer", fmt="whiteboard")
        out = self.seed()
        d = self.pdir()
        self.assertEqual(out.get("skipped"), "explainer channel")
        self.assertTrue((d / "writing_style.md").is_file())                  # the shared writing style still loads
        for name in ("bible.md", "style.md"):
            self.assertFalse((d / name).exists(), name)
        self.assertFalse((d / "refs").exists())

    def test_explainer_stages_skip_images_and_need_no_bible(self):
        self.set_type("explainer", fmt="motion_graphics")
        self.assertTrue(autorun.explainer_production(self.cfg, self.pid))
        plan = {e["stage"]: e for e in autorun.build_plan(self.cfg, self.pid)}
        for stage in ("refs", "images", "motion"):
            self.assertEqual(plan[stage]["action"], "skip", stage)
        self.assertNotIn("bible", plan["shots"]["detail"])                   # scene planning, not the bible gate
        self.assertEqual(plan["shots"]["action"], "run")

    def test_the_shared_stages_are_not_skipped_for_being_an_explainer(self):
        self.set_type("explainer")
        plan = {e["stage"]: e for e in autorun.build_plan(self.cfg, self.pid)}
        for stage in ("style", "script", "audio", "srt"):
            self.assertIn(stage, plan)
            self.assertNotIn("explainer", plan[stage]["detail"], stage)
        self.assertEqual(plan["script"]["action"], "run")
        self.assertEqual(plan["style"]["action"], "run")                     # the channel's writing style is loaded

    def test_old_visual_mode_only_channels_behave_as_explainers(self):
        ss.save_channel(self.conn, self.oc, {"visual_mode": "whiteboard"})
        self.assertTrue(autorun.explainer_production(self.cfg, self.pid))
        self.assertEqual(self.seed().get("skipped"), "explainer channel")


class NormalWorkflowTests(Base):
    def test_a_normal_channel_keeps_its_image_inputs(self):
        for setting in ({}, {"channel_type": "standard", "visual_mode": "images"}):
            ss.save_channel(self.conn, self.oc, setting)
            out = self.seed()
            self.assertTrue(out["bible"] and out["style"], setting)
            self.assertEqual(out["refs"], 1)
            self.assertFalse(autorun.explainer_production(self.cfg, self.pid))
            d = self.pdir()
            self.assertEqual((d / "bible.md").read_text().strip(), "MAYA: woman, scarf")
            self.assertTrue((d / "refs" / "maya.png").is_file())
            for p in [d / "bible.md", d / "style.md", d / "refs" / "maya.png"]:
                p.unlink()
            (d / "refs").rmdir()

    def test_a_normal_channel_still_gates_the_shotlist_on_the_bible(self):
        db.update_own_channel(self.conn, self.oc, bible="", style="", bible_dir="")
        plan = {e["stage"]: e for e in autorun.build_plan(self.cfg, self.pid)}
        self.assertEqual(plan["shots"]["action"], "pause")
        self.assertIn("bible", plan["shots"]["detail"])


class SwitchingTests(Base):
    def test_legacy_channel_choice_wins_over_a_new_global_type(self):
        ss.save_global(self.conn, {"channel_type": "standard", "visual_mode": "images"})
        ss.save_channel(self.conn, self.oc, {"visual_mode": "motion_graphics"})
        values = ss.effective(self.conn, self.oc)
        self.assertEqual(values["channel_type"], "explainer")
        self.assertEqual(values["visual_mode"], "motion_graphics")
        self.assertTrue(autorun.explainer_production(self.cfg, self.pid))
        self.assertEqual(self.seed().get("skipped"), "explainer channel")
        # Resolving old choices does not rewrite the channel's stored options.
        self.assertEqual(ss.load_channel(self.conn, self.oc), {"visual_mode": "motion_graphics"})

    def test_legacy_normal_channel_does_not_inherit_explainer_type(self):
        ss.save_global(self.conn, {"channel_type": "explainer", "explainer.format": "motion_graphics"})
        ss.save_channel(self.conn, self.oc, {"visual_mode": "images"})
        self.assertEqual(ss.effective(self.conn, self.oc)["channel_type"], "standard")
        self.assertFalse(autorun.explainer_production(self.cfg, self.pid))
        self.assertTrue(self.seed()["bible"])

    def test_legacy_channel_format_wins_over_global_format(self):
        ss.save_global(self.conn, {"channel_type": "explainer", "explainer.format": "motion_graphics"})
        ss.save_channel(self.conn, self.oc, {"visual_mode": "whiteboard"})
        self.assertEqual(ss.effective(self.conn, self.oc)["visual_mode"], "whiteboard")

    def test_no_channel_type_choice_still_inherits_global(self):
        ss.save_global(self.conn, {"channel_type": "explainer", "explainer.format": "motion_graphics"})
        self.assertEqual(ss.effective(self.conn, self.oc)["visual_mode"], "motion_graphics")

    def test_switching_type_loses_no_saved_setting(self):
        ss.save_channel(self.conn, self.oc, {"explainer.theme": "chalkboard", "timing.min_image_seconds": 2.0})
        before = {r: db.get_own_channel(self.conn, self.oc)[r] for r in ("bible", "style", "bible_dir", "writing_style")}
        self.set_type("explainer", fmt="motion_graphics")
        self.assertEqual(self.seed().get("skipped"), "explainer channel")
        self.set_type("standard")
        saved = ss.load_channel(self.conn, self.oc)
        self.assertEqual(saved["explainer.theme"], "chalkboard")
        self.assertEqual(saved["timing.min_image_seconds"], 2.0)
        self.assertEqual(saved["explainer.format"], "motion_graphics")       # remembered for the next switch
        after = {r: db.get_own_channel(self.conn, self.oc)[r] for r in before}
        self.assertEqual(before, after)                                       # the image inputs were never touched
        out = self.seed()
        self.assertTrue(out["bible"] and out["style"])                        # and a normal channel uses them again
        self.set_type("explainer")
        self.assertEqual(ss.load_channel(self.conn, self.oc)["explainer.format"], "motion_graphics")

    def test_the_imgtovideo_visual_mode_follows_the_type(self):
        self.set_type("explainer", fmt="whiteboard")
        self.assertEqual(ss.effective(self.conn, self.oc)["visual_mode"], "whiteboard")
        self.set_type("standard")
        self.assertEqual(ss.effective(self.conn, self.oc)["visual_mode"], "images")


class OpeningChoicesAnyTypeTests(Base):
    def setUp(self):
        super().setUp()
        self.cfg.studio_dir.mkdir(parents=True, exist_ok=True)
        self.pdir().mkdir(parents=True, exist_ok=True)
        (self.pdir() / "writing_style.md").write_text("Always state the purpose immediately after the hook.", encoding="utf-8")
        db.update_production(self.conn, self.pid, opening_config=json.dumps(openings.snapshot(self.conn, ["explain", "compare"])))

    def check_all(self):
        prod = self.prod()
        for writer in (True, False):
            self.assertIn("[explain]", openings.instruction(self.cfg, prod, writer=writer))
        for text in (ep.script_writer_prompt(self.cfg, self.pid), ep.script_judge_prompt(self.cfg, self.pid, "A script.")):
            self.assertIn("SELECTED OPENING APPROACH", text)
            self.assertNotIn("CLEAR PURPOSE AFTER THE HOOK", text)           # the universal rule stays removed
        self.assertIn("[explain]", handover.instruction(self.cfg, prod, "script_judge"))
        text = openings.capture(self.cfg, self.pid, "<!-- opening-style:compare -->\nActual narration.")
        self.assertEqual(text, "Actual narration.")                            # metadata never reaches the narration
        with self.assertRaises(ValueError):
            openings.capture(self.cfg, self.pid, "<!-- opening-style:story -->\nWrong approach.")

    def test_for_a_normal_channel(self):
        self.set_type("standard")
        self.check_all()

    def test_for_an_explainer_channel(self):
        self.set_type("explainer")
        self.check_all()


class RouteTests(Base):
    def test_routes_register_once_and_pages_load(self):
        app = create_app(self.cfg)
        rules = [r.rule for r in app.url_map.iter_rules()]
        self.assertEqual(len(rules), len(set((r.rule, tuple(sorted(r.methods))) for r in app.url_map.iter_rules())))
        for needed in ("/opening-styles", "/my-channels/<int:cid>/opening", "/explainer", "/explainer/<pid>/plan"):
            self.assertTrue(any(r.startswith(needed.split("<")[0]) for r in rules), needed)
        client = app.test_client()
        self.assertEqual(client.get("/my-channels").status_code, 200)
        self.assertEqual(client.get("/opening-styles").status_code, 200)


if __name__ == "__main__":
    unittest.main()
