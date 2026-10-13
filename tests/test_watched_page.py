import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


def _ago(days):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()


class WatchedPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = t / "wr.db"
        self.cfg.audio_dir = t / "audio"
        self.cfg.transcripts_dir = t / "tr"
        self.cfg.channels = []
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        db.add_channel(self.conn, "Busy", "UCa", genre="Finance")
        db.add_channel(self.conn, "Sleepy", "UCb", genre="Finance")
        db.add_channel(self.conn, "Typo", "UCc", genre="Japanese Lifestlye")
        db.upsert_videos(self.conn, "UCa", [
            {"video_id": f"a{i}", "title": f"A{i}", "url": "u",
             "published_at": _ago(i + 1), "view_count": 1000}
            for i in range(8)] + [
            {"video_id": "ahit", "title": "Hit", "url": "u",
             "published_at": _ago(2), "view_count": 90000}])
        db.upsert_videos(self.conn, "UCb", [
            {"video_id": "b1", "title": "B1", "url": "u",
             "published_at": _ago(200), "view_count": 10}])
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_stats_count_videos_find_quiet_and_best_outlier(self):
        from whisperradar.webapp import _watched_stats
        st = _watched_stats(self.conn)
        self.assertEqual(st["UCa"]["videos"], 9)
        self.assertFalse(st["UCa"]["quiet"])
        self.assertTrue(st["UCb"]["quiet"])
        self.assertGreaterEqual(st["UCa"]["best"], 5)
        self.assertNotIn("UCc", st)            # no videos: the page copes

    def test_page_shows_columns_chips_and_quiet(self):
        html = self.client.get("/watched").get_data(as_text=True)
        for needle in ("Last upload", "Best outlier", "Quiet (1)",
                       "Finance (2)", "Rename / merge a genre", "Backfill history"):
            self.assertIn(needle, html)

    def test_rename_merges_genres(self):
        r = self.client.post("/watched/genre/rename",
                             data={"old": "Japanese Lifestlye", "new": "Finance"})
        self.assertEqual(r.status_code, 302)
        genres = {c["genre"] for c in db.list_channels(self.conn)}
        self.assertEqual(genres, {"Finance"})

    def test_rename_needs_both_names(self):
        r = self.client.post("/watched/genre/rename", data={"old": "Finance"})
        self.assertIn("error=", r.headers["Location"])


if __name__ == "__main__":
    unittest.main()
