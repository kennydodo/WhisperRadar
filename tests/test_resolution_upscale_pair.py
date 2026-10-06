"""Render resolution and Upscale tier are ONE decision; the resolution wins.

  1080p <-> 1   2k <-> 2   4k <-> 4   flow-native <-> 0     (tier 3 is gone)

Covers the pure helper, global save, channel save, the effective value used by
the pipeline, the dropdown lists/JS, the warnings, and what is written for
ImgToVideo.

Run: python -m unittest tests.test_resolution_upscale_pair
"""
import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from urllib.parse import unquote_plus

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import autorun, db, settings, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

R = settings.reconcile_resolution_upscale


def _png(path: Path, w: int, h: int):
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    raw = b"".join(b"\x00" + b"\x00" * 3 * w for _ in range(1))  # 1 row is enough
    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class HelperTests(unittest.TestCase):
    def test_mapping_is_one_to_one_and_has_no_tier_3(self):
        self.assertEqual(settings.RESOLUTION_UPSCALE,
                         {"1080p": 1, "2k": 2, "4k": 4, "flow-native": 0})
        self.assertEqual(sorted(settings.UPSCALE_LABELS), [0, 1, 2, 4])
        for res, tier in settings.RESOLUTION_UPSCALE.items():
            self.assertEqual(settings.UPSCALE_RESOLUTION[tier], res)

    def test_every_resolution_choice_has_a_tier(self):
        for res in settings.SPEC_BY_KEY["render_resolution"]["choices"]:
            self.assertIsNotNone(settings.upscale_for_resolution(res), res)

    def test_tier_3_reads_as_2(self):
        self.assertEqual(settings.normalize_upscale(3), 2)
        self.assertEqual(settings.normalize_upscale(4), 4)

    def test_resolution_decides_the_upscale(self):
        self.assertEqual(R("4k", 2, True, True)[:2], ("4k", 4))
        self.assertIn("did not match", R("4k", 2, True, True)[2])
        self.assertEqual(R("4k", 4, True, True), ("4k", 4, ""))
        self.assertEqual(R("flow-native", 2, True, True)[:2], ("flow-native", 0))

    def test_upscale_alone_decides_the_resolution(self):
        self.assertEqual(R(None, 4, False, True), ("4k", 4, ""))
        self.assertEqual(R(None, 3, False, True), ("2k", 2, ""))
        self.assertEqual(R(None, 0, False, True), ("flow-native", 0, ""))

    def test_inherit_stays_inherit(self):
        self.assertEqual(R(None, None, True, True), (None, None, ""))
        self.assertEqual(R("", None, True, True), ("", None, ""))

    def test_a_set_resolution_with_inherited_tier_fills_the_tier(self):
        self.assertEqual(R("1080p", None, True, True), ("1080p", 1, ""))


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        self.oc = db.create_own_channel(self.conn, "Ch")
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def row(self):
        return db.get_own_channel(self.conn, self.oc)

    def eff(self):
        pid = db.create_production(self.conn, "P", "general", None, None)
        db.update_production(self.conn, pid, own_channel_id=self.oc)
        return settings.for_production(self.conn, db.get_production(self.conn, pid))


class GlobalSaveTests(_Base):
    def save(self, **form):
        return self.client.post("/settings/save", data=form)

    def test_changing_the_resolution_moves_the_tier(self):
        self.save(render_resolution="4k", default_upscale="2")
        v = settings.load(self.conn)
        self.assertEqual((v["render_resolution"], v["default_upscale"]), ("4k", 4))

    def test_resolution_alone_also_sets_the_tier(self):
        self.save(render_resolution="1080p")
        self.assertEqual(settings.load(self.conn)["default_upscale"], 1)

    def test_changing_only_the_tier_moves_the_resolution(self):
        self.save(default_upscale="4")
        v = settings.load(self.conn)
        self.assertEqual((v["render_resolution"], v["default_upscale"]), ("4k", 4))

    def test_a_disagreement_warns_and_the_resolution_wins(self):
        resp = self.save(render_resolution="2k", default_upscale="4")
        self.assertEqual(settings.load(self.conn)["default_upscale"], 2)
        self.assertIn("the resolution wins", unquote_plus(resp.headers["Location"]))

    def test_a_consistent_pair_has_no_warning(self):
        resp = self.save(render_resolution="4k", default_upscale="4")
        self.assertNotIn("wins", unquote_plus(resp.headers["Location"]))

    def test_tier_3_is_stored_as_2(self):
        self.save(default_upscale="3")
        v = settings.load(self.conn)
        self.assertEqual((v["default_upscale"], v["render_resolution"]), (2, "2k"))

    def test_an_unrelated_save_leaves_the_pair_alone(self):
        self.save(render_resolution="4k", default_upscale="4")
        self.save(per_day="2")
        v = settings.load(self.conn)
        self.assertEqual((v["render_resolution"], v["default_upscale"]), ("4k", 4))

    def test_stored_tier_3_reads_as_2(self):
        db.set_setting(self.conn, "default_upscale", "3")
        self.assertEqual(settings.load(self.conn)["default_upscale"], 2)


