"""A channel's bible text must reach productions created before it was filled
in.

The shots stage's bible pre-checks (the auto-run plan and the manual
"Plan shotlist" route) used to pause on a missing production bible.md WITHOUT
running the channel seeding - so a bible filled in on My Channels after the
production was created never reached it, and the gate fired forever.

Run: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import autorun, db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402

BIBLE = "MAYA - the host, woman in her 30s, black hair, mustard scarf."


class BibleSeedingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.chan = db.create_own_channel(conn, "Ch", bible=BIBLE)
        self.pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, self.pid, own_channel_id=self.chan)
        conn.commit()
        conn.close()

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def test_stage_action_seeds_the_channel_bible(self):
        entry = autorun.stage_action(self.cfg, self.pid, "shots")
        self.assertEqual(entry["action"], "run")
        bible = studio.find_bible(studio.prod_dir(self.cfg, self.pid))
        self.assertIsNotNone(bible)
        self.assertIn("MAYA", bible.read_text(encoding="utf-8"))

    def test_pause_when_no_bible_anywhere(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        bare = db.create_own_channel(conn, "Bare")
        pid2 = db.create_production(conn, "P2", "general", None, None)
        db.update_production(conn, pid2, own_channel_id=bare)
        conn.commit()
        conn.close()
        entry = autorun.stage_action(self.cfg, pid2, "shots")
        self.assertEqual(entry["action"], "pause")
        self.assertIn("bible", entry["detail"])


if __name__ == "__main__":
    unittest.main()
