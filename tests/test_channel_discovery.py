import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
from whisperradar import channel_discovery, db, youtube_api
from tests import wr_tmp


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = SimpleNamespace(db_path=Path(self.tmp.name) / "wr.db")
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.add_channel(conn, "Home channel", "UCseed", genre="home habits")
        db.upsert_video(conn, "UCseed", {"video_id": "v1", "title": "Japanese clean home habits"})
        conn.close()
        app = Flask("whisperradar", template_folder=str(
            Path(__file__).resolve().parents[1] / "whisperradar/templates"))
        channel_discovery.register(app, self.cfg)
        self.client = app.test_client()

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def test_get_suggests_topic_without_api(self):
        with patch.object(youtube_api, "discover_channels") as search:
            response = self.client.get("/watched/UCseed/similar")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Japanese clean home habits", response.data)
        self.assertIn(b"Search this topic on YouTube", response.data)
        search.assert_not_called()
        self.assertEqual(self.client.get("/watched/missing/similar").status_code, 404)

    def test_search_is_saved_and_excludes_watched_on_reload(self):
        found = [{"channel_id": "UCnew", "name": "New Home", "sample": [
            {"title": "Japanese home routine", "video_id": "v2"}],
            "subscribers": 200, "videos": 12, "about": "home tips", "default_language": "en"}]
        with patch.object(youtube_api, "Client"), patch.object(
                youtube_api, "discover_channels", return_value=found) as search:
            response = self.client.post("/watched/UCseed/similar", data={"q": "japanese home"}, follow_redirects=True)
        self.assertIn(b"New Home", response.data)
        self.assertIn(b"Watch this channel", response.data)
        self.assertIn(b"japanese, home", response.data.replace(b"home, japanese", b"japanese, home"))
        self.assertEqual(search.call_args.args[2], {"UCseed"})
        self.assertIn(b"New Home", self.client.get("/watched/UCseed/similar").data)
        conn = db.connect(self.cfg.db_path)
        db.add_channel(conn, "New Home", "UCnew")
        conn.close()
        self.assertNotIn(b"Watch this channel", self.client.get("/watched/UCseed/similar").data)

    def test_missing_key_and_empty_topic(self):
        with patch.object(youtube_api, "Client", side_effect=youtube_api.NoKey("No API key")):
            response = self.client.post("/watched/UCseed/similar", data={"q": "home"})
        self.assertIn(b"No API key", response.data)
        with patch.object(youtube_api, "Client") as client:
            response = self.client.post("/watched/UCseed/similar", data={"q": " "})
        client.assert_not_called()
        self.assertIn(b"Enter a topic", response.data)

    def test_query_frequency_and_genre_fallback(self):
        self.assertEqual(channel_discovery.seed_query(["10 home habits", "home routines"]),
                         "home habits routines")
        self.assertEqual(channel_discovery.seed_query([], "finance"), "finance")

    def test_local_filters_and_history_use_no_api(self):
        import json
        old = {"query": "home", "channels": [
            {"channel_id": "small", "name": "Small", "subscribers": 2000,
             "videos": 20, "sample": [], "shared": [], "default_language": "en"},
            {"channel_id": "big", "name": "Big", "subscribers": 2000000,
             "videos": 30, "sample": [], "shared": [], "default_language": "en"},
            {"channel_id": "hidden", "name": "Hidden", "subscribers": None,
             "videos": 20, "sample": [], "shared": [], "default_language": "en"}]}
        newer = channel_discovery.merge_search(old, "habits", [
            {"channel_id": "small", "name": "Small", "subscribers": 2100,
             "videos": 21, "sample": [], "shared": []}])
        self.assertEqual(len(newer["history"]), 2)
        self.assertEqual(len(newer["channels"]), 3)
        cache = Path(self.tmp.name) / "similar_channel_searches.json"
        cache.write_text(json.dumps({"UCseed": newer}))
        with patch.object(youtube_api, "Client") as client:
            page = self.client.get("/watched/UCseed/similar?min_subs=1000&max_subs=10000&min_videos=10&max_videos=25")
            self.assertIn(b">Small</a>", page.data)
            self.assertNotIn(b">Big</a>", page.data)
            self.assertNotIn(b">Hidden</a>", page.data)
            page = self.client.get("/watched/UCseed/similar?hidden=include")
            self.assertIn(b">Hidden</a>", page.data)
            self.assertIn(b"Saved search history (2)", page.data)
        client.assert_not_called()
        self.assertEqual(json.loads(cache.read_text())["UCseed"], newer)

    def test_invalid_filters_never_search(self):
        with patch.object(youtube_api, "Client") as client:
            response = self.client.post("/watched/UCseed/similar", data={
                "q": "home", "min_subs": "100", "max_subs": "10"})
        client.assert_not_called()
        self.assertIn(b"minimum must not exceed maximum", response.data)

    def test_language_evidence_and_local_filter(self):
        english = {"default_language": "en-US", "subscribers": 200, "videos": 20}
        other = {"default_language": "es", "subscribers": 200, "videos": 20,
                 "about": "This is our channel for your home and habits"}
        unknown = {"subscribers": 200, "videos": 20, "about": "Clean home"}
        self.assertEqual(channel_discovery.language_info(english)[0], "en")
        self.assertEqual(channel_discovery.language_info(other)[0], "es")
        self.assertEqual(channel_discovery.language_info(unknown)[0], "unknown")
        self.assertEqual(channel_discovery.language_info({"about":
            "Welcome to our channel for your home and your daily habits"})[0], "en")
        self.assertEqual(channel_discovery.language_info({"about":
            "Namastey main hoon Chetna. Welcome to our channel for your home"})[0], "unknown")
        filters = channel_discovery.filters_from({})
        self.assertEqual(channel_discovery.filter_matches([english, other, unknown], filters), [english])
        filters["language"] = "all"
        self.assertEqual(len(channel_discovery.filter_matches([english, other, unknown], filters)), 3)
