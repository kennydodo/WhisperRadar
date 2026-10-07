"""YouTube API layer: key handling, quota guard, parsing, fill/discover."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402
from whisperradar import db, youtube_api as ya  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

KEY = "AIzaSyFAKEKEYFORTESTS0123456789abcdef"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "data" / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(ya.ENV_KEY, None)

    def tearDown(self):
        self.env.stop()
        self.conn.close()
        wr_tmp.cleanup(self.tmp)


class Fake:
    """Records urls, answers from a function."""
    def __init__(self, answer):
        self.answer, self.urls = answer, []

    def __call__(self, url):
        self.urls.append(url)
        return self.answer(url)


class KeyTests(Base):
    def test_no_key_raises_a_friendly_error(self):
        with self.assertRaises(ya.NoKey):
            ya.Client(self.cfg)

    def test_env_wins_over_file_and_file_is_local(self):
        ya.save_key(self.cfg, KEY)
        self.assertEqual(ya.key_source(self.cfg), "file")
        self.assertEqual(ya.key_path(self.cfg).parent, self.cfg.db_path.parent)
        os.environ[ya.ENV_KEY] = "x" * 30
        self.assertEqual(ya.key_source(self.cfg), "env")
        self.assertEqual(ya.get_key(self.cfg), "x" * 30)

    def test_junk_keys_are_refused_and_clear_works(self):
        with self.assertRaises(ya.ApiError):
            ya.save_key(self.cfg, "short key with spaces")
        ya.save_key(self.cfg, KEY)
        ya.clear_key(self.cfg)
        self.assertEqual(ya.key_source(self.cfg), "")

    def test_the_key_never_appears_in_an_error(self):
        ya.save_key(self.cfg, KEY)

        def boom(url):
            raise ya.ApiError("failed for " + url)
        client = ya.Client(self.cfg, http=boom)
        with self.assertRaises(ya.ApiError) as cm:
            client.call("videos", id="a")
        self.assertNotIn(KEY, str(cm.exception))


class QuotaTests(Base):
    def test_calls_spend_units_and_the_guard_stops(self):
        ya.save_key(self.cfg, KEY)
        client = ya.Client(self.cfg, http=Fake(lambda u: {"items": []}))
        client.call("videos", id="a")
        client.call("search", q="x")
        self.assertEqual(ya.quota_used(self.cfg), 101)
        ya._spend(self.cfg, 9300)
        with self.assertRaises(ya.QuotaGuard):
            client.call("search", q="y")
        client.call("videos", id="a")           # a 1-unit call still fits

    def test_a_new_day_resets(self):
        ya._spend(self.cfg, 500)
        with mock.patch.object(ya, "_today", return_value="2099-01-01"):
            self.assertEqual(ya.quota_used(self.cfg), 0)


class ParseTests(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(ya.parse_duration("PT1H2M3S"), 3723)
        self.assertEqual(ya.parse_duration("PT45S"), 45)
        self.assertEqual(ya.parse_duration("P1DT1H"), 90000)
        self.assertIsNone(ya.parse_duration("garbage"))
        self.assertIsNone(ya.parse_duration(None))


def video_item(i, views=1000, days=3):
    return {"id": i, "snippet": {"title": f"T{i}", "channelId": "UC1",
                                 "publishedAt": f"2026-09-{days:02d}T10:00:00Z"},
            "contentDetails": {"duration": "PT10M"},
            "statistics": {"viewCount": str(views)}}


class CallTests(Base):
    def setUp(self):
        super().setUp()
        ya.save_key(self.cfg, KEY)

    def test_video_details_batches_of_50(self):
        def answer(url):
            ids = url.split("id=")[1].split("&")[0].replace("%2C", ",")
            return {"items": [video_item(i) for i in ids.split(",")]}
        fake = Fake(answer)
        got = ya.video_details(ya.Client(self.cfg, http=fake),
                               [f"v{i}" for i in range(120)])
        self.assertEqual(len(got), 120)
        self.assertEqual(len(fake.urls), 3)
        self.assertEqual(got["v5"]["duration"], 600)
        self.assertEqual(got["v5"]["view_count"], 1000)

    def test_fill_missing_dates_videos_and_records_snapshots(self):
        db.add_channel(self.conn, "Chan", "UC1", genre="g")
        db.upsert_videos(self.conn, "UC1", [
            {"video_id": "a", "title": "A", "url": "u", "view_count": 10},
            {"video_id": "b", "title": "B", "url": "u", "view_count": 20,
             "published_at": "2020-01-01T00:00:00+00:00"}])
        self.conn.execute("UPDATE videos SET duration = 99 WHERE video_id='b'")
        self.conn.commit()
        fake = Fake(lambda u: {"items": [video_item("a", 5000, 4)]})
        res = ya.fill_missing(self.cfg, self.conn,
                              ya.Client(self.cfg, http=fake))
        self.assertEqual((res["looked_up"], res["dated"]), (1, 1))
        row = self.conn.execute(
            "SELECT published_at, duration, view_count FROM videos"
            " WHERE video_id='a'").fetchone()
        self.assertEqual(row[0], "2026-09-04T10:00:00Z")
        self.assertEqual((row[1], row[2]), (600, 5000))
        # the already-dated video is untouched and was never asked for
        self.assertNotIn("%2Cb", fake.urls[0])
        self.assertNotIn("id=b", fake.urls[0])

    def test_channel_uploads_uses_the_uploads_playlist(self):
        def answer(url):
            if "playlistItems" in url:
                self.assertIn("UU1", url)
                return {"items": [{"contentDetails": {"videoId": "x"}},
                                  {"contentDetails": {"videoId": "y"}}]}
            return {"items": [video_item("x"), video_item("y")]}
        got = ya.channel_uploads(ya.Client(self.cfg, http=Fake(answer)), "UC1")
        self.assertEqual([v["video_id"] for v in got], ["x", "y"])
        with self.assertRaises(ya.ApiError):
            ya.channel_uploads(ya.Client(self.cfg, http=Fake(answer)), "bad")

    def test_discover_skips_watched_and_ranks_small_big_first(self):
        def answer(url):
            if "/search" in url:
                return {"items": [
                    {"id": {"videoId": "1"}, "snippet": {
                        "channelId": "UCbig", "channelTitle": "Big",
                        "title": "t1"}},
                    {"id": {"videoId": "2"}, "snippet": {
                        "channelId": "UCsmall", "channelTitle": "Small",
                        "title": "t2"}},
                    {"id": {"videoId": "3"}, "snippet": {
                        "channelId": "UCwatched", "channelTitle": "W",
                        "title": "t3"}}]}
            return {"items": [
                {"id": "UCbig", "statistics": {
                    "subscriberCount": "1000000", "videoCount": "300",
                    "viewCount": "100000000"}, "snippet": {}},
                {"id": "UCsmall", "statistics": {
                    "subscriberCount": "2000", "videoCount": "20",
                    "viewCount": "4000000"}, "snippet": {}}]}
        fake = Fake(answer)
        got = ya.discover_channels(ya.Client(self.cfg, http=fake), "cats",
                                   {"UCwatched"})
        self.assertEqual([c["channel_id"] for c in got], ["UCsmall", "UCbig"])
        self.assertEqual(ya.quota_used(self.cfg), 101)
        small = ya.discover_channels(ya.Client(self.cfg, http=Fake(answer)),
                                     "cats", set(), max_subs=10000)
        self.assertEqual([c["channel_id"] for c in small], ["UCsmall"])


class PageTests(Base):
    def test_page_without_and_with_a_key_and_the_key_is_never_shown(self):
        client = create_app(self.cfg).test_client()
        html = client.get("/research?tab=channels").get_data(as_text=True)
        self.assertIn("No key yet", html)
        r = client.post("/research/youtube-key", data={"key": "bad key"})
        self.assertIn("error=", r.headers["Location"])
        r = client.post("/research/youtube-key", data={"key": KEY})
        self.assertIn("msg=", r.headers["Location"])
        html = client.get("/research?tab=channels").get_data(as_text=True)
        self.assertIn("A key is set", html)
        self.assertNotIn(KEY, html)
        self.assertIn("Find new channels", html)
        client.post("/research/youtube-key", data={"remove": "1"})
        self.assertEqual(ya.key_source(self.cfg), "")

    def test_discover_and_fill_need_a_key_and_say_so(self):
        client = create_app(self.cfg).test_client()
        r = client.post("/research/discover", data={"q": "cats"})
        self.assertIn("No%20YouTube%20API%20key", r.headers["Location"])
        r = client.post("/research/fill-dates")
        self.assertIn("error=", r.headers["Location"])

    def test_discover_page_lists_results_with_a_watch_button(self):
        ya.save_key(self.cfg, KEY)

        def answer(url):
            if "/search" in url:
                return {"items": [{"id": {"videoId": "1"}, "snippet": {
                    "channelId": "UCnew", "channelTitle": "Newbie",
                    "title": "wow"}}]}
            return {"items": [{"id": "UCnew", "statistics": {
                "subscriberCount": "500", "videoCount": "9",
                "viewCount": "90000"}, "snippet": {"description": "about"}}]}
        with mock.patch.object(ya, "_default_http", answer):
            client = create_app(self.cfg).test_client()
            r = client.post("/research/discover", data={"q": "cats"})
        self.assertIn("msg=", r.headers["Location"])
        html = client.get("/research?tab=channels").get_data(as_text=True)
        self.assertIn("Newbie", html)
        self.assertIn('name="url" value="UCnew"', html)


if __name__ == "__main__":
    unittest.main()
