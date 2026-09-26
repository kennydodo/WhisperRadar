"""Reset LLM providers: a full fresh start for a broken or inherited setup.

Removing a provider from the editor must not leave stale references behind -
the Default LLM, the judge picks, the channels' producer LLM and production
pins all used to keep a deleted provider "alive".

Run: python -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

PROVIDERS = [
    {"name": "glm-5.3-flash", "base_url": "https://api.b.ai/v1",
     "api_key": "", "env_key": "WR_GLM_FLASH_API_KEY",
     "models": [{"id": "glm-5.3-flash", "name": "glm-5.3-flash"}]},
    {"name": "openrouter-claude", "base_url": "https://openrouter.ai/api/v1",
     "api_key": "sk-test", "env_key": "WR_OPENROUTER_API_KEY",
     "models": [{"id": "anthropic/claude-sonnet-4",
                 "name": "openrouter-claude"}]},
]


class ProvidersResetTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(tempfile.mkdtemp()) / "wr.db"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.set_setting(conn, "llm_providers", json.dumps(PROVIDERS))
        db.set_setting(conn, "llm_default", "glm-5.3-flash")
        # one judge points at the DELETED provider, one at a KEPT one - only
        # the deleted reference may be cleared
        db.set_setting(conn, "script_judge_provider", "glm-5.3-flash")
        db.set_setting(conn, "shotlist_judge_provider", "openrouter-claude")
        self.chan = db.create_own_channel(
            conn, "Ch", producer_llm_provider="glm-5.3-flash")
        self.pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, self.pid, own_channel_id=self.chan,
                             llm_provider="glm-5.3-flash")
        conn.commit()
        conn.close()
        self.client = create_app(self.cfg).test_client()

    def test_reset_removes_providers_and_every_reference(self):
        r = self.client.post("/settings/providers/reset",
                             follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        conn = db.connect(self.cfg.db_path)
        self.assertIsNone(db.get_setting(conn, "llm_providers"))
        self.assertIsNone(db.get_setting(conn, "llm_default"))
        self.assertIsNone(db.get_setting(conn, "script_judge_provider"))
        self.assertIsNone(db.get_setting(conn, "shotlist_judge_provider"))
        chan = conn.execute("select producer_llm_provider from own_channels"
                            ).fetchone()
        prod = conn.execute("select llm_provider from productions"
                            ).fetchone()
        conn.close()
        self.assertIsNone(chan[0])
        self.assertIsNone(prod[0])
        self.assertEqual(studio.providers(self.cfg), [])

    def test_reset_then_readd_works(self):
        self.client.post("/settings/providers/reset", follow_redirects=False)
        r = self.client.post(
            "/settings/providers",
            data={"providers_json": json.dumps(PROVIDERS)},
            follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        conn = db.connect(self.cfg.db_path)
        self.assertEqual(len(json.loads(db.get_setting(conn,
                                                       "llm_providers"))), 2)
        conn.close()
        self.assertEqual(len(studio.providers(self.cfg)), 2)

    def test_deleting_a_specific_provider_cleans_its_references(self):
        # remove the glm gateway card but keep openrouter: every reference to
        # glm-5.3-flash must be cleared, references to openrouter-claude kept
        kept = [PROVIDERS[1]]
        r = self.client.post("/settings/providers",
                             data={"providers_json": json.dumps(kept)},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        conn = db.connect(self.cfg.db_path)
        self.assertEqual(db.get_setting(conn, "llm_default"), "")
        self.assertEqual(db.get_setting(conn, "script_judge_provider"), "")
        self.assertEqual(db.get_setting(conn, "shotlist_judge_provider"),
                         "openrouter-claude")
        chan = conn.execute("select producer_llm_provider from own_channels"
                            ).fetchone()
        prod = conn.execute("select llm_provider from productions"
                            ).fetchone()
        conn.close()
        self.assertIsNone(chan[0])
        self.assertIsNone(prod[0])


if __name__ == "__main__":
    unittest.main()
