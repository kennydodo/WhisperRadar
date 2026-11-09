"""Explainer presets: global < preset < channel layering, the channel dropdown, the presets page."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402,F401

from whisperradar import db, explainer_presets as ep  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.video import settings_spec as ss  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class PresetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.oc = db.create_own_channel(conn, "Chan")
        conn.close()
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def conn(self):
        c = db.connect(self.cfg.db_path)
        db.init_db(c)
        return c

    def eff(self):
        c = self.conn()
        try:
            return ss.effective(c, self.oc)
        finally:
            c.close()

    def post(self, **fields):
        return self.client.post("/my-channels/edit",
                                data={"id": str(self.oc), "explainer_present": "1", **fields})

    def saved(self):
        c = self.conn()
        try:
            return ss.load_channel(c, self.oc)
        finally:
            c.close()

    def test_preset_beats_global_and_channel_beats_preset(self):
        c = self.conn()
        ss.save_global(c, {"explainer.text_size": "normal", "explainer.pen_speed": "fast"})
        c.close()
        self.assertEqual(self.eff().get("explainer.text_size"), "normal")
        self.assertEqual(self.post(explainer_preset="coding_lesson").status_code, 302)
        e = self.eff()
        self.assertEqual(e["explainer.text_size"], "small")
        self.assertEqual(e["explainer.text_weight"], "thin")
        self.assertEqual(e["explainer.pen_speed"], "fast")      # not in the preset: global stays
        self.post(explainer_preset="coding_lesson", explainer_text_size="smaller")
        self.assertEqual(self.eff()["explainer.text_size"], "smaller")   # channel wins
        self.assertEqual(self.eff()["explainer.text_weight"], "thin")

    def test_preset_key_never_reaches_a_production(self):
        self.post(explainer_preset="coding_lesson")
        self.assertNotIn("explainer.preset", self.eff())

    def test_unknown_preset_is_refused(self):
        r = self.post(explainer_preset="nope")
        self.assertIn("error=", r.headers["Location"])
        self.assertNotIn("explainer.preset", self.saved())

    def test_reset_checkbox_drops_own_look_fields(self):
        self.post(explainer_preset="coding_lesson", explainer_text_size="smaller")
        html = self.client.get("/my-channels").get_data(as_text=True)
        self.assertIn("explainer_reset_custom", html)
        self.post(explainer_preset="coding_lesson", explainer_text_size="smaller", explainer_reset_custom="1")
        self.assertEqual(self.saved(), {"explainer.preset": "coding_lesson"})
        self.assertEqual(self.eff()["explainer.text_size"], "small")

    def test_generic_video_options_save_keeps_the_preset(self):
        self.post(explainer_preset="coding_lesson")
        r = self.client.post(f"/video-options?scope={self.oc}", data={})
        self.assertIn(self.saved().get("explainer.preset", "coding_lesson"), ("coding_lesson",))
        self.assertLess(r.status_code, 500)

    def test_channel_page_has_dropdown_and_video_type_label(self):
        html = self.client.get("/my-channels").get_data(as_text=True)
        self.assertIn('name="explainer_preset"', html)
        self.assertIn("Coding lesson", html)
        self.assertIn("video type", html)

    def test_custom_preset_roundtrip_and_validation(self):
        c = self.conn()
        try:
            key = ep.save_custom(c, "My Dark", {"explainer.theme": "chalkboard", "explainer.text_size": "small"})
            self.assertEqual(key, "my_dark")
            self.assertIn(key, ep.all_presets(c))
            with self.assertRaises(ValueError):
                ep.save_custom(c, "", {"explainer.theme": "chalkboard"})
            with self.assertRaises(ValueError):
                ep.save_custom(c, "chalkboard", {"explainer.theme": "chalkboard"})
            with self.assertRaises(ValueError):
                ep.save_custom(c, "Empty", {})
            with self.assertRaises(ValueError):
                ep.save_custom(c, "Bad", {"explainer.text_size": "gigantic"})
            self.assertTrue(ep.delete_custom(c, key))
            self.assertFalse(ep.delete_custom(c, key))
        finally:
            c.close()

    def test_presets_page_save_use_and_delete(self):
        r = self.client.get("/explainer/presets")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Coding lesson", r.get_data(as_text=True))
        r = self.client.post("/explainer/presets/save", data={
            "label": "Tiny", "v::explainer.text_size": "small", "v::explainer.theme": "chalkboard"})
        self.assertIn("msg=", r.headers["Location"])
        self.assertIn("Tiny", self.client.get("/explainer/presets").get_data(as_text=True))
        self.post(explainer_preset="tiny")
        self.assertEqual(self.eff()["explainer.text_size"], "small")
        self.assertIn("Chan", self.client.get("/explainer/presets").get_data(as_text=True))
        r = self.client.post("/explainer/presets/delete", data={"key": "tiny"})
        self.assertIn("msg=", r.headers["Location"])
        self.assertIn("error=", self.client.post("/explainer/presets/save", data={"label": "x"}).headers["Location"])


if __name__ == "__main__":
    unittest.main()
