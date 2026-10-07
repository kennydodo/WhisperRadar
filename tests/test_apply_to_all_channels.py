"""Apply a setting to ALL channels.

Two actions, both limited to db.APPLY_ALL_FIELDS (production defaults):
  - Settings page: save the global value and make every channel inherit it
    (channel override -> NULL).
  - Channel form: save this channel, then copy its saved value to every other
    channel.
Productions are never touched.

Run: python -m unittest tests.test_apply_to_all_channels
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402
from whisperradar import db, settings, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        self.a = db.create_own_channel(self.conn, "Alpha")
        self.b = db.create_own_channel(self.conn, "Bravo")
        self.c = db.create_own_channel(self.conn, "Charlie")
        db.set_setting(self.conn, "show_apply_all", "1")   # opt-in, see SwitchTests
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def row(self, oc, fresh=True):
        return db.get_own_channel(self.conn, oc)

    def set_(self, oc, **fields):
        db.update_own_channel(self.conn, oc, **fields)


class WhitelistTests(_Base):
    def test_only_production_defaults_are_eligible(self):
        # every eligible field has a global setting of the same name ...
        for field in db.APPLY_ALL_FIELDS:
            self.assertIn(field, settings.SPEC_BY_KEY, field)
            self.assertIn(field, db._OWN_CHANNEL_FIELDS, field)
        # ... and channel-specific identity / art direction never is
        for field in ("name", "genre", "youtube_handle", "default_voice",
                      "bible", "bible_dir", "refs_dir", "style",
                      "flow_project_url", "watched_channels", "active",
                      "renderly_channel_id", "brief_motion"):
            self.assertNotIn(field, db.APPLY_ALL_FIELDS, field)

    def test_helpers_refuse_other_fields(self):
        for fn in (lambda: db.channels_overriding(self.conn, "name"),
                   lambda: db.reset_channel_overrides(self.conn, "default_voice"),
                   lambda: db.copy_channel_value_to_all(self.conn, self.a,
                                                        "bible_dir")):
            with self.assertRaises(ValueError):
                fn()


class ResetOverridesTests(_Base):
    def test_every_channel_inherits_afterwards(self):
        self.set_(self.a, render_resolution="2k")
        self.set_(self.b, render_resolution="4k")
        names = db.reset_channel_overrides(self.conn, "render_resolution")
        self.assertEqual(sorted(names), ["Alpha", "Bravo"])
        for oc in (self.a, self.b, self.c):
            self.assertIsNone(self.row(oc)["render_resolution"])

    def test_returns_only_channels_that_changed(self):
        self.set_(self.a, per_day=3)
        self.assertEqual(db.reset_channel_overrides(self.conn, "per_day"),
                         ["Alpha"])
        self.assertEqual(db.reset_channel_overrides(self.conn, "per_day"), [])

    def test_blank_text_counts_as_not_overriding(self):
        self.set_(self.a, topic_pick="")
        self.assertEqual(db.channels_overriding(self.conn, "topic_pick"), [])

    def test_zero_is_a_real_override(self):
        self.set_(self.a, default_upscale=0, autorun_enabled=0)
        self.assertEqual([r["name"] for r in
                          db.channels_overriding(self.conn, "default_upscale")],
                         ["Alpha"])
        db.reset_channel_overrides(self.conn, "autorun_enabled")
        self.assertIsNone(self.row(self.a)["autorun_enabled"])

    def test_other_fields_and_productions_are_untouched(self):
        self.set_(self.a, render_resolution="2k", per_day=2,
                  default_voice="v", bible_dir="X:/b")
        pid = db.create_production(self.conn, "P", "general", None, None)
        db.update_production(self.conn, pid, own_channel_id=self.a,
                             render_mode="flow", voice="pv")
        db.reset_channel_overrides(self.conn, "render_resolution")
        row = self.row(self.a)
        self.assertEqual((row["per_day"], row["default_voice"],
                          row["bible_dir"]), (2, "v", "X:/b"))
        prod = db.get_production(self.conn, pid)
        self.assertEqual((prod["render_mode"], prod["voice"], prod["own_channel_id"]),
                         ("flow", "pv", self.a))

    def test_effective_value_is_the_global_one(self):
        self.set_(self.a, render_resolution="4k")
        pid = db.create_production(self.conn, "P", "general", None, None)
        db.update_production(self.conn, pid, own_channel_id=self.a)
        db.set_setting(self.conn, "render_resolution", "2k")
        db.reset_channel_overrides(self.conn, "render_resolution")
        eff = settings.for_production(self.conn, db.get_production(self.conn, pid))
        self.assertEqual(eff["render_resolution"], "2k")


class CopyToAllTests(_Base):
    def test_copies_the_saved_value_to_every_other_channel(self):
        self.set_(self.a, render_resolution="2k")
        self.set_(self.b, render_resolution="4k")
        names = db.copy_channel_value_to_all(self.conn, self.a, "render_resolution")
        self.assertEqual(sorted(names), ["Bravo", "Charlie"])
        for oc in (self.a, self.b, self.c):
            self.assertEqual(self.row(oc)["render_resolution"], "2k")

    def test_unchanged_channels_are_not_reported(self):
        self.set_(self.a, per_day=2)
        self.set_(self.b, per_day=2)
        self.assertEqual(db.copy_channel_value_to_all(self.conn, self.a, "per_day"),
                         ["Charlie"])

    def test_inherit_on_the_source_makes_the_others_inherit(self):
        self.set_(self.b, per_day=5)
        db.copy_channel_value_to_all(self.conn, self.a, "per_day")  # a: NULL
        self.assertIsNone(self.row(self.b)["per_day"])

    def test_zero_is_copied_as_zero(self):
        self.set_(self.a, default_upscale=0)
        db.copy_channel_value_to_all(self.conn, self.a, "default_upscale")
        self.assertEqual(self.row(self.c)["default_upscale"], 0)

    def test_other_fields_untouched(self):
        self.set_(self.a, per_day=2)
        self.set_(self.b, default_voice="bv", render_resolution="4k")
        db.copy_channel_value_to_all(self.conn, self.a, "per_day")
        row = self.row(self.b)
        self.assertEqual((row["default_voice"], row["render_resolution"]),
                         ("bv", "4k"))

    def test_unknown_source_channel(self):
        with self.assertRaises(ValueError):
            db.copy_channel_value_to_all(self.conn, 9999, "per_day")

    def test_single_channel_is_a_noop(self):
        for oc in (self.b, self.c):
            db.remove_own_channel(self.conn, oc)
        self.set_(self.a, per_day=2)
        self.assertEqual(db.copy_channel_value_to_all(self.conn, self.a, "per_day"),
                         [])


class SettingsRouteTests(_Base):
    def post(self, **extra):
        data = {"render_resolution": "2k"}
        data.update(extra)
        return self.client.post("/settings/save", data=data)

    def test_save_with_apply_all_resets_every_channel(self):
        self.set_(self.a, render_resolution="4k")
        self.set_(self.b, render_resolution="1080p")
        resp = self.post(apply_all="render_resolution")
        self.assertIn(resp.status_code, (302, 303))
        self.assertEqual(settings.load(self.conn)["render_resolution"], "2k")
        for oc in (self.a, self.b, self.c):
            self.assertIsNone(self.row(oc)["render_resolution"])
        from urllib.parse import unquote_plus
        msg = unquote_plus(resp.headers["Location"])
        self.assertIn("all channels now follow the global value (2k)", msg)
        self.assertIn("2 reset: Alpha, Bravo", msg)

    def test_the_message_says_when_nothing_needed_resetting(self):
        from urllib.parse import unquote_plus
        resp = self.post(apply_all="render_resolution")
        msg = unquote_plus(resp.headers["Location"])
        self.assertIn("no channel had its own value", msg)
        self.assertIn("already follows the global value (2k)", msg)

    def test_plain_save_leaves_channel_overrides_alone(self):
        self.set_(self.a, render_resolution="4k")
        self.post()
        self.assertEqual(self.row(self.a)["render_resolution"], "4k")

    def test_only_the_named_field_is_applied(self):
        self.set_(self.a, render_resolution="4k", per_day=3)
        self.post(apply_all="render_resolution")
        self.assertEqual(self.row(self.a)["per_day"], 3)

    def test_several_fields_at_once_and_duplicates(self):
        self.set_(self.a, render_resolution="4k", per_day=3)
        self.client.post("/settings/save", data={
            "render_resolution": "2k", "per_day": "1",
            "apply_all": ["render_resolution", "per_day", "per_day"]})
        self.assertIsNone(self.row(self.a)["render_resolution"])
        self.assertIsNone(self.row(self.a)["per_day"])

    def test_a_non_eligible_field_is_refused_with_a_warning(self):
        self.set_(self.a, default_voice="keep", bible_dir="X:/keep")
        resp = self.post(apply_all="default_voice")
        self.assertEqual(self.row(self.a)["default_voice"], "keep")
        self.assertIn("cannot", resp.headers["Location"].lower().replace("%20", " "))
        resp = self.post(apply_all="nonsense")
        self.assertIn(resp.status_code, (302, 303))
        self.assertEqual(self.row(self.a)["bible_dir"], "X:/keep")

    def test_the_global_value_is_saved_before_the_reset(self):
        self.set_(self.a, per_day=9)
        self.client.post("/settings/save",
                         data={"per_day": "2", "apply_all": "per_day"})
        self.assertEqual(settings.load(self.conn)["per_day"], 2)
        self.assertIsNone(self.row(self.a)["per_day"])


class ChannelRouteTests(_Base):
    def edit(self, oc, **data):
        form = {"id": str(oc), "name": self.row(oc)["name"]}
        form.update(data)
        return self.client.post("/my-channels/edit", data=form)

    def test_save_and_copy_uses_the_value_just_typed(self):
        self.set_(self.b, render_resolution="4k")
        resp = self.edit(self.a, render_resolution="2k",
                         apply_all="render_resolution")
        self.assertIn(resp.status_code, (302, 303))
        for oc in (self.a, self.b, self.c):
            self.assertEqual(self.row(oc)["render_resolution"], "2k")

    def test_plain_save_does_not_touch_other_channels(self):
        self.set_(self.b, render_resolution="4k")
        self.edit(self.a, render_resolution="2k")
        self.assertEqual(self.row(self.b)["render_resolution"], "4k")
        self.assertEqual(self.row(self.a)["render_resolution"], "2k")

    def test_the_message_names_the_channels_that_changed(self):
        from urllib.parse import unquote_plus
        self.set_(self.b, per_day=4)
        resp = self.edit(self.a, per_day="2", apply_all="per_day")
        msg = unquote_plus(resp.headers["Location"])
        self.assertIn("copied to 2 other channel(s): Bravo, Charlie", msg)
        resp = self.edit(self.a, per_day="2", apply_all="per_day")
        self.assertIn("every other channel already had this value",
                      unquote_plus(resp.headers["Location"]))

    def test_inherit_is_copied_too(self):
        self.set_(self.b, default_upscale=3)
        self.edit(self.a, default_upscale="", apply_all="default_upscale")
        self.assertIsNone(self.row(self.b)["default_upscale"])

    def test_numeric_value_is_clamped_then_copied(self):
        self.edit(self.a, per_day="999", apply_all="per_day")
        self.assertEqual(self.row(self.c)["per_day"], 50)

    def test_voice_and_bible_cannot_be_applied_to_all(self):
        self.set_(self.b, default_voice="bv", bible_dir="X:/b")
        resp = self.edit(self.a, default_voice="av", bible_dir="X:/a",
                         apply_all=["default_voice", "bible_dir"])
        self.assertEqual(self.row(self.b)["default_voice"], "bv")
        self.assertEqual(self.row(self.b)["bible_dir"], "X:/b")
        self.assertEqual(self.row(self.a)["default_voice"], "av")
        self.assertIn("not+applicable",
                      resp.headers["Location"].replace("%20", "+"))

    def test_invalid_value_is_not_copied_raw(self):
        # the route validates first; a bad resolution becomes inherit (NULL)
        self.set_(self.b, render_resolution="4k")
        self.edit(self.a, render_resolution="bogus",
                  apply_all="render_resolution")
        self.assertIsNone(self.row(self.b)["render_resolution"])

    def test_unknown_channel_changes_nothing(self):
        self.set_(self.b, per_day=4)
        resp = self.client.post("/my-channels/edit", data={
            "id": "9999", "per_day": "1", "apply_all": "per_day"})
        self.assertIn("Unknown", resp.headers["Location"])
        self.assertEqual(self.row(self.b)["per_day"], 4)


class PageTests(_Base):
    def test_settings_page_has_a_button_only_on_eligible_settings(self):
        html = self.client.get("/settings").get_data(as_text=True)
        for key in ("render_resolution", "per_day", "default_engine"):
            self.assertIn(f"wrApplyAll(this, '{key}'", html)
        self.assertNotIn("wrApplyAll(this, 'scheduler_enabled'", html)
        self.assertNotIn("wrApplyAll(this, 'llm_default'", html)
        # never a submit button: Enter must not trigger apply-to-all
        self.assertNotIn('name="apply_all"', html)

    def test_settings_confirm_shows_the_override_count(self):
        self.set_(self.a, render_resolution="4k")
        self.set_(self.b, render_resolution="2k")
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("apply to all channels (2 override)", html)

    def test_channels_page_ships_the_whitelist_to_the_script(self):
        html = self.client.get("/my-channels").get_data(as_text=True)
        self.assertIn("wrApplyAll", html)
        self.assertIn('"render_resolution"', html)
        shipped = html.split("function wrApplyAll")[1].split("var fields = ")[1].split(";")[0]
        self.assertIn("render_resolution", shipped)
        for field in ("default_voice", "bible_dir", "refs_dir", "name"):
            self.assertNotIn(f'"{field}"', shipped)


class SwitchTests(_Base):
    """The buttons exist only for a user who ticked Settings > Service handling."""

    def off(self):
        db.set_setting(self.conn, "show_apply_all", "0")

    def test_off_by_default(self):
        self.assertFalse(settings.SPEC_BY_KEY["show_apply_all"]["default"])
        fresh = tempfile.TemporaryDirectory()
        try:
            conn = db.connect(Path(fresh.name) / "x.db")
            db.init_db(conn)
            self.assertFalse(settings.load(conn)["show_apply_all"])
            conn.close()
        finally:
            fresh.cleanup()

    def test_the_checkbox_lives_in_service_handling(self):
        groups = dict(settings.GROUPS)
        self.assertIn("show_apply_all", groups["Service handling"])
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('name="show_apply_all"', html)

    def test_the_checkbox_saves_on_and_off(self):
        self.off()
        # a ticked box posts hidden "0" then "on"; an unticked one only "0"
        self.client.post("/settings/save",
                         data={"show_apply_all": ["0", "on"]})
        self.assertTrue(settings.load(self.conn)["show_apply_all"])
        self.client.post("/settings/save", data={"show_apply_all": ["0"]})
        self.assertFalse(settings.load(self.conn)["show_apply_all"])

    def test_the_checkbox_is_rendered_checked_only_when_on(self):
        html = self.client.get("/settings").get_data(as_text=True)
        seg = html.split('id="f-show_apply_all"')[1][:80]
        self.assertIn("checked", seg)
        self.off()
        html = self.client.get("/settings").get_data(as_text=True)
        seg = html.split('id="f-show_apply_all"')[1][:80]
        self.assertNotIn("checked", seg)

    def test_untouched_other_saves_do_not_flip_it(self):
        self.client.post("/settings/save", data={"per_day": "2"})
        self.assertTrue(settings.load(self.conn)["show_apply_all"])

    def test_buttons_hidden_on_the_settings_page_when_off(self):
        self.off()
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertNotIn("wrApplyAll(this,", html)
        self.assertNotIn("data-apply-all", html)

    def test_buttons_shown_on_the_settings_page_when_on(self):
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("wrApplyAll(this, 'render_resolution'", html)

    def test_channel_page_ships_no_fields_when_off(self):
        self.off()
        html = self.client.get("/my-channels").get_data(as_text=True)
        shipped = html.split("function wrApplyAll")[1].split("var fields = ")[1].split(";")[0]
        self.assertEqual(shipped.strip(), "[]")

    def test_channel_page_ships_the_fields_when_on(self):
        html = self.client.get("/my-channels").get_data(as_text=True)
        shipped = html.split("function wrApplyAll")[1].split("var fields = ")[1].split(";")[0]
        self.assertIn("render_resolution", shipped)

    def test_inherit_labels_still_show_when_off(self):
        self.off()
        html = self.client.get("/my-channels").get_data(as_text=True)
        self.assertIn("var globals_ = ", html)

    def test_the_server_refuses_settings_apply_when_off(self):
        self.off()
        self.set_(self.a, render_resolution="4k", default_upscale=4)
        resp = self.client.post("/settings/save", data={
            "render_resolution": "2k", "apply_all": "render_resolution"})
        self.assertEqual(self.row(self.a)["render_resolution"], "4k")
        self.assertIn("switched off",
                      __import__("urllib.parse").parse.unquote_plus(
                          resp.headers["Location"]))
        # the plain save still happened
        self.assertEqual(settings.load(self.conn)["render_resolution"], "2k")

    def test_the_server_refuses_channel_copy_when_off(self):
        self.off()
        self.set_(self.b, per_day=4)
        resp = self.client.post("/my-channels/edit", data={
            "id": str(self.a), "name": "Alpha", "per_day": "2",
            "apply_all": "per_day"})
        self.assertEqual(self.row(self.b)["per_day"], 4)
        self.assertEqual(self.row(self.a)["per_day"], 2)    # own save kept
        self.assertIn("switched+off", resp.headers["Location"].replace("%20", "+"))

    def test_ticking_the_box_in_the_same_save_does_not_apply_anything(self):
        self.off()
        self.set_(self.a, per_day=3)
        self.client.post("/settings/save", data={
            "show_apply_all": ["0", "on"], "per_day": "1"})
        self.assertEqual(self.row(self.a)["per_day"], 3)    # no apply_all sent
        self.assertTrue(settings.load(self.conn)["show_apply_all"])


class InheritLabelTests(_Base):
    def test_channels_page_ships_the_global_values_for_the_inherit_label(self):
        db.set_setting(self.conn, "render_resolution", "2k")
        db.set_setting(self.conn, "per_day", "4")
        html = self.client.get("/my-channels").get_data(as_text=True)
        shipped = html.split("var globals_ = ")[1].split(";")[0]
        self.assertIn('"render_resolution": "2k"', shipped)
        self.assertIn('"per_day": "4"', shipped)
        self.assertNotIn("default_voice", shipped)
        self.assertIn("inherit (global: ", html)


if __name__ == "__main__":
    unittest.main()
