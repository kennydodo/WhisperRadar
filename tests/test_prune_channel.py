"""Paused (pruned) watched channels: hidden from the dashboard and the
active list, visible in their own view, and never resurrected by config sync."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class PruneChannelTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(tempfile.mkdtemp()) / "wr.db"
        self.cfg.studio_dir = Path(self.cfg.db_path).parent / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        db.add_channel(self.conn, "Active Chan", "UCactive", genre="tech")
        db.add_channel(self.conn, "Paused Chan", "UCpaused", genre="tech")
        db.update_channel(self.conn, "UCpaused", active=0)
        db.upsert_video(self.conn, "UCactive",
                        {"video_id": "vidA", "title": "Active Video",
                         "url": "u", "published_at": "2026-01-01"})
        db.upsert_video(self.conn, "UCpaused",
                        {"video_id": "vidP", "title": "Paused Video",
                         "url": "u", "published_at": "2026-01-02"})
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()

    def test_video_queries_can_exclude_paused_channels(self):
        titles = {r["title"] for r in db.get_videos(self.conn)}
        self.assertEqual(titles, {"Active Video", "Paused Video"})
        titles = {r["title"] for r in db.get_videos(self.conn, active_only=True)}
        self.assertEqual(titles, {"Active Video"})
        self.assertEqual(db.count_videos(self.conn, active_only=True), 1)
        rows, total = db.get_videos_page(self.conn, active_only=True)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["title"], "Active Video")

    def test_dashboard_hides_paused_channel_videos(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Active Video", html)
        self.assertNotIn("Paused Video", html)
        self.assertNotIn("Paused Chan", html)  # dropdown lists active only

    def test_watched_splits_active_and_paused_views(self):
        active = self.client.get("/watched").get_data(as_text=True)
        self.assertIn("Active Chan", active)
        self.assertNotIn("Paused Chan", active)
        self.assertIn("Paused (1)", active)
        paused = self.client.get("/watched?state=paused").get_data(as_text=True)
        self.assertIn("Paused Chan", paused)
        self.assertNotIn("Active Chan", paused)
        self.assertIn("Active (1)", paused)

    def test_sync_channels_keeps_a_paused_channel_paused(self):
        # config entry without an explicit active key must not resurrect it
        db.sync_channels(self.conn, [
            {"name": "Paused Chan", "id": "UCpaused", "kind": "primary",
             "genre": "tech"},
        ])
        self.assertEqual(db.get_channel(self.conn, "UCpaused")["active"], 0)

    def test_sync_channels_honours_explicit_active(self):
        db.sync_channels(self.conn, [
            {"name": "Paused Chan", "id": "UCpaused", "active": True},
        ])
        self.assertEqual(db.get_channel(self.conn, "UCpaused")["active"], 1)
        db.sync_channels(self.conn, [
            {"name": "Paused Chan", "id": "UCpaused", "active": False},
        ])
        self.assertEqual(db.get_channel(self.conn, "UCpaused")["active"], 0)

    def test_readding_a_paused_channel_reactivates_it(self):
        db.add_channel(self.conn, "Paused Chan", "UCpaused", genre="tech")
        self.assertEqual(db.get_channel(self.conn, "UCpaused")["active"], 1)

    def test_pause_and_activate_buttons(self):
        self.client.post("/channels/state",
                         data={"channel_id": "UCactive", "state": "active",
                               "active": "0"})
        self.assertEqual(db.get_channel(self.conn, "UCactive")["active"], 0)
        paused = self.client.get("/watched?state=paused").get_data(as_text=True)
        self.assertIn("Active Chan", paused)
        self.client.post("/channels/state",
                         data={"channel_id": "UCactive", "state": "paused",
                               "active": "1"})
        self.assertEqual(db.get_channel(self.conn, "UCactive")["active"], 1)

    def test_my_channels_tick_list_hides_paused(self):
        db.create_own_channel(self.conn, "My Channel")
        html = self.client.get("/my-channels").get_data(as_text=True)
        self.assertIn("Active Chan", html)
        self.assertNotIn("Paused Chan", html)

    def test_paused_pick_stays_selected_and_saved(self):
        import json
        oc_id = db.create_own_channel(self.conn, "My Channel")
        db.update_own_channel(self.conn, oc_id,
                              watched_channels=json.dumps(["UCactive", "UCpaused"]))
        html = self.client.get("/my-channels").get_data(as_text=True)
        self.assertNotIn("Paused Chan", html)  # no visible label
        self.assertIn('<input type="hidden" name="watched" value="UCpaused">',
                      html)  # but the pick survives
        # a browser submitting that form keeps the paused pick
        self.client.post("/my-channels/edit", data={
            "id": str(oc_id), "watched_present": "1",
            "watched": ["UCactive", "UCpaused"]})
        conn = db.connect(self.cfg.db_path)
        try:
            row = db.get_own_channel(conn, oc_id)
            self.assertEqual(json.loads(row["watched_channels"]),
                             ["UCactive", "UCpaused"])
        finally:
            conn.close()

    def test_auto_run_candidates_skip_paused_channels(self):
        from whisperradar import producer
        self.conn.execute("UPDATE videos SET status = 'transcribed'")
        self.conn.commit()
        titles = {r["title"] for r in producer.candidates(
            self.conn, {"genre": "tech"}, window_days=0)}
        self.assertEqual(titles, {"Active Video"})


if __name__ == "__main__":
    unittest.main()
