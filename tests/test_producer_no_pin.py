"""The Auto Run producer must not pin llm_provider on a production it creates.

autorun._run_script/_run_shots stopped writing productions.llm_provider
(commit 2615632) because it silently outranks the channel's
producer_llm_provider and the global Default LLM forever - a stale pin never
lets a later Default LLM change take effect. producer.run() (the fully
automated per-channel producer) had the exact same implicit-pin bug on the
row it creates; this guards the fix.

Run: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db, producer, settings, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402


class ProducerDoesNotPinProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.set_setting(conn, "autorun_enabled", "1")
        # Keep the Auto Run window permanently open (start == end is the
        # documented always-open case): without this the producer correctly
        # pauses outside 09:00-23:00 and this test fails just for running late.
        db.set_setting(conn, "run_window_start", "00:00")
        db.set_setting(conn, "run_window_end", "00:00")
        db.set_setting(conn, "producer_llm_provider", "deepseek")
        db.add_channel(conn, "Source", "src1", genre="general")
        db.upsert_video(conn, "src1", {
            "video_id": "vid1", "title": "A source video",
            "url": "https://x", "published_at": "2026-09-01"})
        conn.execute("UPDATE videos SET status = 'transcribed'"
                     " WHERE video_id = 'vid1'")
        self.oc = db.create_own_channel(conn, "Own", genre="general")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_created_production_has_no_llm_provider_pin(self):
        with mock.patch.object(studio, "seed_production",
                               return_value=False):
            result = producer.run(self.cfg, stop_before="style")
        self.assertEqual(len(result["created"]), 1)
        pid = result["created"][0]["pid"]
        conn = db.connect(self.cfg.db_path)
        row = conn.execute("SELECT llm_provider, own_channel_id, autorun"
                           " FROM productions WHERE id = ?", (pid,)).fetchone()
        conn.close()
        self.assertIsNone(row["llm_provider"])
        self.assertEqual(row["own_channel_id"], self.oc)
        self.assertEqual(row["autorun"], 1)


if __name__ == "__main__":
    unittest.main()
