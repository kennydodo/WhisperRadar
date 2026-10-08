"""The review stage offers a thumbnail option (web chat) for productions that
never got one from the packaging plan at the start."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402
from whisperradar import db, studio, thumbnails  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class ThumbnailAtReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        self.pid = db.create_production(self.conn, "A video", "general",
                                        None, None)
        db.update_production(self.conn, self.pid, stage="review",
                             status="ready")
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def test_review_offers_generate_when_no_concepts(self):
        html = self.client.get(f"/studio/{self.pid}").get_data(as_text=True)
        self.assertIn("Generate thumbnail concepts", html)
        self.assertIn(f"/studio/{self.pid}/thumbnails", html)
        self.assertIn("none yet", html)

    def test_review_links_to_existing_concepts(self):
        pdir = studio.prod_dir(self.cfg, self.pid)
        thumbnails.save_thumbs(pdir, {"concepts": [
            {"id": f"c{i}", "text": "T", "art_prompt": "p"} for i in range(3)]})
        html = self.client.get(f"/studio/{self.pid}").get_data(as_text=True)
        self.assertIn("Open thumbnails", html)
        self.assertIn("3 concept(s)", html)
        self.assertNotIn("Generate thumbnail concepts", html)


if __name__ == "__main__":
    unittest.main()
