"""Video list paging: totals, sorting, search, page-size and view preservation.

Run: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

CH = "UC" + "p" * 22
N = 60


class PaginationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(tempfile.mkdtemp()) / "wr.db"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.add_channel(conn, name="Chan", channel_id=CH, genre="g")
        for i in range(N):
            db.upsert_video(conn, CH, {
                "video_id": f"v{i:03d}",
                "title": f"Video {i} " + ("cats" if i % 2 == 0 else "dogs"),
                "url": f"https://youtu.be/v{i:03d}",
                "published_at": f"2024-01-01T00:{i:02d}:00+00:00",
                "view_count": i * 1000,
            }, auto=0)
        conn.close()
        self.client = create_app(self.cfg).test_client()

    def _conn(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        return conn

    def test_page_returns_total_and_slice(self):
        conn = self._conn()
        rows, total = db.get_videos_page(conn, channel=CH, limit=25, offset=0)
        self.assertEqual(total, N)
        self.assertEqual(len(rows), 25)
        rows2, total2 = db.get_videos_page(conn, channel=CH, limit=25, offset=25)
        self.assertEqual(total2, N)
        self.assertEqual(len(rows2), 25)
        # newest first (created ascending, so v059 is newest)
        self.assertEqual(rows[0]["video_id"], "v059")
        conn.close()

    def test_page_past_the_end_still_reports_total(self):
        conn = self._conn()
        rows, total = db.get_videos_page(conn, channel=CH, limit=25, offset=9999)
        self.assertEqual(rows, [])
        self.assertEqual(total, N)
        conn.close()

    def test_sort_by_views(self):
        conn = self._conn()
        rows, _ = db.get_videos_page(conn, channel=CH, sort="views",
                                     limit=5, offset=0)
        self.assertEqual([r["video_id"] for r in rows],
                         ["v059", "v058", "v057", "v056", "v055"])
        conn.close()

    def test_search_filters_titles(self):
        conn = self._conn()
        rows, total = db.get_videos_page(conn, channel=CH, q="cats",
                                         limit=200, offset=0)
        self.assertEqual(total, N // 2)
        self.assertTrue(all("cats" in r["title"] for r in rows))
        conn.close()

    def test_malformed_page_does_not_500(self):
        for query in ("/?page=abc", "/?page=-5", "/?per_page=zzz",
                      "/?per_page=7"):
            resp = self.client.get(query)
            self.assertEqual(resp.status_code, 200, query)

    def test_out_of_range_page_clamps(self):
        resp = self.client.get("/?channel=" + CH + "&page=9999")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("page ", resp.get_data(as_text=True))
        self.assertIn("of 2", resp.get_data(as_text=True))  # 60 / 50 -> 2 pages

    def test_per_page_is_remembered_in_a_cookie(self):
        resp = self.client.get("/?per_page=25")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("wr_per_page=25", resp.headers.get("Set-Cookie", ""))
        # the cookie then drives later requests
        resp = self.client.get("/")
        self.assertIn("of 3", resp.get_data(as_text=True))  # 60 / 25 -> 3 pages

    def test_back_preserves_sort_and_page(self):
        resp = self.client.post("/videos/action", data={
            "video_id": "v000", "action": "queue",
            "sort": "views", "page": "3", "channel": CH,
        })
        self.assertIn(resp.status_code, (302, 303))
        location = resp.headers["Location"]
        self.assertIn("sort=views", location)
        self.assertIn("page=3", location)


if __name__ == "__main__":
    unittest.main()
