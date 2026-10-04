"""The writing-style stage no longer needs a source transcript.

With a source video it still analyses that narration. Without one, the guide
is written from the title, the channel and the creator's direction, the
button is available, and auto-run runs the stage instead of pausing.

Run: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import autorun, db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class StyleWithoutSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.chan = db.create_own_channel(conn, "Calm Habits")
        conn.execute("UPDATE own_channels SET description=? WHERE id=?",
                     ("Gentle Japanese living tips", self.chan))
        conn.commit()
        self.pid = db.create_production(conn, "Morning rituals", "general",
                                        None, None)
        db.update_production(conn, self.pid, own_channel_id=self.chan)
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_prompt_uses_title_channel_and_notes(self):
        p = studio.style_prompt_from_scratch(
            "Morning rituals", "general", "Calm Habits", "Gentle tips",
            "warm")
        for bit in ("Morning rituals", "Calm Habits", "Gentle tips", "warm",
                    "## Voice & Tone"):
            self.assertIn(bit, p)
        self.assertNotIn("TRANSCRIPT TO ANALYZE", p)

    def test_auto_run_plans_the_style_stage_instead_of_pausing(self):
        entry = autorun.stage_action(self.cfg, self.pid, "style")
        self.assertEqual(entry["action"], "run")

    def test_stage_writes_the_guide_without_a_source(self):
        seen = {}

        def fake(cfg, prompt, provider=None, **kw):
            seen["prompt"] = prompt
            return "## Voice & Tone\nwarm"

        with mock.patch.object(studio, "llm_generate", fake):
            autorun._run_style(self.cfg, self.pid, provider="x")
        self.assertIn("Calm Habits", seen["prompt"])
        self.assertIn("Gentle Japanese living tips", seen["prompt"])
        pdir = studio.prod_dir(self.cfg, self.pid)
        self.assertTrue((pdir / "writing_style.md").exists())

    def test_button_is_not_disabled_for_lack_of_a_source(self):
        client = create_app(self.cfg).test_client()
        with mock.patch.object(studio, "provider_ready", return_value=True), \
                mock.patch.object(studio, "providers",
                                  return_value=[{"name": "x", "label": "x"}]):
            html = client.get(f"/studio/{self.pid}?stage=style").get_data(
                as_text=True)
        i = html.index("/style/generate")
        button = html[i:html.index("</button>", i)]
        self.assertNotIn("disabled", button)


if __name__ == "__main__":
    unittest.main()
