"""Per-channel planning-brief profiles: motion policy + presentation.

The manifest-authoring brief is a template (whisperradar/brief_template.md).
A channel's motion profile fills the motion/pacing slots, a free-text
presentation block fills the who-is-on-screen slot, and the same profile drives
the review gates. The standard profile must reproduce the original brief
exactly, so a channel with nothing set plans as it always did.

Run: python -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import autorun, briefs, db, settings, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

# the brief as it was before it became a template (sibling ImgToVideo repo);
# the equivalence test is skipped when that checkout is not next to this one
ORIGINAL = ROOT.parent / "ImgToVideo" / "docs" / "manifest-authoring-brief.md"


def _ts(t):
    m, s = divmod(int(t), 60)
    return f"00:{m:02d}:{s:02d},000"


def _cues(count, secs):
    return [{"index": i + 1, "start": _ts(i * secs), "end": _ts((i + 1) * secs),
             "text": f"cue {i + 1}"} for i in range(count)]


def _shots(spec):
    return {"shots": [{"cues": f"{first}-{last}", "asset": asset,
                       "scene": "S01", "motion": motion}
                      for asset, first, last, motion in spec],
            "images": [{"file": asset, "prompt": "p"}
                       for asset, _, _, _ in spec]}


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.template = briefs.read_template()

    @unittest.skipUnless(ORIGINAL.exists(), "ImgToVideo checkout not found")
    def test_the_default_render_is_the_original_brief_exactly(self):
        original = ORIGINAL.read_text(encoding="utf-8").split("\n---\n", 1)[1]
        self.assertEqual(briefs.render_brief(self.template), original.strip())

    def test_every_preset_renders_with_no_slot_left_over(self):
        for key, profile in briefs.MOTION_PRESETS.items():
            text = briefs.render_brief(self.template, profile, "Host opens.")
            self.assertNotIn("{{", text, key)
            self.assertNotIn("[[", text, key)

    def test_no_presentation_adds_nothing(self):
        text = briefs.render_brief(self.template)
        self.assertNotIn("SECTION 6B", text)
        self.assertNotIn("\n\n\n", text)

    def test_presentation_sits_between_the_refs_and_the_output_sections(self):
        text = briefs.render_brief(self.template, None, "Maya opens the video.")
        self.assertIn("Maya opens the video.", text)
        self.assertLess(text.index("## SECTION 6 "), text.index("SECTION 6B"))
        self.assertLess(text.index("SECTION 6B"), text.index("## SECTION 7"))

    def test_static_profile_removes_every_motion_instruction(self):
        text = briefs.render_brief(self.template, briefs.STATIC)
        self.assertIn("every shot is static (ST)", text)
        self.assertIn("ST: 2304x1296", text)
        self.assertNotIn("PU/PD: 2304x2160", text)
        self.assertNotIn("| SCN character beat | ZI", text)
        self.assertIn("S01_03_SCN_ST.png", text)       # the examples too
        self.assertNotIn("S01_03_SCN_PR.png", text)

    def test_long_hold_profile_states_its_hold_range(self):
        text = briefs.render_brief(self.template, briefs.LONG_HOLDS)
        self.assertIn("between 10 and 30 seconds", text)
        self.assertIn("10-30s is normal", text)
        # everything else is the standard brief (all motions still allowed)
        self.assertIn("PU/PD: 2304x2160", text)

    def test_a_slotless_custom_template_is_fine_for_standard_only(self):
        plain = "A plain brief with no slots."
        self.assertEqual(briefs.render_brief(plain), plain)
        with self.assertRaises(RuntimeError):
            briefs.render_brief(plain, briefs.STATIC)
        # a presentation is appended rather than dropped
        self.assertIn("Maya opens",
                      briefs.render_brief(plain, None, "Maya opens"))

    def test_an_unknown_slot_is_an_error_not_a_silent_hole(self):
        with self.assertRaises(RuntimeError):
            briefs.render_brief("x {{NOPE}} y")

    def test_unknown_or_empty_keys_fall_back_to_standard(self):
        self.assertIs(briefs.get_profile(None), briefs.STANDARD)
        self.assertIs(briefs.get_profile("nonsense"), briefs.STANDARD)
        self.assertIs(briefs.get_profile("STATIC"), briefs.STATIC)


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.studio_manifest_brief = None

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_built_in_template_is_used_by_default(self):
        text = studio.load_manifest_brief(self.cfg)
        self.assertIn("## SECTION 5", text)
        self.assertIn("ST/ZI/ZO: 2304x1296", text)

    def test_a_profile_key_or_object_both_work(self):
        by_key = studio.load_manifest_brief(self.cfg, "static")
        by_obj = studio.load_manifest_brief(self.cfg, briefs.STATIC)
        self.assertEqual(by_key, by_obj)
        self.assertIn("every shot is static (ST)", by_key)

    def test_a_custom_brief_with_a_howto_header_is_still_supported(self):
        custom = Path(self.tmp.name) / "brief.md"
        custom.write_text("how to use this\n\n---\n\nTHE PROMPT\n",
                          encoding="utf-8")
        self.cfg.studio_manifest_brief = str(custom)
        self.assertEqual(studio.load_manifest_brief(self.cfg), "THE PROMPT")

    def test_a_missing_custom_brief_says_so(self):
        self.cfg.studio_manifest_brief = str(Path(self.tmp.name) / "gone.md")
        with self.assertRaises(RuntimeError) as ctx:
            studio.load_manifest_brief(self.cfg)
        self.assertIn("does not exist", str(ctx.exception))


class GateTests(unittest.TestCase):
    def test_the_standard_profile_still_rejects_an_all_static_plan(self):
        cues = _cues(8, 5)  # 40s
        data = _shots([(f"S01_0{i}_SCN_ST.png", 2 * i - 1, 2 * i, "ST")
                       for i in range(1, 5)])
        faults, _ = studio.shotlist_pacing(data, cues, 12.0)
        self.assertTrue(any("ST is" in f for f in faults))

    def test_static_profile_accepts_an_all_static_plan(self):
        cues = _cues(8, 5)
        data = _shots([(f"S01_0{i}_SCN_ST.png", 2 * i - 1, 2 * i, "ST")
                       for i in range(1, 5)])
        faults, _ = studio.shotlist_pacing(data, cues, 12.0, briefs.STATIC)
        self.assertEqual(faults, [])

    def test_static_profile_allows_long_static_holds_within_the_cap(self):
        cues = _cues(6, 5)  # two 15s static shots: fine here, fatal normally
        data = _shots([("S01_01_SCN_ST.png", 1, 3, "ST"),
                       ("S01_02_SCN_ST.png", 4, 6, "ST")])
        self.assertEqual(
            studio.shotlist_pacing(data, cues, 20.0, briefs.STATIC)[0], [])
        self.assertTrue(studio.shotlist_pacing(data, cues, 20.0)[0])

    def test_static_profile_rejects_any_camera_motion(self):
        cues = _cues(8, 2)  # two 8s shots - well inside the 12s hold cap
        data = _shots([("S01_01_SCN_ST.png", 1, 4, "ST"),
                       ("S01_02_SCN_PR.png", 5, 8, "PR")])
        faults, _ = studio.shotlist_pacing(data, cues, 12.0, briefs.STATIC)
        self.assertEqual(len(faults), 1)
        self.assertIn("does not allow", faults[0])
        self.assertIn("S01_02_SCN_PR.png", faults[0])

    def test_static_profile_still_enforces_the_max_hold(self):
        cues = _cues(8, 5)  # one 40s shot
        data = _shots([("S01_01_SCN_ST.png", 1, 8, "ST")])
        faults, _ = studio.shotlist_pacing(data, cues, 12.0, briefs.STATIC)
        self.assertTrue(any("hold longer than" in f for f in faults))

    def test_long_hold_profile_accepts_20s_shots_that_12s_would_reject(self):
        cues = _cues(16, 5)  # 80s as four 20s motion shots
        data = _shots([("S01_01_SCN_ZI.png", 1, 4, "ZI"),
                       ("S01_02_SCN_PU.png", 5, 8, "PU"),
                       ("S01_03_SCN_ZO.png", 9, 12, "ZO"),
                       ("S01_04_SCN_PD.png", 13, 16, "PD")])
        self.assertTrue(studio.shotlist_pacing(data, cues, 12.0)[0])
        self.assertEqual(
            studio.shotlist_pacing(data, cues, 30.0, briefs.LONG_HOLDS)[0], [])

    def test_long_hold_profile_rejects_a_plan_chopped_into_short_shots(self):
        cues = _cues(16, 5)  # sixteen single-cue shots of 5s each
        data = _shots([(f"S01_{i:02d}_SCN_ZI.png", i, i, "ZI")
                       for i in range(1, 17)])
        faults, _ = studio.shotlist_pacing(data, cues, 30.0, briefs.LONG_HOLDS)
        self.assertTrue(any("hold under the 10s" in f for f in faults))

    def test_long_hold_profile_exempts_the_final_short_shot(self):
        cues = _cues(13, 5)  # three 20s shots, then a 5s closing shot
        data = _shots([("S01_01_SCN_ZI.png", 1, 4, "ZI"),
                       ("S01_02_SCN_PU.png", 5, 8, "PU"),
                       ("S01_03_SCN_ZO.png", 9, 12, "ZO"),
                       ("S01_04_SCN_PD.png", 13, 13, "PD")])
        self.assertEqual(
            studio.shotlist_pacing(data, cues, 30.0, briefs.LONG_HOLDS)[0], [])

    def test_long_hold_profile_keeps_the_motion_variety_rules(self):
        cues = _cues(16, 5)
        data = _shots([(f"S01_{i:02d}_SCN_PR.png", 4 * i - 3, 4 * i, "PR")
                       for i in range(1, 5)])
        faults, _ = studio.shotlist_pacing(data, cues, 30.0, briefs.LONG_HOLDS)
        self.assertTrue(any("motion PR" in f for f in faults))

    def test_the_review_passes_the_profile_to_the_pacing_gate(self):
        cues = _cues(8, 5)
        data = _shots([("S01_01_SCN_ST.png", 1, 4, "ST"),
                       ("S01_02_SCN_ST.png", 5, 8, "ST")])
        data["images"] = [{"file": "S01_01_SCN_ST.png", "prompt": "a"},
                          {"file": "S01_02_SCN_ST.png", "prompt": "b"}]
        with mock.patch.object(studio, "llm_generate",
                               side_effect=RuntimeError("no judge")):
            standard = studio.review_shotlist(self.cfg_stub(), data, cues, "j")
            static = studio.review_shotlist(self.cfg_stub(), data, cues, "j",
                                            profile=briefs.STATIC)
        self.assertTrue(any("ST is" in f for f in standard["faults"]))
        self.assertFalse(any("ST is" in f for f in static["faults"]))

    @staticmethod
    def cfg_stub():
        return load_config(ROOT / "config.yaml")


class PacingNoteTests(unittest.TestCase):
    def test_the_standard_note_is_the_one_the_planner_always_got(self):
        note = briefs.pacing_note(None, 960, 200, 12.0, 0.9)
        self.assertIn("the narration runs ~960s across 200 cues", note)
        self.assertIn("no shot holding longer than 12s", note)
        self.assertIn("80 is only the ABSOLUTE FLOOR", note)
        self.assertIn("each new concrete detail deserves its own visual", note)

    def test_the_long_hold_note_asks_for_grouping_not_splitting(self):
        note = briefs.pacing_note(briefs.LONG_HOLDS, 900, 180, 30.0, 0.9)
        self.assertIn("10-30s", note)
        self.assertIn("about 30 shots", note)     # 900 / 30
        self.assertIn("about 90 ", note)          # 900 / 10
        self.assertIn("do NOT split at every new concrete detail", note)
        self.assertNotIn("WELL ABOVE", note)

    def test_no_narration_means_no_note(self):
        self.assertEqual(briefs.pacing_note(None, 0, 0, 12.0, 0.9), "")


class _ChannelCase(unittest.TestCase):
    """A throw-away DB with one channel and a test client."""
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(tempfile.mkdtemp()) / "wr.db"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.oc = db.create_own_channel(conn, "Ch")
        conn.commit()
        conn.close()
        self.client = create_app(self.cfg).test_client()

    def _row(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        try:
            return db.get_own_channel(conn, self.oc)
        finally:
            conn.close()

    def _post(self, **fields):
        resp = self.client.post("/my-channels/edit",
                                data={"id": str(self.oc), "name": "Ch", **fields})
        self.assertIn(resp.status_code, (302, 303))


class ChannelSettingsTests(_ChannelCase):
    def test_a_new_channel_inherits_the_standard_brief(self):
        row = self._row()
        self.assertIsNone(row["brief_motion"])
        self.assertIsNone(row["brief_presentation"])

    def test_the_motion_profile_and_presentation_are_saved(self):
        self._post(brief_motion="static",
                   brief_presentation="  Maya opens.\nThen the families.  ")
        row = self._row()
        self.assertEqual(row["brief_motion"], "static")
        self.assertEqual(row["brief_presentation"],
                         "Maya opens.\nThen the families.")

    def test_standard_empty_or_unknown_all_mean_inherit(self):
        for raw in ("standard", "", "bogus"):
            self._post(brief_motion="long_holds")
            self.assertEqual(self._row()["brief_motion"], "long_holds")
            self._post(brief_motion=raw)
            self.assertIsNone(self._row()["brief_motion"], raw)

    def test_an_empty_presentation_clears_it(self):
        self._post(brief_presentation="Maya opens.")
        self._post(brief_presentation="   ")
        self.assertIsNone(self._row()["brief_presentation"])

    def test_the_effective_settings_carry_the_profile(self):
        self._post(brief_motion="long_holds", brief_presentation="Maya opens.")
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, pid, own_channel_id=self.oc)
        eff = settings.for_production(conn, db.get_production(conn, pid))
        conn.close()
        self.assertEqual(eff["brief_motion"], "long_holds")
        self.assertEqual(eff["brief_presentation"], "Maya opens.")

    def test_the_shots_stage_page_names_the_brief_in_force(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, pid, own_channel_id=self.oc, stage="shots")
        conn.commit()
        conn.close()
        self._post(brief_motion="static", brief_presentation="Maya opens.")
        page = self.client.get(f"/studio/{pid}?stage=shots").get_data(as_text=True)
        self.assertIn("Planning brief:", page)
        self.assertIn(briefs.STATIC.label, page)
        self.assertIn("presentation note", page)

    def test_the_channel_form_offers_every_preset_and_starter(self):
        page = self.client.get("/my-channels").get_data(as_text=True)
        for preset in briefs.MOTION_PRESETS.values():
            self.assertIn(preset.label, page)
        for label, _text in briefs.PRESENTATION_STARTERS.values():
            self.assertIn(label, page)
        self.assertIn('name="brief_presentation"', page)


class HoldRangeTests(unittest.TestCase):
    """A channel's own hold range lies over its motion preset and feeds the
    prompt AND the gates, so the two cannot disagree."""

    def test_no_override_returns_the_preset_itself(self):
        self.assertIs(briefs.resolve_profile(None), briefs.STANDARD)
        self.assertIs(briefs.resolve_profile("long_holds"), briefs.LONG_HOLDS)

    def test_a_channel_range_overrides_the_preset_range(self):
        prof = briefs.resolve_profile("long_holds", 15, 45)
        self.assertEqual((prof.min_hold, prof.max_hold), (15.0, 45.0))
        text = briefs.render_brief(briefs.read_template(), prof)
        self.assertIn("between 15 and 45 seconds", text)
        self.assertNotIn("between 10 and 30", text)

    def test_a_max_alone_leaves_the_preset_minimum(self):
        prof = briefs.resolve_profile("long_holds", None, 20)
        self.assertEqual((prof.min_hold, prof.max_hold), (10.0, 20.0))

    def test_a_min_on_a_preset_without_one_borrows_the_global_max(self):
        prof = briefs.resolve_profile("standard", 8, None, default_max=12.0)
        self.assertEqual((prof.min_hold, prof.max_hold), (8.0, 12.0))
        text = briefs.render_brief(briefs.read_template(), prof)
        self.assertIn("between 8 and 12 seconds", text)
        self.assertIn("must carry motion", text)

    def test_static_with_a_range_never_asks_for_motion(self):
        prof = briefs.resolve_profile("static", 10, 30)
        text = briefs.render_brief(briefs.read_template(), prof)
        self.assertIn("between 10 and 30 seconds", text)
        self.assertNotIn("must carry motion", text)
        note = briefs.pacing_note(prof, 900, 180, 30.0, 0.9)
        self.assertIn("10-30s", note)
        self.assertNotIn("carries motion", note)

    def test_a_min_not_below_the_max_is_dropped(self):
        prof = briefs.resolve_profile("standard", 30, 20, default_max=12.0)
        self.assertIsNone(prof.min_hold)
        self.assertEqual(prof.max_hold, 20.0)

    def test_a_static_channel_can_hold_longer_than_the_global_12s(self):
        prof = briefs.resolve_profile("static", None, 25)
        cues = _cues(10, 5)  # two 25s static shots
        data = _shots([("S01_01_SCN_ST.png", 1, 5, "ST"),
                       ("S01_02_SCN_ST.png", 6, 10, "ST")])
        self.assertTrue(studio.shotlist_pacing(data, cues, 12.0, prof)[0])
        self.assertEqual(
            studio.shotlist_pacing(data, cues, prof.max_hold, prof)[0], [])


class HoldRangeSettingsTests(_ChannelCase):
    def test_the_hold_range_is_saved_and_reaches_the_effective_settings(self):
        self._post(brief_motion="long_holds", brief_min_hold="12",
                   brief_max_hold="40")
        row = self._row()
        self.assertEqual((row["brief_min_hold"], row["brief_max_hold"]),
                         (12.0, 40.0))
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, pid, own_channel_id=self.oc)
        eff = settings.for_production(conn, db.get_production(conn, pid))
        conn.close()
        prof = briefs.resolve_profile(eff["brief_motion"], eff["brief_min_hold"],
                                      eff["brief_max_hold"])
        self.assertEqual((prof.min_hold, prof.max_hold), (12.0, 40.0))

    def test_empty_or_garbage_clears_the_override(self):
        self._post(brief_min_hold="12", brief_max_hold="40")
        self._post(brief_min_hold="", brief_max_hold="oops")
        row = self._row()
        self.assertIsNone(row["brief_min_hold"])
        self.assertIsNone(row["brief_max_hold"])

    def test_a_min_not_below_the_max_saves_nothing(self):
        self._post(brief_motion="static", brief_min_hold="30",
                   brief_max_hold="20")
        row = self._row()
        self.assertIsNone(row["brief_motion"])
        self.assertIsNone(row["brief_min_hold"])

    def test_the_shots_page_shows_the_range(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, pid, own_channel_id=self.oc, stage="shots")
        conn.commit()
        conn.close()
        self._post(brief_motion="long_holds", brief_min_hold="12",
                   brief_max_hold="40")
        page = self.client.get(f"/studio/{pid}?stage=shots").get_data(as_text=True)
        self.assertIn("holds 12-40s", page)


class PlannerIntegrationTests(unittest.TestCase):
    """The shots stage hands the planner the CHANNEL's rendered brief."""
    SRT = "".join(f"{i}\n00:00:{(i - 1) * 4:02d},000 --> "
                  f"00:00:{i * 4:02d},000\nCue number {i}.\n\n"
                  for i in range(1, 5))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.studio_manifest_brief = None
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.chan = db.create_own_channel(conn, "Ch")
        db.update_own_channel(conn, self.chan, brief_motion="static",
                              brief_presentation="Maya opens the video.")
        self.pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, self.pid, own_channel_id=self.chan)
        db.set_setting(conn, "shotlist_judge_provider", "judge-test")
        db.set_setting(conn, "shotlist_max_attempts", 1)
        conn.commit()
        conn.close()
        pdir = studio.prod_dir(self.cfg, self.pid)
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "bible.md").write_text("MAYA - the host.", encoding="utf-8")
        (pdir / "subtitles.srt").write_text(self.SRT, encoding="utf-8")
        self.pdir = pdir

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_planner_prompt_uses_the_channels_profile(self):
        seen = []
        plan = json.dumps({
            "shots": [{"cues": "1-4", "asset": "S01_01_SCN_ST.png",
                       "scene": "S01", "motion": "ST"}],
            "images": [{"file": "S01_01_SCN_ST.png", "prompt": "a host"}]})

        def fake(cfg, prompt, provider=None, max_tokens=None, temperature=1.0):
            if provider == "planner-test":
                seen.append(prompt)
                return plan
            raise RuntimeError("judge unavailable")

        with mock.patch.object(studio, "llm_generate", fake):
            autorun.run_stage(self.cfg, self.pid, "shots",
                              {"provider": "planner-test"})
        self.assertTrue(seen, "the planner was never called")
        prompt = seen[0]
        self.assertIn("every shot is static (ST)", prompt)
        self.assertIn("Maya opens the video.", prompt)
        self.assertNotIn("| SCN character beat | ZI", prompt)
        # the production keeps a record of the brief it was planned with
        used = self.pdir / "versions" / "shotlist" / "brief_used.md"
        self.assertIn("every shot is static (ST)",
                      used.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
