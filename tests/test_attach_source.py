"""Studio: the source video is attached on create and attachable later."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db, source_prep  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class AttachSourceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        tmp = Path(tempfile.mkdtemp())
        self.cfg.db_path = tmp / "wr.db"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.add_channel(conn, "Src", "UCsrc")
        db.upsert_video(conn, "UCsrc", {
            "video_id": "abcdefghijk", "title": "Dogs Do This",
            "url": "https://youtu.be/abcdefghijk"})
        conn.execute("UPDATE videos SET status='transcribed'"
                     " WHERE video_id='abcdefghijk'")
        conn.commit()
        self.pid = db.create_production(
            conn, "Other", "general", None, str(tmp / "p"))
        conn.close()
        self.wd = str(tmp / "w")
        app = create_app(self.cfg)
        app.testing = True
        self.client = app.test_client()

    def _src(self, pid):
        conn = db.connect(self.cfg.db_path)
        try:
            return db.get_production(conn, pid)["source_video_id"]
        finally:
            conn.close()

    def test_attach_later_by_title_and_link(self):
        with mock.patch.object(source_prep, "start_background"):
            self.client.post(f"/studio/{self.pid}/source",
                             data={"source": "dogs do this"})
        self.assertEqual(self._src(self.pid), "abcdefghijk")
        conn = db.connect(self.cfg.db_path)
        db.update_production(conn, self.pid, source_video_id=None)
        conn.close()
        with mock.patch.object(source_prep, "start_background"):
            self.client.post(f"/studio/{self.pid}/source", data={
                "source": "https://www.youtube.com/watch?v=abcdefghijk"})
        self.assertEqual(self._src(self.pid), "abcdefghijk")

    def test_unknown_source_changes_nothing(self):
        self.client.post(f"/studio/{self.pid}/source", data={"source": "nope"})
        self.assertIsNone(self._src(self.pid))

    def test_create_with_exact_title_attaches(self):
        with mock.patch.object(source_prep, "start_background"):
            r = self.client.post("/studio/new", data={"title": "Dogs Do This",
                           "work_dir": self.wd})
        loc = r.headers["Location"]
        self.assertIn("/studio/", loc, loc)
        pid = int(loc.split("/studio/")[1].split("?")[0])
        self.assertEqual(self._src(pid), "abcdefghijk")


if __name__ == "__main__":
    unittest.main()
