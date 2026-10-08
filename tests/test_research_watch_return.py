"""Research: clicking "Watch" on a discovered channel stays on Research, and
already-watched discovered rows are hidden."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402
from whisperradar import db  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

WID = "UC" + "w" * 22   # matches CHANNEL_ID_RE (UC + 22 chars), no network
NID = "UC" + "n" * 22
OTHER = "UC" + "o" * 22


class WatchReturnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        db.add_channel(self.conn, "Active One", WID, genre="g")
        # discovery (and its results table) only render with an API key present
        (Path(self.tmp.name) / "youtube_api_key.txt").write_text(
            "AIza" + "x" * 30, encoding="utf-8")
        # a persisted discover result: one channel already watched, one new
        (Path(self.tmp.name) / "research_discover.json").write_text(
            json.dumps({"query": "q", "channels": [
                {"channel_id": WID, "name": "Disc Watched", "about": "a",
                 "subscribers": 100, "videos": 5, "views": 500, "sample": []},
                {"channel_id": NID, "name": "Disc New", "about": "b",
                 "subscribers": 200, "videos": 8, "views": 900, "sample": []},
            ]}), encoding="utf-8")
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def test_watch_discovered_returns_to_research_not_watched(self):
        r = self.client.post("/channels/add", data={
            "url": NID, "name": "Disc New", "genre": "g",
            "return": "/research?tab=channels"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].startswith("/research?tab=channels"))
        self.assertIn("msg=", r.headers["Location"])
        # it really got added
        conn = db.connect(self.cfg.db_path)
        try:
            self.assertIsNotNone(db.get_channel(conn, NID))
        finally:
            conn.close()

    def test_add_without_return_still_goes_to_watched(self):
        r = self.client.post("/channels/add", data={
            "url": OTHER, "name": "Other", "genre": "g"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].startswith("/watched"))

    def test_research_hides_already_watched_discovered_rows(self):
        html = self.client.get("/research?tab=channels").get_data(as_text=True)
        self.assertIn("Disc New", html)        # not watched yet -> shown
        self.assertNotIn("Disc Watched", html)  # already watched -> hidden


if __name__ == "__main__":
    unittest.main()
