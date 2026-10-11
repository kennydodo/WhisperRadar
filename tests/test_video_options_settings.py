"""Video options: every ImgToVideo option as a setting (video/settings_spec.py)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402,F401

from whisperradar import db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.video import settings_spec as ss  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.oc = db.create_own_channel(conn, "Chan")
        self.pid = db.create_production(conn, "P", "general", None, None)
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def conn(self):
        c = db.connect(self.cfg.db_path)
        db.init_db(c)
        return c


class Spec(unittest.TestCase):
    def test_covers_all_options(self):
        self.assertGreaterEqual(len(ss.SPEC), 45)
        self.assertNotIn("output.width", ss.BY_PATH)

    def test_parse_blank_inherits_and_errors(self):
        vals, errs = ss.parse_form({"v::motion.easing": "linear",
                                    "v::timing.min_image_seconds": ""})
        self.assertEqual(vals, {"motion.easing": "linear"})
        self.assertEqual(errs, [])
        _, errs = ss.parse_form({"v::motion.easing": "bogus"})
        self.assertTrue(errs)

    def test_validate(self):
        self.assertEqual(ss.validate({"motion.easing": "linear"}), [])
        self.assertTrue(ss.validate({"timing.min_image_seconds": 10}))


class Merge(Base):
    def test_channel_over_global_and_hand_edits_kept(self):
        c = self.conn()
        ss.save_global(c, {"motion.easing": "linear", "output.fps": 24})
        ss.save_channel(c, self.oc, {"output.fps": 30})
        self.assertEqual(ss.effective(c, self.oc),
                         {"motion.easing": "linear", "output.fps": 30})
        c.close()
        pdir = studio.prod_dir(self.cfg, self.pid)
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "imgtovideo.json").write_text(json.dumps(
            {"output": {"fps": 60}}), encoding="utf-8")
        changed = ss.apply_to_project(pdir, {"output.fps": 30,
                                             "motion.easing": "linear"})
        data = json.loads((pdir / "imgtovideo.json").read_text())
        self.assertEqual(data["output"]["fps"], 60)       # hand edit kept
        self.assertEqual(data["motion"]["easing"], "linear")
        self.assertEqual(changed, ["motion.easing"])
        # removing the setting removes only what we wrote
        ss.apply_to_project(pdir, {})
        data = json.loads((pdir / "imgtovideo.json").read_text())
        self.assertNotIn("motion", data)
        self.assertEqual(data["output"]["fps"], 60)

    def test_prepare_project_folder_applies(self):
        c = self.conn()
        ss.save_global(c, {"motion.easing": "linear"})
        c.close()
        pdir = studio.prepare_project_folder(self.cfg, self.pid)
        data = json.loads((pdir / "imgtovideo.json").read_text())
        self.assertEqual(data["motion"]["easing"], "linear")


class Routes(Base):
    def test_page_save_global_and_channel(self):
        client = create_app(self.cfg).test_client()
        self.assertEqual(client.get("/video-options").status_code, 200)
        r = client.post("/video-options/save", data={"v::motion.easing": "linear"})
        self.assertIn("msg=", r.headers["Location"])
        r = client.post("/video-options/save", data={
            "channel": str(self.oc), "v::output.fps": "30"})
        self.assertIn("msg=", r.headers["Location"])
        page = client.get(f"/video-options?channel={self.oc}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'value="30.0"', page.data)
        r = client.post("/video-options/save",
                        data={"v::timing.min_image_seconds": "10"})
        self.assertIn("error=", r.headers["Location"])
        c = self.conn()
        self.assertEqual(ss.load_global(c), {"motion.easing": "linear"})
        c.close()


class Sounds(Base):
    def test_all_sounds_listed_and_playable(self):
        import shutil
        client = create_app(self.cfg).test_client()
        page = client.get("/video-options").data
        for name in (b"whoosh", b"ding", b"swipe", b"pop_marimba"):
            self.assertIn(b"playSfx('" + name, page)
        self.assertEqual(client.get("/video-options/sfx/nope.wav").status_code, 404)
        if shutil.which("ffmpeg"):
            r = client.get("/video-options/sfx/whoosh.wav")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.data.startswith(b"RIFF"))


if __name__ == "__main__":
    unittest.main()
