"""More motion presets, the Custom profile and the extra presentation starters.

Each preset/custom spec fills the brief's motion slots AND sets what the
shotlist review enforces, so the prompt and the gate cannot disagree.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import briefs, db, settings, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


def _ts(t):
    m, s = divmod(int(t), 60)
    return f"00:{m:02d}:{s:02d},000"


def _cues(count, secs):
    return [{"index": i + 1, "start": _ts(i * secs), "end": _ts((i + 1) * secs),
             "text": f"cue {i + 1}"} for i in range(count)]


def _plan(motions, span=2):
    """One shot per motion code, `span` cues each."""
    shots = [{"cues": f"{i * span + 1}-{(i + 1) * span}",
              "asset": f"S01_{i + 1:02d}_SCN_{m}.png", "scene": "S01",
              "motion": m} for i, m in enumerate(motions)]
    return {"shots": shots,
            "images": [{"file": s["asset"], "prompt": "p"} for s in shots]}


def _faults(motions, profile, secs=3, span=2, cap=12.0):
    cues = _cues(len(motions) * span, secs)
    return studio.shotlist_pacing(_plan(motions, span), cues, cap, profile)[0]


class PresetRenderTests(unittest.TestCase):
    def setUp(self):
        self.template = briefs.read_template()

    def render(self, profile):
        return briefs.render_brief(self.template, profile)

    def test_new_presets_are_registered_with_distinct_labels(self):
        keys = {"flow_safe", "zooms_only", "fast_paced", "documentary"}
        self.assertTrue(keys <= set(briefs.MOTION_PRESETS))
        labels = [p.label for p in briefs.MOTION_PRESETS.values()]
        self.assertEqual(len(labels), len(set(labels)))

    def test_flow_safe_never_offers_horizontal_pans(self):
        text = self.render(briefs.FLOW_SAFE)
        self.assertIn("ST, ZI, ZO, PU and PD", text)
        self.assertIn("ST/ZI/ZO: 2304x1296 · PU/PD: 2304x2160", text)
        self.assertNotIn("2880x1296", text)

    def test_zooms_only_names_only_zooms(self):
        text = self.render(briefs.ZOOMS_ONLY)
        self.assertIn("ST/ZI/ZO: 2304x1296", text)
        self.assertIn("zooms only", text)

    def test_fast_paced_states_its_hold_and_st_share(self):
        text = self.render(briefs.FAST_PACED)
        self.assertIn("fast-paced", text)
        self.assertIn("~25% of shots", text)
        self.assertEqual(briefs.FAST_PACED.max_hold, 6.0)

    def test_documentary_states_its_hold_range(self):
        text = self.render(briefs.resolve_profile("documentary"))
        self.assertIn("between 8 and 20 seconds", text)


class PresetGateTests(unittest.TestCase):
    def test_flow_safe_rejects_pans_but_accepts_tilts_and_zooms(self):
        bad = _faults(["ZI", "PL", "ZO", "PD", "PU"], briefs.FLOW_SAFE)
        self.assertTrue(any("does not allow" in f and "PL" in f for f in bad))
        ok = _faults(["ZI", "ZO", "PD", "PU", "ZI", "ZO", "PD", "PU", "ZI", "ST"],
                     briefs.FLOW_SAFE, secs=2)
        self.assertEqual(ok, [])

    def test_zooms_only_rejects_any_tilt(self):
        bad = _faults(["ZI", "ZO", "PU"], briefs.ZOOMS_ONLY)
        self.assertTrue(any("does not allow" in f for f in bad))
        self.assertEqual(
            _faults(["ZI", "ZO"] * 5, briefs.ZOOMS_ONLY), [])

    def test_fast_paced_allows_more_st_than_standard(self):
        motions = ["ST", "ZI", "ZO", "ZI"] * 2          # ST = 25%
        std = _faults(motions, briefs.STANDARD)
        fast = _faults(motions, briefs.FAST_PACED)
        self.assertTrue(any("ST is" in f for f in std))
        self.assertFalse(any("ST is" in f for f in fast))

    def test_documentary_caps_pans_tighter_than_zooms(self):
        motions = ["ZI", "ZO", "ZI", "ZO", "PL", "ZI", "ZO", "ZI", "ZO", "PU"]
        faults = _faults(motions, briefs.resolve_profile("documentary"),
                         secs=5, span=2, cap=30)
        self.assertEqual([f for f in faults if "motion" in f and "cap" in f],
                         [])
        heavy = ["ZI", "ZO", "PL", "PL", "ZI", "ZO", "ZI", "ZO"]
        faults = _faults(heavy, briefs.resolve_profile("documentary"),
                         secs=5, span=2, cap=30)
        self.assertTrue(any("motion PL" in f for f in faults))


class CustomSpecTests(unittest.TestCase):
    def test_normalize_orders_codes_and_drops_junk(self):
        spec = briefs.normalize_custom({
            "allowed": ["zo", "ZI", "XX", "st"], "st_max_share": "20",
            "st_max_hold": "3", "code_max_share": "", "rules": "  hi  "})
        self.assertEqual(spec["allowed"], ["ST", "ZI", "ZO"])
        self.assertEqual(spec["st_max_share"], 0.2)
        self.assertEqual(spec["st_max_hold"], 3.0)
        self.assertIsNone(spec["code_max_share"])
        self.assertEqual(spec["rules"], "hi")

    def test_normalize_accepts_stored_json_and_rejects_empty(self):
        self.assertEqual(
            briefs.normalize_custom(json.dumps({"allowed": ["ZI"]}))["allowed"],
            ["ZI"])
        for bad in (None, "", "not json", {"allowed": []}, {"allowed": ["QQ"]}):
            self.assertIsNone(briefs.normalize_custom(bad))

    def test_an_impossible_cap_is_an_error(self):
        err = briefs.custom_error({"allowed": ["ZI"], "code_max_share": 40})
        self.assertIn("raise a cap", err)
        self.assertIsNone(briefs.custom_error(
            {"allowed": ["ZI", "ZO"], "code_max_share": 60}))
        self.assertIsNotNone(briefs.custom_error({"allowed": []}))

    def test_profile_mirrors_the_spec(self):
        p = briefs.custom_profile({
            "allowed": ["ST", "ZI", "PU", "PD"], "st_max_share": 20,
            "st_max_hold": 3, "code_max_share": 50})
        self.assertEqual(p.key, "custom")
        self.assertEqual(p.allowed, ("ST", "ZI", "PU", "PD"))
        self.assertEqual((p.st_max_share, p.st_max_hold, p.code_max_share),
                         (0.2, 3.0, 0.5))
        self.assertEqual(p.slots["CANVAS_SPEC"],
                         "ST/ZI: 2304x1296 · PU/PD: 2304x2160")

    def test_no_st_means_no_st_limits_and_every_shot_moves(self):
        p = briefs.custom_profile({"allowed": ["ZI", "ZO"]})
        self.assertIsNone(p.st_max_share)
        self.assertIsNone(p.static_long_hold)
        self.assertIn("no static code", p.slots["MOTION_SECTION"])

    def test_all_codes_allowed_means_no_restriction(self):
        p = briefs.custom_profile({"allowed": list(briefs.MOTION_CODES)})
        self.assertIsNone(p.allowed)

    def test_st_only_is_the_static_policy(self):
        p = briefs.custom_profile({"allowed": ["ST"], "rules": "keep it calm"})
        self.assertEqual(p.allowed, ("ST",))
        self.assertIn("keep it calm", p.slots["MOTION_SECTION"])
        self.assertEqual(_faults(["ST", "ST"], p), [])

    def test_unusable_spec_falls_back_to_standard(self):
        self.assertIs(briefs.custom_profile(None), briefs.STANDARD)
        self.assertIs(briefs.custom_profile(
            {"allowed": ["ZI"], "code_max_share": 30}), briefs.STANDARD)

    def test_the_brief_carries_codes_canvas_and_the_creators_rules(self):
        p = briefs.custom_profile({
            "allowed": ["ZI", "ZO", "PU"], "rules": "ZO on every list."})
        text = briefs.render_brief(briefs.read_template(), p)
        self.assertNotIn("{{", text)
        self.assertIn("Allowed motion codes on this channel: ZI, ZO, PU.", text)
        self.assertIn("Never use ST, PL, PR, PD, PV", text)
        self.assertIn("ZO on every list.", text)
        self.assertIn("ZI/ZO: 2304x1296 · PU: 2304x2160", text)
        self.assertIn("`motion`: ZI | ZO | PU", text)

    def test_the_gate_enforces_the_ticked_codes_and_caps(self):
        p = briefs.custom_profile({"allowed": ["ZI", "ZO", "PU"],
                                   "code_max_share": 50})
        self.assertTrue(any("does not allow" in f
                            for f in _faults(["ZI", "ZO", "PL"], p)))
        self.assertTrue(any("motion ZI" in f
                            for f in _faults(["ZI", "ZI", "ZI", "ZO"], p)))
        self.assertEqual(_faults(["ZI", "ZO", "PU", "ZI", "ZO", "PU"], p), [])

    def test_the_hold_range_still_lies_over_a_custom_profile(self):
        p = briefs.resolve_profile("custom", 6, 15,
                                   custom={"allowed": ["ZI", "ZO"]})
        self.assertEqual((p.min_hold, p.max_hold), (6.0, 15.0))
        text = briefs.render_brief(briefs.read_template(), p)
        self.assertIn("between 6 and 15 seconds", text)

    def test_custom_without_a_spec_is_standard(self):
        self.assertIs(briefs.resolve_profile("custom"), briefs.STANDARD)


class StarterTests(unittest.TestCase):
    def test_the_new_starters_exist_and_are_distinct(self):
        starters = briefs.PRESENTATION_STARTERS
        for key in ("two_hosts", "first_person", "mascot", "host_bookends",
                    "subject_only"):
            self.assertIn(key, starters)
        texts = [t for _l, t in starters.values()]
        self.assertEqual(len(texts), len(set(texts)))
        self.assertGreaterEqual(len(starters), 8)


class _ChannelCase(unittest.TestCase):
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
        return self.client.post("/my-channels/edit",
                                data={"id": str(self.oc), "name": "Ch", **fields})


class GroupCapTests(unittest.TestCase):
    ALL = ["ST", "ZI", "ZO", "PL", "PR", "PU", "PD", "PV"]

    def test_normalize_reads_the_family_caps(self):
        spec = briefs.normalize_custom({
            "allowed": self.ALL, "pan_max_share": "15",
            "tilt_max_share": 0.3, "zoom_max_share": ""})
        self.assertEqual((spec["pan_max_share"], spec["tilt_max_share"],
                          spec["zoom_max_share"]), (0.15, 0.3, None))

    def test_each_family_cap_lands_on_its_codes(self):
        p = briefs.custom_profile({
            "allowed": self.ALL, "pan_max_share": 5,
            "tilt_max_share": 20, "zoom_max_share": 50})
        o = p.code_share_overrides
        self.assertEqual((o["PL"], o["PR"]), (0.05, 0.05))
        self.assertEqual((o["PU"], o["PD"], o["PV"]), (0.2, 0.2, 0.2))
        self.assertEqual((o["ZI"], o["ZO"]), (0.5, 0.5))

    def test_an_unset_family_keeps_the_standard_cap(self):
        p = briefs.custom_profile({"allowed": self.ALL, "tilt_max_share": 25})
        self.assertEqual(p.code_share_overrides["PL"], 0.10)
        self.assertEqual(p.code_share_overrides["PU"], 0.25)

    def test_zoom_left_empty_takes_whatever_is_left(self):
        p = briefs.custom_profile({"allowed": self.ALL})
        self.assertNotIn("ZI", p.code_share_overrides)
        self.assertNotIn("ZO", p.code_share_overrides)
        # 90% ZI/ZO is fine when the pans are barely used
        motions = ["ZI", "ZO"] * 4 + ["PL", "PU"]
        faults = _faults(motions, p, secs=5, span=2, cap=30)
        self.assertEqual([f for f in faults if "motion" in f and "cap" in f], [])

    def test_family_caps_work_with_few_codes(self):
        p = briefs.custom_profile({"allowed": ["ZI", "PR"],
                                   "pan_max_share": 30, "zoom_max_share": 70})
        self.assertEqual(p.code_share_overrides, {"ZI": 0.7, "PR": 0.3})
        self.assertIn("ZI ~70%", p.slots["MOTION_SECTION"])
        self.assertNotIn("100%", p.slots["MOTION_SECTION"])

    def test_caps_that_cannot_cover_the_shots_are_an_error(self):
        err = briefs.custom_error({"allowed": ["ZI", "PR"],
                                   "pan_max_share": 10, "zoom_max_share": 30})
        self.assertIn("raise a cap", err)
        self.assertIsNone(briefs.custom_error(
            {"allowed": ["ZI", "PR"], "pan_max_share": 30,
             "zoom_max_share": 70}))

    def test_the_gate_enforces_a_family_cap(self):
        prof = briefs.custom_profile({
            "allowed": self.ALL, "pan_max_share": 10})
        motions = ["PL", "PL", "ZI", "ZO", "PU", "PD", "PV", "ZI", "ZO", "ST"]
        faults = _faults(motions, prof, secs=5, span=2, cap=30)
        self.assertTrue(any("motion PL" in f for f in faults))


class CustomChannelTests(_ChannelCase):
    def test_a_ticked_pan_or_tilt_group_needs_its_share(self):
        self._post(brief_motion="static")
        for allowed, share in ((["ZI", "PL"], {}),
                               (["ZI", "PU"], {"custom_pan_share": "10"})):
            resp = self._post(brief_motion="custom", custom_allowed=allowed,
                              **share)
            self.assertIn("enter%20a%20max%20share", resp.headers["Location"])
            self.assertEqual(self._row()["brief_motion"], "static")
        # ZI/ZO alone, no share at all, is fine: they take what is left
        self._post(brief_motion="custom", custom_allowed=["ZI", "ZO"])
        self.assertEqual(self._row()["brief_motion"], "custom")

    def test_the_boxes_grey_out_with_their_codes(self):
        page = self.client.get("/my-channels").get_data(as_text=True)
        self.assertIn('data-codes="PL,PR"', page)
        self.assertIn("f.disabled = !on", page)

    def test_family_caps_are_saved_and_shown(self):
        self._post(brief_motion="custom",
                   custom_allowed=["ST", "ZI", "ZO", "PL", "PR", "PU", "PD", "PV"], custom_pan_share="8",
                   custom_tilt_share="25", custom_zoom_share="45")
        spec = json.loads(self._row()["brief_custom"])
        self.assertEqual((spec["pan_max_share"], spec["tilt_max_share"],
                          spec["zoom_max_share"]), (0.08, 0.25, 0.45))
        page = self.client.get("/my-channels").get_data(as_text=True)
        for v in ('value="8"', 'value="25"', 'value="45"'):
            self.assertIn(v, page)

    def test_custom_is_saved_with_its_spec(self):
        resp = self._post(brief_motion="custom",
                          custom_allowed=["ZI", "ZO", "PU"],
                          custom_tilt_share="40", custom_st_share="", custom_st_hold="",
                          custom_code_share="60", custom_rules=" ZO on lists ")
        self.assertIn(resp.status_code, (302, 303))
        row = self._row()
        self.assertEqual(row["brief_motion"], "custom")
        spec = json.loads(row["brief_custom"])
        self.assertEqual(spec["allowed"], ["ZI", "ZO", "PU"])
        self.assertEqual(spec["code_max_share"], 0.6)
        self.assertEqual(spec["rules"], "ZO on lists")

    def test_a_bad_custom_saves_nothing(self):
        self._post(brief_motion="static")
        for data in ({"custom_allowed": []},
                     {"custom_allowed": ["ZI"], "custom_code_share": "30"}):
            resp = self._post(brief_motion="custom", **data)
            self.assertIn("Custom%20motion", resp.headers["Location"])
            row = self._row()
            self.assertEqual(row["brief_motion"], "static")
            self.assertIsNone(row["brief_custom"])

    def test_switching_to_a_preset_keeps_the_custom_spec(self):
        self._post(brief_motion="custom", custom_allowed=["ZI", "ZO"])
        self._post(brief_motion="static")
        row = self._row()
        self.assertEqual(row["brief_motion"], "static")
        self.assertEqual(json.loads(row["brief_custom"])["allowed"],
                         ["ZI", "ZO"])

    def test_effective_settings_and_the_profile_carry_the_spec(self):
        self._post(brief_motion="custom", custom_allowed=["ZI", "PU"],
                   custom_tilt_share="40", custom_rules="tilt for comparisons")
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, pid, own_channel_id=self.oc)
        eff = settings.for_production(conn, db.get_production(conn, pid))
        conn.close()
        self.assertEqual(eff["brief_motion"], "custom")
        self.assertEqual(eff["brief_custom"]["allowed"], ["ZI", "PU"])
        prof = briefs.resolve_profile(eff["brief_motion"],
                                      custom=eff["brief_custom"])
        self.assertEqual(prof.allowed, ("ZI", "PU"))

    def test_the_form_offers_custom_and_its_fields(self):
        page = self.client.get("/my-channels").get_data(as_text=True)
        self.assertTrue(briefs.CUSTOM_LABEL in page)
        for name in ("custom_allowed", "custom_st_share", "custom_st_hold",
                     "custom_pan_share", "custom_tilt_share",
                     "custom_zoom_share", "custom_rules"):
            self.assertTrue(f'name="{name}"' in page, name)

    def test_the_presenter_starter_has_a_custom_option_that_clears_the_box(self):
        page = self.client.get("/my-channels").get_data(as_text=True)
        self.assertTrue('<option value="__custom__">Custom - write my own</option>'
                        in page)
        self.assertTrue("box.value = ''; box.focus()" in page)
        # the starters still fill the box, and the dropdown resets afterwards
        self.assertTrue("box.value = this.value" in page)
        self.assertTrue("this.selectedIndex = 0" in page)

    def test_the_two_brief_dropdowns_have_room_between_them(self):
        page = self.client.get("/my-channels").get_data(as_text=True)
        self.assertTrue('<div class="inline" style="gap:28px">\n'
                        '              <label>motion &amp; pacing' in page)

    def test_the_edit_form_is_grouped_and_keeps_every_field(self):
        import re
        page = self.client.get("/my-channels").get_data(as_text=True)
        edit = page[page.index('action="/my-channels/edit"'):]
        edit = edit[:edit.index("</form>")]
        titles = re.findall(r'<summary>(.*?) <span', edit)
        self.assertEqual(titles, [
            "Basics", "Production defaults", "Visual style, bible &amp; references",
            "Planning brief", "Auto Run", "Quality gates"])
        groups = re.split(r'<details class="grp"', edit)[1:]
        self.assertEqual(len(groups), 6)
        where = {}
        for title, body in zip(titles, groups):
            for name in re.findall(r'name="([a-z_]+)"', body):
                where[name] = title
        for name, title in (
                ("name", "Basics"), ("default_voice", "Production defaults"),
                ("style", "Visual style, bible &amp; references"),
                ("bible_dir", "Visual style, bible &amp; references"),
                ("brief_motion", "Planning brief"),
                ("brief_presentation", "Planning brief"),
                ("custom_allowed", "Planning brief"),
                ("run_window_start", "Auto Run"),
                ("script_min_rating", "Quality gates"),
                ("shotlist_min_alignment", "Quality gates")):
            self.assertEqual(where.get(name), title, name)
        self.assertTrue("</details>" in edit and edit.count("<details") == 6)

    def test_the_groups_keep_their_contents_off_the_border(self):
        page = self.client.get("/my-channels").get_data(as_text=True)
        self.assertTrue("padding: 0 16px 16px;" in page)

    def _brief_post(self, **fields):
        return self.client.post("/my-channels/brief",
                                data={"id": str(self.oc), **fields})

    def test_the_planning_brief_saves_on_its_own(self):
        self._post(brief_motion="static", genre="Keep", style="old style")
        resp = self._brief_post(
            brief_motion="custom", custom_allowed=["ZI", "ZO"],
            custom_rules="r", brief_min_hold="5", brief_max_hold="9",
            brief_presentation=" Host opens. ",
            # everything else on the form is ignored by this save
            name="IGNORED", genre="Ignored", style="new style")
        self.assertIn(f"#edit-{self.oc}-brief", resp.headers["Location"])
        row = self._row()
        self.assertEqual(row["brief_motion"], "custom")
        self.assertEqual(json.loads(row["brief_custom"])["allowed"], ["ZI", "ZO"])
        self.assertEqual((row["brief_min_hold"], row["brief_max_hold"]), (5.0, 9.0))
        self.assertEqual(row["brief_presentation"], "Host opens.")
        self.assertEqual(row["name"], "Ch")
        self.assertEqual(row["genre"], "Keep")
        self.assertEqual(row["style"], "old style")

    def test_a_preset_pick_with_its_own_hold_range_overrides_the_preset(self):
        self._brief_post(brief_motion="documentary", brief_min_hold="6",
                         brief_max_hold="14")
        row = self._row()
        prof = briefs.resolve_profile(row["brief_motion"], row["brief_min_hold"],
                                      row["brief_max_hold"])
        self.assertEqual(prof.key, "documentary")
        self.assertEqual((prof.min_hold, prof.max_hold), (6.0, 14.0))

    def test_a_bad_brief_save_changes_nothing_and_says_why(self):
        self._brief_post(brief_motion="static")
        for data in ({"brief_motion": "custom"},
                     {"brief_min_hold": "9", "brief_max_hold": "5"}):
            resp = self._brief_post(**data)
            self.assertIn("error=", resp.headers["Location"])
            self.assertIn(f"#edit-{self.oc}-brief", resp.headers["Location"])
            self.assertEqual(self._row()["brief_motion"], "static")
            self.assertIsNone(self._row()["brief_min_hold"])

    def test_the_brief_save_button_and_group_ids_are_on_the_form(self):
        page = self.client.get("/my-channels").get_data(as_text=True)
        self.assertTrue('formaction="/my-channels/brief"' in page)
        self.assertTrue("save planning brief" in page)
        for gid in ("basics", "production", "creative", "brief", "autorun",
                    "gates"):
            self.assertTrue(f'id="edit-{self.oc}-{gid}"' in page, gid)

    def test_the_form_shows_the_saved_spec(self):
        self._post(brief_motion="custom", custom_allowed=["ZI", "ZO"],
                   custom_rules="unique-rule-text")
        page = self.client.get("/my-channels").get_data(as_text=True)
        self.assertTrue("unique-rule-text" in page)
        self.assertTrue('value="PL" checked' not in page)
        self.assertTrue('value="ZI" checked' in page)


if __name__ == "__main__":
    unittest.main()
