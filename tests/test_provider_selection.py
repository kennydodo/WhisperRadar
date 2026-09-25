"""Which LLM the style/script/shots stages use.

precedence: the production's own `llm_provider`, then its channel's
`producer_llm_provider`, then the global `studio.llm_default`. The channel step
was missing, so a channel set to deepseek still ran on the global glm-flash
(which stalls on large prompts).

Run: python -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar.config import load_config  # noqa: E402
from whisperradar import autorun, db, studio  # noqa: E402


class ProvidersFlattenTests(unittest.TestCase):
    """Settings saves one entry per gateway + key with several models; the
    pipeline needs one flat entry per MODEL sharing that gateway's key."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.set_setting(conn, "llm_providers", json.dumps([
            {"name": "b.ai", "base_url": "https://api.b.ai/v1",
             "api_key": "K", "env_key": "WR_BAI",
             "models": [{"id": "deepseek-v4.1-flash", "name": "deepseek"},
                        {"id": "glm-5.2", "name": "glm"}]},
            {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
             "api_key": "O", "env_key": "WR_OPENROUTER_API_KEY",
             "models": [{"id": "openai/gpt-5", "name": "gpt"}]},
        ]))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_key_many_models(self):
        provs = studio.providers(self.cfg)
        self.assertEqual([p["name"] for p in provs], ["deepseek", "glm", "gpt"])
        by = {p["name"]: p for p in provs}
        self.assertEqual(by["deepseek"]["model"], "deepseek-v4.1-flash")
        self.assertEqual(by["deepseek"]["base_url"], "https://api.b.ai/v1")
        self.assertEqual(by["deepseek"]["api_key"], "K")
        self.assertEqual(by["gpt"]["gateway"], "OpenRouter")

    def test_config_providers_apply_when_nothing_is_saved(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.set_setting(conn, "llm_providers", "[]")
        conn.commit()
        conn.close()
        names = [p["name"] for p in studio.providers(self.cfg)]
        self.assertIn("deepseek", names)

    def test_the_judge_prefers_a_different_gateway(self):
        # the writer is on api.b.ai; the judge must not share that gateway
        self.assertEqual(studio.judge_provider(self.cfg, "deepseek", None), "gpt")

    def test_the_fallback_prefers_a_different_gateway(self):
        alt = studio._fallback_provider(self.cfg, "deepseek")
        self.assertIsNotNone(alt)
        self.assertEqual(alt["name"], "gpt")


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
