"""The style character counters are on the pages that hold a style.

Run: python -m unittest tests.test_style_counter
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class StyleCounterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = load_config(ROOT / "config.yaml")
        cfg.db_path = Path(self.tmp.name) / "wr.db"
        cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        self.chan = db.create_own_channel(conn, "Chan", style="flat vector " * 40)
        self.pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, self.pid, own_channel_id=self.chan)
        conn.close()
        self.client = create_app(cfg).test_client()

    def test_channel_style_has_a_counter_with_the_flow_limit(self):
        html = self.client.get("/my-channels").get_data(as_text=True)
        self.assertIn("style_counter.js", html)
        self.assertIn(f'data-limit="{studio.FLOWBATCH_MAX_PROMPT_CHARS}"', html)
        self.assertIn("wrStyleCount(", html)

    def test_studio_page_counts_the_visual_style_and_the_shotlist(self):
        html = self.client.get(f"/studio/{self.pid}?stage=shots").get_data(
            as_text=True)
        self.assertIn("style_counter.js", html)
        self.assertIn(f"var LIMIT = {studio.FLOWBATCH_MAX_PROMPT_CHARS};", html)
        self.assertIn('id="shotcount"', html)
        self.assertIn('id="vstylecount"', html)

    def test_script_file_is_served(self):
        r = self.client.get("/static/style_counter.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"left for each image prompt", r.data)


if __name__ == "__main__":
    unittest.main()
