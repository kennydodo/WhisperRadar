"""Which LLM the style/script/shots stages use.

precedence: the production's own `llm_provider`, then its channel's
`producer_llm_provider`, then the global `studio.llm_default`. The channel step
was missing, so a channel set to deepseek still ran on the global glm-flash
(which stalls on large prompts).

Run: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar.config import load_config  # noqa: E402
from whisperradar import autorun, db  # noqa: E402


class ProviderSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_llm_default = "glm-flash"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.chan = db.create_own_channel(conn, "Ch", producer_llm_provider="deepseek")
        self.pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, self.pid, own_channel_id=self.chan)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_channel_preference_is_used(self):
        self.assertEqual(autorun._default_provider(self.cfg, self.pid), "deepseek")

    def test_the_production_beats_the_channel(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.update_production(conn, self.pid, llm_provider="gpt")
        conn.commit()
        conn.close()
        self.assertEqual(autorun._default_provider(self.cfg, self.pid), "gpt")

    def test_the_global_default_is_the_last_resort(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.update_own_channel(conn, self.chan, producer_llm_provider=None)
        conn.commit()
        conn.close()
        self.assertEqual(autorun._default_provider(self.cfg, self.pid), "glm-flash")


if __name__ == "__main__":
    unittest.main()
