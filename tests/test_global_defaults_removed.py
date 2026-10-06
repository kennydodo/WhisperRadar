"""The global narration voice and per-genre bible/refs folders are gone.

Voice and art direction are per channel (own_channels.default_voice /
bible_dir / refs_dir) or per production; Settings > Production & images no
longer offers a global one. Old stored values must be ignored, not crash.

Run: python -m unittest tests.test_global_defaults_removed
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db, settings, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class GlobalDefaultsRemovedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_not_in_spec_or_groups(self):
        for key in ("default_voice", "seed_dirs"):
            self.assertNotIn(key, settings.SPEC_BY_KEY)
            self.assertNotIn(key, settings.load(self.conn))
            for _title, keys in settings.GROUPS:
                self.assertNotIn(key, keys)

    def test_settings_page_has_no_such_fields(self):
        html = create_app(self.cfg).test_client().get("/settings") \
            .get_data(as_text=True)
        self.assertNotIn("Narration voice", html)
        self.assertNotIn("bible/refs folders", html)
        self.assertNotIn('name="seed_dirs"', html)
        self.assertNotIn('name="default_voice"', html)

    def test_stale_stored_values_are_ignored(self):
        db.set_setting(self.conn, "default_voice", "old-voice")
        db.set_setting(self.conn, "seed_dirs", json.dumps({"general": "X:/s"}))
        self.conn.commit()
        self.assertNotIn("default_voice", settings.load(self.conn))
        prod = db.create_production(self.conn, "P", "general", None, None)
        row = db.get_production(self.conn, prod)
        self.assertIsNone(settings.for_production(self.conn, row)["voice"])

    def test_voice_is_production_then_channel_only(self):
        oc = db.create_own_channel(self.conn, "Ch")
        db.update_own_channel(self.conn, oc, default_voice="chan-voice")
        pid = db.create_production(self.conn, "P", "general", None, None)
        db.update_production(self.conn, pid, own_channel_id=oc)
        row = db.get_production(self.conn, pid)
        self.assertEqual(settings.for_production(self.conn, row)["voice"],
                         "chan-voice")
        db.update_production(self.conn, pid, voice="prod-voice")
        row = db.get_production(self.conn, pid)
        self.assertEqual(settings.for_production(self.conn, row)["voice"],
                         "prod-voice")

    def test_no_global_seed_for_a_channelless_production(self):
        seed = Path(self.tmp.name) / "seed"
        (seed / "refs").mkdir(parents=True)
        (seed / "bible.md").write_text("GLOBAL BIBLE", encoding="utf-8")
        db.set_setting(self.conn, "seed_dirs",
                       json.dumps({"general": str(seed)}))
        pid = db.create_production(self.conn, "P", "general", None, None)
        self.conn.commit()
        row = db.get_production(self.conn, pid)
        out = studio.seed_production(self.cfg, self.conn, row)
        self.assertEqual(out["source"], "")
        self.assertIsNone(studio.find_bible(studio.prod_dir(self.cfg, pid)))

    def test_channel_bible_dir_still_seeds(self):
        seed = Path(self.tmp.name) / "chan"
        seed.mkdir()
        (seed / "bible.md").write_text("CHANNEL BIBLE", encoding="utf-8")
        oc = db.create_own_channel(self.conn, "Ch")
        db.update_own_channel(self.conn, oc, bible_dir=str(seed))
        pid = db.create_production(self.conn, "P", "general", None, None)
        db.update_production(self.conn, pid, own_channel_id=oc)
        self.conn.commit()
        row = db.get_production(self.conn, pid)
        out = studio.seed_production(self.cfg, self.conn, row)
        self.assertTrue(out["bible"])


if __name__ == "__main__":
    unittest.main()