class ChannelSaveTests(_Base):
    def edit(self, **data):
        form = {"id": str(self.oc), "name": "Ch"}
        form.update(data)
        return self.client.post("/my-channels/edit", data=form)

    def test_resolution_sets_the_channels_tier(self):
        self.edit(render_resolution="4k", default_upscale="2")
        self.assertEqual((self.row()["render_resolution"],
                          self.row()["default_upscale"]), ("4k", 4))

    def test_disagreement_warns(self):
        resp = self.edit(render_resolution="1080p", default_upscale="4")
        self.assertEqual(self.row()["default_upscale"], 1)
        self.assertIn("the resolution wins", unquote_plus(resp.headers["Location"]))

    def test_tier_alone_sets_the_resolution(self):
        self.edit(default_upscale="4")
        self.assertEqual(self.row()["render_resolution"], "4k")

    def test_tier_3_becomes_2k(self):
        self.edit(default_upscale="3")
        self.assertEqual((self.row()["default_upscale"],
                          self.row()["render_resolution"]), (2, "2k"))

    def test_both_inherit_clears_both(self):
        self.edit(render_resolution="4k")
        self.edit(render_resolution="", default_upscale="")
        self.assertIsNone(self.row()["render_resolution"])
        self.assertIsNone(self.row()["default_upscale"])

    def test_flow_native_means_tier_0(self):
        self.edit(render_resolution="flow-native", default_upscale="")
        self.assertEqual(self.row()["default_upscale"], 0)

    def test_unrelated_edit_does_not_touch_the_pair(self):
        self.edit(render_resolution="4k")
        self.edit(per_day="3")
        self.assertEqual((self.row()["render_resolution"],
                          self.row()["default_upscale"]), ("4k", 4))


class EffectiveValueTests(_Base):
    def test_the_resolution_decides_the_pipelines_tier(self):
        db.update_own_channel(self.conn, self.oc, render_resolution="4k")
        eff = self.eff()
        self.assertEqual((eff["render_resolution"], eff["upscale"]), ("4k", 4))
        self.assertEqual(eff["upscale_warning"], "")   # tier just inherited

    def test_legacy_mismatch_is_healed_and_warned(self):
        db.update_own_channel(self.conn, self.oc, render_resolution="2k",
                              default_upscale=4)
        eff = self.eff()
        self.assertEqual(eff["upscale"], 2)
        self.assertIn("the resolution wins", eff["upscale_warning"])

    def test_legacy_tier_3_with_2k_is_consistent(self):
        db.update_own_channel(self.conn, self.oc, render_resolution="2k",
                              default_upscale=3)
        eff = self.eff()
        self.assertEqual((eff["upscale"], eff["upscale_warning"]), (2, ""))

    def test_global_pair_mismatch_warns(self):
        db.set_setting(self.conn, "render_resolution", "4k")   # tier stays 2
        eff = self.eff()
        self.assertEqual(eff["upscale"], 4)
        self.assertIn("4k", eff["upscale_warning"])

    def test_flow_native_never_upscales_in_the_engine(self):
        db.update_own_channel(self.conn, self.oc, render_resolution="flow-native")
        self.assertEqual(autorun._upscale_for(self.eff()), 0)

    def test_manual_upscale_uses_the_resolution_tier(self):
        db.update_own_channel(self.conn, self.oc, render_resolution="4k")
        self.assertEqual(autorun.manual_upscale_tier(self.eff()), "4k")


