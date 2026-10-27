"""The per-production "Target words" box feeds studio.script_target_words."""
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


class ScriptWordsBoxTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(tempfile.mkdtemp()) / "wr.db"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.pid = db.create_production(conn, "T", "general")
        conn.close()
        self.client = create_app(self.cfg).test_client()

    def _prod(self):
        conn = db.connect(self.cfg.db_path)
        try:
            return db.get_production(conn, self.pid)
        finally:
            conn.close()

    def test_explicit_value_wins_over_source_cap_and_default(self):
        self.assertEqual(studio.script_target_words(None, 0), 4000)
        self.assertEqual(studio.script_target_words(None, 800), 4000)
        self.assertEqual(studio.script_target_words(None, 800, explicit=2500), 2500)
        self.assertEqual(studio.script_target_words(None, 0, explicit=2500), 2500)

    def test_external_prompts_default_to_4000(self):
        from whisperradar import external_prompts as ep
        self.assertEqual(ep._target_words(self.cfg, self.pid, None), 4000)

    def test_box_saves_reads_and_clears(self):
        self.client.post(f"/studio/{self.pid}/script-words",
                         data={"script_words": "2,500"})
        self.assertEqual(studio.script_words_override(self._prod()), 2500)
        # other per-stage directions are untouched
        self.client.post(f"/studio/{self.pid}/extra/save",
                         data={"stage": "script", "extra_prompt": "hook first"})
        self.assertEqual(studio.script_words_override(self._prod()), 2500)
        self.assertEqual(db.stage_extra(self._prod(), "script"), "hook first")
        self.client.post(f"/studio/{self.pid}/script-words",
                         data={"script_words": ""})
        self.assertIsNone(studio.script_words_override(self._prod()))

    def test_out_of_range_is_refused(self):
        self.client.post(f"/studio/{self.pid}/script-words",
                         data={"script_words": "5"})
        self.assertIsNone(studio.script_words_override(self._prod()))

    def test_both_llm_prompts_use_it(self):
        from whisperradar import external_prompts as ep
        self.client.post(f"/studio/{self.pid}/script-words",
                         data={"script_words": "2000"})
        self.assertEqual(ep._target_words(self.cfg, self.pid, None), 2000)

    def test_stage_page_shows_the_box(self):
        page = self.client.get(f"/studio/{self.pid}?stage=script").get_data(as_text=True)
        self.assertIn('name="script_words"', page)
        self.assertIn('placeholder="4000"', page)


if __name__ == "__main__":
    unittest.main()
