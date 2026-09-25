"""The My-channels form saves the judge LLMs.

A provider added in Settings > Providers lives in the DB, not config.yaml. The
form used to validate against config.yaml only, so picking one of those saved
NULL ("channel setting changes are not saved for judge llm").

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
from whisperradar.webapp import create_app  # noqa: E402
from whisperradar import db, studio  # noqa: E402

UI_PROVIDER = "openrouter-gpt 6 luna"   # NOT a config.yaml provider name


class ChannelJudgeSaveTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(tempfile.mkdtemp()) / "wr.db"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        # a provider that exists only in the DB (as the Providers page saves it)
        db.set_setting(conn, "llm_providers", json.dumps([
            {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
             "api_key": "k", "env_key": "WR_OPENROUTER_API_KEY",
             "models": [{"id": "openai/gpt-6-luna", "name": UI_PROVIDER}]},
        ]))
        self.oc = db.create_own_channel(conn, "Ch")
        conn.commit()
        conn.close()
        self.client = create_app(self.cfg).test_client()

    def test_a_db_provider_is_saved_as_the_judge(self):
        self.assertIn(UI_PROVIDER, [p["name"] for p in studio.providers(self.cfg)])
        resp = self.client.post("/my-channels/edit", data={
            "id": str(self.oc),
            "name": "Ch",
            "script_judge_provider": UI_PROVIDER,
            "shotlist_judge_provider": UI_PROVIDER,
        })
        self.assertIn(resp.status_code, (302, 303))
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        row = db.get_own_channel(conn, self.oc)
        conn.close()
        self.assertEqual(row["script_judge_provider"], UI_PROVIDER)
        self.assertEqual(row["shotlist_judge_provider"], UI_PROVIDER)

    def test_an_unknown_provider_still_clears_the_override(self):
        resp = self.client.post("/my-channels/edit", data={
            "id": str(self.oc), "name": "Ch",
            "script_judge_provider": "not-a-provider",
        })
        self.assertIn(resp.status_code, (302, 303))
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        row = db.get_own_channel(conn, self.oc)
        conn.close()
        self.assertIsNone(row["script_judge_provider"])


if __name__ == "__main__":
    unittest.main()
