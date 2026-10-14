"""Research page: a video that already has a production (any channel, any
state) shows a badge, so the same video is not made twice."""
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from whisperradar import db  # noqa: E402


class SourceUsesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _own(self, name):
        cur = self.conn.execute(
            "INSERT INTO own_channels (name, genre, active) VALUES (?,?,1)",
            (name, "general"))
        return cur.lastrowid

    def _prod(self, vid, own, status):
        cur = self.conn.execute(
            "INSERT INTO productions (title, genre, source_video_id,"
            " own_channel_id, status) VALUES ('t','general',?,?,?)",
            (vid, own, status))
        return cur.lastrowid

    def test_any_channel_any_state(self):
        a, b = self._own("Alpha"), self._own("Beta")
        p1 = self._prod("v1", a, "active")
        self._prod("v1", b, "published")
        self._prod("v2", a, "failed")
        self.conn.commit()
        out = db.source_uses(self.conn, ["v1", "v2", "v3"])
        self.assertEqual([u["label"] for u in out["v1"]],
                         ["in production", "published"])
        self.assertEqual(out["v1"][0]["id"], p1)
        self.assertEqual(out["v1"][0]["channel"], "Alpha")
        self.assertEqual(out["v2"][0]["label"], "failed")
        self.assertNotIn("v3", out)

    def test_empty_and_unknown(self):
        self.assertEqual(db.source_uses(self.conn, []), {})
        self.assertEqual(db.source_uses(self.conn, [None, ""]), {})


if __name__ == "__main__":
    unittest.main()


class ResearchPageTests(unittest.TestCase):
    def setUp(self):
        from tests import wr_tmp
        from whisperradar.config import load_config
        from whisperradar.webapp import create_app
        self.wr_tmp = wr_tmp
        root = Path(__file__).resolve().parents[1]
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(root / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        db.add_channel(self.conn, "Watched X", "UCx", genre="general")
        for i in range(8):
            db.upsert_video(self.conn, "UCx", {
                "video_id": f"v{i}", "title": f"Plain video {i}",
                "url": f"https://youtu.be/v{i}", "view_count": 1000,
                "published_at": "2026-01-01T00:00:00Z"})
        db.upsert_video(self.conn, "UCx", {
            "video_id": "vbig", "title": "Huge outlier",
            "url": "https://youtu.be/vbig", "view_count": 900000,
            "published_at": "2026-01-01T00:00:00Z"})
        self.own = db.create_own_channel(self.conn, "My Channel")
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()
        self.wr_tmp.cleanup(self.tmp)

    def test_badge_shows_for_a_taken_video(self):
        html = self.client.get("/research").get_data(as_text=True)
        self.assertIn("Huge outlier", html)
        self.assertNotRegex(html, r'class="badge taken[^"]*"')
        pid = db.create_production(self.conn, "Huge outlier", "general",
                                   "vbig", None)
        db.update_production(self.conn, pid, own_channel_id=self.own)
        html = self.client.get("/research").get_data(as_text=True)
        m = re.search(r'<a class="badge taken active"[^>]*>(.*?)</a>', html)
        self.assertIsNotNone(m)
        self.assertIn("in production", m.group(1))
        self.assertIn("My Channel", m.group(1))
        self.assertIn(f'href="/studio/{pid}"', html)

    def test_make_this_is_grayed_out_only_for_a_taken_video(self):
        html = self.client.get("/research").get_data(as_text=True)
        self.assertNotIn('disabled class="off"', html)
        pid = db.create_production(self.conn, "Huge outlier", "general",
                                   "vbig", None)
        db.update_production(self.conn, pid, own_channel_id=self.own)
        html = self.client.get("/research").get_data(as_text=True)
        self.assertIn('disabled class="off"', html)
        self.assertIn(f"(#{pid})", html)
        # the plain videos stay available (not outliers, so no button) and
        # the analyze panel for the taken video is disabled too
        html = self.client.get("/research?tab=analyze&v=vbig").get_data(
            as_text=True)
        self.assertIn('disabled class="off"', html)

    def test_other_states_are_shown_too(self):
        pid = db.create_production(self.conn, "Huge outlier", "general",
                                   "vbig", None)
        db.update_production(self.conn, pid, own_channel_id=self.own,
                             status="published")
        html = self.client.get("/research").get_data(as_text=True)
        self.assertRegex(html, r'class="badge taken published"[^>]*>published')