class ImgToVideoTests(_Base):
    def setUp(self):
        super().setUp()
        self.pid = db.create_production(self.conn, "P", "general", None, None)
        db.update_production(self.conn, self.pid, own_channel_id=self.oc)
        self.pdir = studio.prod_dir(self.cfg, self.pid)
        self.pdir.mkdir(parents=True, exist_ok=True)

    def opts(self):
        return json.loads((self.pdir / "imgtovideo.json").read_text("utf-8"))

    def test_the_output_size_follows_the_resolution(self):
        for res, size in (("1080p", (1920, 1080)), ("2k", (2560, 1440)),
                          ("4k", (3840, 2160)), ("flow-native", (1376, 768))):
            db.update_own_channel(self.conn, self.oc, render_resolution=res)
            studio.prepare_project_folder(self.cfg, self.pid)
            out = self.opts()["output"]
            self.assertEqual((out["width"], out["height"]), size, res)

    def test_changing_the_resolution_rewrites_an_existing_file_keeping_keys(self):
        (self.pdir / "imgtovideo.json").write_text(json.dumps(
            {"schema_version": 1, "sound": {"default_pop": "pop_snap"}}), "utf-8")
        db.update_own_channel(self.conn, self.oc, render_resolution="4k")
        studio.prepare_project_folder(self.cfg, self.pid)
        data = self.opts()
        self.assertEqual(data["output"]["width"], 3840)
        self.assertEqual(data["sound"]["default_pop"], "pop_snap")

    def test_small_images_for_a_big_output_warn(self):
        (self.pdir / "images").mkdir(exist_ok=True)
        _png(self.pdir / "images" / "S01_01_ST.png", 1376, 768)
        db.update_own_channel(self.conn, self.oc, render_resolution="4k")
        studio.prepare_project_folder(self.cfg, self.pid)
        warning = studio.image_size_warning(self.cfg, self.pid)
        self.assertIn("1376", warning)
        self.assertIn("3840", warning)

    def test_matching_images_do_not_warn(self):
        (self.pdir / "images").mkdir(exist_ok=True)
        _png(self.pdir / "images" / "S01_01_ST.png", 2560, 1440)
        studio.prepare_project_folder(self.cfg, self.pid)
        self.assertEqual(studio.image_size_warning(self.cfg, self.pid), "")

    def test_square_and_wide_images_are_judged_by_their_long_side(self):
        (self.pdir / "images").mkdir(exist_ok=True)
        _png(self.pdir / "images" / "S01_01_PU.png", 2560, 2560)
        _png(self.pdir / "images" / "S01_02_PL.png", 3360, 1440)
        studio.prepare_project_folder(self.cfg, self.pid)
        self.assertEqual(studio.image_size_warning(self.cfg, self.pid), "")

    def test_no_images_no_warning_and_non_png_ignored(self):
        studio.prepare_project_folder(self.cfg, self.pid)
        self.assertEqual(studio.image_size_warning(self.cfg, self.pid), "")
        (self.pdir / "images").mkdir(exist_ok=True)
        (self.pdir / "images" / "x.png").write_bytes(b"not a png")
        self.assertEqual(studio.image_size_warning(self.cfg, self.pid), "")


class PageTests(_Base):
    def test_channel_dropdown_has_no_tier_3(self):
        html = self.client.get("/my-channels").get_data(as_text=True)
        self.assertIn("2 - 2K 2560x1440", html)
        self.assertIn("4 - 4K 3840x2160", html)
        self.assertNotIn('<option value="3"', html.split('name="default_upscale"')[1][:900])

    def test_a_legacy_3_shows_as_2_selected(self):
        db.update_own_channel(self.conn, self.oc, default_upscale=3)
        html = self.client.get("/my-channels").get_data(as_text=True)
        seg = html.split('name="default_upscale"')[1][:900]
        self.assertRegex(seg, r'<option value="2" selected>')

    def test_settings_dropdown_has_no_tier_3_and_is_synced(self):
        html = self.client.get("/settings").get_data(as_text=True)
        seg = html.split('id="f-default_upscale"')[1][:700]
        self.assertNotIn('value="3"', seg)
        self.assertIn("1 - HD 1920x1080", seg)
        self.assertIn("var R2U", html)

    def test_the_sync_script_is_on_both_pages(self):
        for url in ("/settings", "/my-channels"):
            html = self.client.get(url).get_data(as_text=True)
            self.assertIn("'flow-native': '0'", html, url)


class ApplyAllStillWorks(_Base):
    def test_applying_one_applies_its_partner(self):
        self.assertEqual(settings.with_partners(["render_resolution"]),
                         ["render_resolution", "default_upscale"])
        self.assertEqual(settings.with_partners(["default_upscale", "per_day"]),
                         ["default_upscale", "render_resolution", "per_day"])

    def test_settings_apply_of_the_resolution_also_resets_the_tier(self):
        db.update_own_channel(self.conn, self.oc, render_resolution="4k",
                              default_upscale=4)
        self.client.post("/settings/save", data={
            "render_resolution": "2k", "apply_all": "render_resolution"})
        self.assertIsNone(self.row()["default_upscale"])
        self.assertIsNone(self.row()["render_resolution"])

    def test_channel_copy_of_the_tier_also_copies_the_resolution(self):
        other = db.create_own_channel(self.conn, "Other")
        db.update_own_channel(self.conn, other, render_resolution="1080p",
                              default_upscale=1)
        self.client.post("/my-channels/edit", data={
            "id": str(self.oc), "name": "Ch", "default_upscale": "4",
            "apply_all": "default_upscale"})
        row = db.get_own_channel(self.conn, other)
        self.assertEqual((row["render_resolution"], row["default_upscale"]),
                         ("4k", 4))

    def test_apply_all_of_resolution_keeps_pairing(self):
        db.update_own_channel(self.conn, self.oc, render_resolution="4k",
                              default_upscale=4)
        self.client.post("/settings/save", data={
            "render_resolution": "2k", "apply_all": ["render_resolution",
                                                      "default_upscale"]})
        v = settings.load(self.conn)
        self.assertEqual((v["render_resolution"], v["default_upscale"]), ("2k", 2))
        self.assertIsNone(self.row()["render_resolution"])
        self.assertIsNone(self.row()["default_upscale"])


if __name__ == "__main__":
    unittest.main()
