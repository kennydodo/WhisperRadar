"""Dashboard: a visible "make it" button makes ONE video's transcript in a
slot of its own; the other row buttons sit in a "more" menu."""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from whisperradar import db, webapp  # noqa: E402


class DashboardMakeTests(unittest.TestCase):
    def setUp(self):
        from tests import wr_tmp
        from whisperradar.config import load_config
        self.wr_tmp = wr_tmp
        root = Path(__file__).resolve().parents[1]
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(root / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.add_channel(conn, "Chan", "UCx", genre="g")
        for vid, status in (("v-new", "new"), ("v-done", "transcribed")):
            db.upsert_video(conn, "UCx", {
                "video_id": vid, "title": f"Title {vid}", "url": "u",
                "view_count": 5, "published_at": "2026-01-01T00:00:00Z"})
            db.set_video(conn, vid, status=status)
        conn.close()
        self.gate = threading.Event()
        self.calls = []

        def fake(cfg, video_id):
            self.calls.append(video_id)
            self.gate.wait(5)
        self.patch = mock.patch.object(webapp, "_make_transcript", fake)
        self.patch.start()
        self.client = webapp.create_app(self.cfg).test_client()

    def tearDown(self):
        self.gate.set()
        self.patch.stop()
        time.sleep(0.2)
        self.wr_tmp.cleanup(self.tmp)

    def test_make_it_is_visible_and_the_rest_is_in_the_more_menu(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('action="/videos/make"', html)
        self.assertEqual(html.count('action="/videos/make"'), 1)  # not the transcribed row
        self.assertIn('<details class="more">', html)
        self.assertIn("delete</button>", html)
        self.assertIn('/transcript/v-done', html)

    def test_it_runs_in_its_own_slot_and_only_one_at_a_time(self):
        r = self.client.post("/videos/make", data={"video_id": "v-new"})
        self.assertEqual(r.status_code, 302)
        time.sleep(0.2)
        st = self.client.get("/status").get_json()
        self.assertTrue(st["make_running"])
        self.assertFalse(st["running"])            # the dashboard's own job is free
        self.assertIn("make transcript", st["make_kind"])
        r2 = self.client.post("/videos/make", data={"video_id": "v-new"})
        self.assertIn("already%20being%20made", r2.headers["Location"])
        self.gate.set()
        time.sleep(0.5)
        self.assertFalse(self.client.get("/status").get_json()["make_running"])
        self.assertEqual(self.calls, ["v-new"])

    def test_unknown_and_already_transcribed(self):
        r = self.client.post("/videos/make", data={"video_id": "nope"})
        self.assertIn("Unknown%20video", r.headers["Location"])
        r = self.client.post("/videos/make", data={"video_id": "v-done"})
        self.assertIn("Already%20has%20a%20transcript", r.headers["Location"])
        self.assertEqual(self.calls, [])


class MakeTranscriptFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from whisperradar.config import load_config
        root = Path(__file__).resolve().parents[1]
        self.cfg = load_config(root / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.add_channel(conn, "Chan", "UCx", genre="g")
        db.upsert_video(conn, "UCx", {"video_id": "v1", "title": "T", "url": "u",
                                      "view_count": 1,
                                      "published_at": "2026-01-01T00:00:00Z"})
        conn.close()

    def tearDown(self):
        from tests import wr_tmp
        wr_tmp.cleanup(self.tmp)

    def _status(self):
        c = db.connect(self.cfg.db_path)
        try:
            return db.get_video(c, "v1")["status"]
        finally:
            c.close()

    def test_downloads_then_transcribes(self):
        order = []

        def dl(cfg, conn, video_ids=None):
            order.append("download")
            db.set_video(conn, video_ids[0], status="downloaded",
                         audio_path="a.mp3")

        def tr(cfg, conn, video_ids=None):
            order.append("transcribe")
            db.set_video(conn, video_ids[0], status="transcribed")
        with mock.patch.object(webapp.pipeline, "process_downloads", dl), \
                mock.patch.object(webapp.pipeline, "process_transcripts", tr):
            webapp._make_transcript(self.cfg, "v1")
        self.assertEqual(order, ["download", "transcribe"])
        self.assertEqual(self._status(), "transcribed")

    def test_a_failed_step_is_reported(self):
        def dl(cfg, conn, video_ids=None):
            db.set_video(conn, video_ids[0], status="error", error="boom")
        with mock.patch.object(webapp.pipeline, "process_downloads", dl), \
                mock.patch.object(webapp.pipeline, "process_transcripts",
                                  side_effect=AssertionError("not reached")):
            with self.assertRaises(RuntimeError) as cm:
                webapp._make_transcript(self.cfg, "v1")
        self.assertIn("boom", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
