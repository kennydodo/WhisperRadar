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
            "subscribers": 200, "videos": 12, "about": "home tips"}]
        with patch.object(youtube_api, "Client"), patch.object(
                youtube_api, "discover_channels", return_value=found) as search:
            response = self.client.post("/watched/UCseed/similar", data={"q": "japanese home"})
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
