"""Picture frame (16:9 / 9:16): channel setting, production stamp, lock, Flow, render size."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import briefs, db, frames, settings, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402


class FrameBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.cfg.flowbatch_project_url = None
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def make(self, frame=None, **chan):
        ch = db.create_own_channel(self.conn, "Ch", frame=frame, **chan)
        pid = db.create_production(self.conn, "P", "general", None, None)
        db.update_production(self.conn, pid, own_channel_id=ch)
        return ch, pid


class FrameSettingTests(FrameBase):
    def test_default_is_landscape(self):
        ch, pid = self.make()
        prod = db.get_production(self.conn, pid)
        self.assertEqual(frames.effective(prod, db.get_own_channel(self.conn, ch)), "16:9")
        self.assertEqual(settings.for_production(self.conn, prod)["frame"], "16:9")

    def test_assigning_a_vertical_channel_stamps_the_production(self):
        ch, pid = self.make(frame="9:16")
        frames.stamp_from_channel(self.conn, pid, db.get_own_channel(self.conn, ch))
        prod = db.get_production(self.conn, pid)
        self.assertEqual(prod["frame"], "9:16")
        self.assertEqual(settings.for_production(self.conn, prod)["frame"], "9:16")

    def test_the_production_keeps_its_frame_when_the_channel_changes_later(self):
        ch, pid = self.make(frame="9:16")
        frames.stamp_from_channel(self.conn, pid, db.get_own_channel(self.conn, ch))
        db.update_own_channel(self.conn, ch, frame="16:9")
        prod = db.get_production(self.conn, pid)
        self.assertEqual(settings.for_production(self.conn, prod)["frame"], "9:16")

    def test_locked_once_references_start(self):
        ch, pid = self.make(frame="9:16")
        db.update_production(self.conn, pid, stage="refs", frame="16:9")
        frames.stamp_from_channel(self.conn, pid, db.get_own_channel(self.conn, ch))
        self.assertEqual(db.get_production(self.conn, pid)["frame"], "16:9")
        self.assertTrue(frames.is_locked(db.get_production(self.conn, pid)))

    def test_open_while_still_in_the_early_stages(self):
        ch, pid = self.make(frame="9:16")
        for stage in ("style", "script", "audio", "srt", "shots"):
            db.update_production(self.conn, pid, stage=stage)
            self.assertFalse(frames.is_locked(db.get_production(self.conn, pid)), stage)

    def test_junk_means_landscape(self):
        for v in (None, "", "4:3", "x"):
            self.assertEqual(frames.normalize(v), "16:9")
        self.assertEqual(frames.normalize("9:16"), "9:16")


class VerticalOutputTests(FrameBase):
    def _vertical(self, **chan):
        ch, pid = self.make(frame="9:16", **chan)
        frames.stamp_from_channel(self.conn, pid, db.get_own_channel(self.conn, ch))
        self.conn.commit()
        return pid

    def test_render_size_and_preview(self):
        pid = self._vertical(render_resolution="2k")
        studio.apply_render_resolution(self.cfg, pid)
        o = json.loads((studio.prod_dir(self.cfg, pid) / "imgtovideo.json").read_text("utf-8"))
        self.assertEqual((o["output"]["width"], o["output"]["height"]), (1080, 1920))
        self.assertEqual((o["render"]["preview_width"], o["render"]["preview_height"]), (360, 640))

    def test_flow_native_is_768_by_1376(self):
        pid = self._vertical(render_resolution="flow-native")
        studio.apply_render_resolution(self.cfg, pid)
        o = json.loads((studio.prod_dir(self.cfg, pid) / "imgtovideo.json").read_text("utf-8"))
        self.assertEqual((o["output"]["width"], o["output"]["height"]), (768, 1376))

    def test_landscape_is_untouched(self):
        ch, pid = self.make(render_resolution="2k")
        self.conn.commit()
        studio.apply_render_resolution(self.cfg, pid)
        o = json.loads((studio.prod_dir(self.cfg, pid) / "imgtovideo.json").read_text("utf-8"))
        self.assertEqual((o["output"]["width"], o["output"]["height"]), (2560, 1440))
        self.assertNotIn("render", o)

    def test_a_preview_size_set_on_purpose_is_kept(self):
        pid = self._vertical()
        pdir = studio.prod_dir(self.cfg, pid)
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "imgtovideo.json").write_text(json.dumps(
            {"render": {"preview_width": 540, "preview_height": 960}}), "utf-8")
        studio.apply_render_resolution(self.cfg, pid)
        o = json.loads((pdir / "imgtovideo.json").read_text("utf-8"))
        self.assertEqual((o["render"]["preview_width"], o["render"]["preview_height"]), (540, 960))

    def test_flow_job_asks_for_9_16_and_no_per_shot_override(self):
        pid = self._vertical()
        pdir = studio.prod_dir(self.cfg, pid)
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "shotlist.json").write_text(json.dumps({"style": "s", "images": [
            {"file": f"A{i}_{m}.png", "prompt": "p"}
            for i, m in enumerate(("ZI", "PU", "PV"), 1)]}), "utf-8")
        job_path, _ = studio.prepare_flowbatch_job(self.cfg, pdir, pid)
        job = json.loads(job_path.read_text("utf-8"))
        self.assertEqual(job["defaults"]["aspectRatio"], "9:16")
        self.assertTrue(all("aspectRatio" not in i for i in job["images"]))


class VerticalMotionTests(unittest.TestCase):
    def test_only_push_pull_and_still(self):
        prof = briefs.resolve_profile(None, vertical=True)
        self.assertEqual(set(prof.allowed), {"ST", "ZI", "ZO"})

    def test_a_preset_that_allows_tilts_is_narrowed(self):
        key = "flow_safe"
        self.assertIn("PU", briefs.get_profile(key).allowed)
        prof = briefs.resolve_profile(key, vertical=True)
        self.assertNotIn("PU", prof.allowed)

    def test_landscape_is_unchanged(self):
        self.assertEqual(briefs.resolve_profile(None).allowed,
                         briefs.resolve_profile(None, vertical=False).allowed)


class FlowVerticalMasterTests(unittest.TestCase):
    def test_768x1376_is_cropped_not_padded_for_9_16(self):
        from whisperradar.video import motion_engine
        r = motion_engine._canvas_rect(768, 1376, 9 / 16)
        self.assertEqual(r.width, 768)          # no black sliver either side
        self.assertLessEqual(r.height, 1376)

    def test_a_clearly_narrower_image_is_still_padded(self):
        from whisperradar.video import motion_engine
        r = motion_engine._canvas_rect(500, 1000, 9 / 16)
        self.assertGreater(r.width, 500)


if __name__ == "__main__":
    unittest.main()
