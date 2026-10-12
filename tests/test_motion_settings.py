"""Animated opening: per-channel settings, defaults, window and clip fitting."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db, motion, settings  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class MotionPureTests(unittest.TestCase):
    def test_clamp_and_defaults(self):
        self.assertEqual(motion.clamp_seconds(5), 30)
        self.assertEqual(motion.clamp_seconds(999), 180)
        self.assertEqual(motion.clamp_seconds("90"), 90)
        self.assertEqual(motion.clamp_seconds("abc"), 60)
        eff = motion.effective(None)
        self.assertEqual(eff, {"animate_enabled": False, "animate_seconds": 60,
                               "animate_model": "Veo 3.1 - Lite"})

    def test_window_ends_on_a_shot_boundary(self):
        shots = [{"start": i * 4.0, "end": (i + 1) * 4.0} for i in range(30)]
        win = motion.window_shots(shots, 30)
        self.assertEqual(win[-1]["start"], 28.0)       # starts before 30
        self.assertEqual(win[-1]["end"], 32.0)         # ...kept whole
        self.assertEqual(len(win), 8)

    def test_fit_trim_stretch_hold(self):
        self.assertEqual(motion.fit(4), {"play": 4.0, "speed": 1.0, "hold": 0.0})
        self.assertEqual(motion.fit(8), {"play": 8.0, "speed": 1.0, "hold": 0.0})
        nine = motion.fit(9)
        self.assertEqual((nine["play"], nine["hold"]), (8.0, 0.0))
        self.assertAlmostEqual(nine["speed"], 8 / 9, places=3)
        ten = motion.fit(10)
        self.assertAlmostEqual(ten["speed"], 0.8, places=3)
        self.assertEqual(ten["hold"], 0.0)
        long_ = motion.fit(14)
        self.assertEqual(long_["speed"], 0.8)
        self.assertAlmostEqual(long_["hold"], 4.0, places=3)


class MotionChannelTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(tempfile.mkdtemp()) / "wr.db"
        self.cfg.studio_dir = Path(self.cfg.db_path).parent / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        self.cid = db.create_own_channel(self.conn, "Anim")
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()

    def _eff(self):
        prod = None
        row = db.get_own_channel(self.conn, self.cid)
        return motion.effective(row)

    def test_off_by_default(self):
        self.assertFalse(self._eff()["animate_enabled"])

    def test_edit_form_saves_and_clamps(self):
        r = self.client.post("/my-channels/edit", data={
            "id": self.cid, "animate_enabled": "1", "animate_seconds": "400",
            "animate_model": "Veo 3.1 - Fast"})
        self.assertEqual(r.status_code, 302)
        eff = self._eff()
        self.assertTrue(eff["animate_enabled"])
        self.assertEqual(eff["animate_seconds"], 180)
        self.assertEqual(eff["animate_model"], "Veo 3.1 - Fast")
        self.client.post("/my-channels/edit", data={
            "id": self.cid, "animate_enabled": "0", "animate_model": "bogus"})
        eff = self._eff()
        self.assertFalse(eff["animate_enabled"])
        self.assertEqual(eff["animate_model"], "Veo 3.1 - Lite")

    def test_page_shows_the_controls(self):
        html = self.client.get("/my-channels").get_data(as_text=True)
        self.assertIn('name="animate_enabled"', html)
        self.assertIn('name="animate_seconds"', html)
        self.assertIn("Veo 3.1 - Fast", html)


if __name__ == "__main__":
    unittest.main()
