"""The swipe file: saved winners, their storage and the writer's view."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import db, thumbnails  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class DbSwipeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "wr.db")
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def test_add_list_delete(self):
        sid = db.add_swipe(self.conn, kind="thumbnail", title="The Box Rule",
                           note="red ellipse on the clutter",
                           video_id="V1", channel_name="Home One",
                           genre="home", thumb_url="http://x/1.jpg")
        rows = db.list_swipes(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "The Box Rule")
        self.assertEqual(db.list_swipes(self.conn, kind="title"), [])
        self.assertEqual(len(db.list_swipes(self.conn, kind="thumbnail")), 1)
        db.delete_swipe(self.conn, sid)
        self.assertEqual(db.list_swipes(self.conn), [])

    def test_saving_the_same_video_twice_is_one_entry(self):
        a = db.add_swipe(self.conn, kind="thumbnail", title="T",
                        video_id="V1")
        b = db.add_swipe(self.conn, kind="thumbnail", title="T again",
                        video_id="V1")
        self.assertEqual(a, b)
        self.assertEqual(len(db.list_swipes(self.conn)), 1)
        c = db.add_swipe(self.conn, kind="title", title="T", video_id="V1")
        self.assertNotEqual(c, a)          # other kind may coexist

    def test_manual_entries_without_a_video_are_all_kept(self):
        db.add_swipe(self.conn, kind="hook", title="one")
        db.add_swipe(self.conn, kind="hook", title="two")
        self.assertEqual(len(db.list_swipes(self.conn)), 2)


BASE_CTX = {"channel": "Chan", "genre": "home", "channel_about": "about",
            "kit_title": "Kit title", "keyword": "kw", "script": "text",
            "bible": "", "ref_text": "(none)", "refs": [("t", 5.0)]}


class WriterPromptTests(unittest.TestCase):
    def test_swipe_block_appears_only_when_there_are_swipes(self):
        with_swipes = dict(BASE_CTX, swipes=[
            {"kind": "thumbnail", "title": "The Box Rule",
             "note": "ellipse on the clutter", "channel_name": "Home One"}])
        text = thumbnails.writer_prompt(with_swipes)
        self.assertIn("SWIPE FILE", text)
        self.assertIn("The Box Rule", text)
        self.assertIn("ellipse on the clutter", text)
        self.assertIn("do not copy", text)
        without = thumbnails.writer_prompt(dict(BASE_CTX, swipes=[]))
        self.assertNotIn("SWIPE FILE", without)

    def test_missing_swipes_key_is_fine(self):
        self.assertNotIn("SWIPE FILE", thumbnails.writer_prompt(BASE_CTX))


class SwipePageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        db.add_channel(self.conn, "Home One", "UCH", genre="home")
        db.upsert_videos(self.conn, "UCH", [
            {"video_id": f"h{i}", "title": f"japanese home habit {i}",
             "url": "u", "view_count": 20000 if i == 0 else 1000}
            for i in range(10)])
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def test_save_button_posts_and_the_page_shows_it(self):
        r = self.client.post("/swipes", data={
            "kind": "thumbnail", "title": "japanese home habit 0",
            "video_id": "h0", "channel_name": "Home One", "genre": "home",
            "thumb_url": "https://i.ytimg.com/vi/h0/mqdefault.jpg",
            "back": "/research?tab=outliers"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("tab=outliers", r.headers["Location"])
        html = self.client.get("/swipes").get_data(as_text=True)
        self.assertIn("japanese home habit 0", html)
        self.assertIn("mqdefault.jpg", html)
        self.assertIn("Swipe file", self.client.get(
            "/research").get_data(as_text=True))

    def test_bad_kind_falls_back_and_manual_add_works(self):
        self.client.post("/swipes", data={"kind": "sparkles",
                                          "title": "a hook", "note": "n"})
        rows = db.list_swipes(self.conn)
        self.assertEqual(rows[0]["kind"], "thumbnail")

    def test_delete_route_removes_it(self):
        sid = db.add_swipe(self.conn, kind="title", title="keep",
                           video_id="")
        r = self.client.post(f"/swipes/{sid}/delete")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(db.list_swipes(self.conn), [])

    def test_outlier_rows_carry_a_save_form(self):
        html = self.client.get("/research").get_data(as_text=True)
        self.assertIn('action="/swipes"', html)
        self.assertIn('name="thumb_url"', html)


if __name__ == "__main__":
    unittest.main()
