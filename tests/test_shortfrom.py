"""A new Short (9:16 production) from a window of an existing video."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import db, shortfrom, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402

CUES = [("00:00:00,000", "00:00:05,000", "First part of the story."),
        ("00:00:05,000", "00:00:10,000", "The middle holds the twist."),
        ("00:00:10,000", "00:00:20,000", "And the ending lands here.")]
SRT = "".join(f"{i}\n{a} --> {b}\n{t}\n\n" for i, (a, b, t) in enumerate(CUES, 1))


class ShortFromTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.root = Path(self.tmp.name) / "productions"
        self.root.mkdir()
        db.set_setting(conn, "productions_root", str(self.root))
        self.ch = db.create_own_channel(conn, "Ch")
        self.src = db.create_production(conn, "Big video", "general", None, None)
        db.update_production(conn, self.src, own_channel_id=self.ch, stage="review")
        conn.commit()
        conn.close()
        d = studio.prod_dir(self.cfg, self.src)
        d.mkdir(parents=True, exist_ok=True)
        (d / "subtitles.srt").write_text(SRT, encoding="utf-8")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=300:duration=20", str(d / "audio.mp3")],
                       check=True)

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def prod(self, pid):
        conn = db.connect(self.cfg.db_path)
        try:
            return db.get_production(conn, pid)
        finally:
            conn.close()

    def test_makes_a_vertical_production_at_the_shots_stage(self):
        pid = shortfrom.create(self.cfg, self.src, "0:05", "0:20")
        p = self.prod(pid)
        self.assertEqual((p["frame"], p["stage"], p["own_channel_id"]),
                         ("9:16", "shots", self.ch))
        d = studio.prod_dir(self.cfg, pid)
        self.assertTrue((d / "audio.mp3").exists())
        self.assertIn("twist", (d / "script.md").read_text(encoding="utf-8"))
        self.assertNotIn("First part", (d / "script.md").read_text(encoding="utf-8"))
        srt = (d / "subtitles.srt").read_text(encoding="utf-8")
        self.assertIn("00:00:00,000 --> 00:00:05,000", srt)   # shifted to start at 0
        self.assertIn('"from_production"',
                      (d / "short_source.json").read_text(encoding="utf-8"))

    def test_the_old_production_is_untouched(self):
        shortfrom.create(self.cfg, self.src, "0:05", "0:20")
        p = self.prod(self.src)
        self.assertEqual((p["stage"], p["frame"]), ("review", None))

    def test_window_rules(self):
        for a, b in (("0:05", "0:08"), ("0:00", "5:00"), ("", "0:20"), ("x", "9")):
            with self.assertRaises(shortfrom.ShortError, msg=(a, b)):
                shortfrom.create(self.cfg, self.src, a, b)
        with self.assertRaises(shortfrom.ShortError):
            shortfrom.create(self.cfg, self.src, "0:30", "0:50")   # after the end

    def test_needs_audio_and_subtitles(self):
        conn = db.connect(self.cfg.db_path)
        other = db.create_production(conn, "Empty", "general", None, None)
        conn.close()
        with self.assertRaises(shortfrom.ShortError):
            shortfrom.create(self.cfg, other, "0:00", "0:20")

    def test_no_productions_location_is_a_clear_error(self):
        conn = db.connect(self.cfg.db_path)
        db.set_setting(conn, "productions_root", "")
        conn.close()
        with self.assertRaises(shortfrom.ShortError) as cm:
            shortfrom.create(self.cfg, self.src, "0:05", "0:20")
        self.assertIn("productions location", str(cm.exception))

    def test_time_parsing(self):
        self.assertEqual(shortfrom.parse_time("83"), 83)
        self.assertEqual(shortfrom.parse_time("1:23"), 83)
        self.assertEqual(shortfrom.parse_time("0:01:23.5"), 83.5)


class ShortFromRouteTests(ShortFromTests):
    def test_page_and_post(self):
        from whisperradar.webapp import create_app
        client = create_app(self.cfg).test_client()
        page = client.get(f"/studio/{self.src}/shorts").get_data(as_text=True)
        self.assertIn("New Short production from this video", page)
        r = client.post(f"/studio/{self.src}/shorts/new",
                        data={"start": "0:05", "end": "0:20"})
        self.assertEqual(r.status_code, 302)
        self.assertRegex(r.headers["Location"], r"^/studio/\d+\?msg=")
        bad = client.post(f"/studio/{self.src}/shorts/new",
                          data={"start": "0:05", "end": "0:07"})
        self.assertIn("error=", bad.headers["Location"])


if __name__ == "__main__":
    unittest.main()
