"""The from-scratch writing-style prompt (kept for the prompt builders).

Stage 1 itself no longer generates a style: it loads the channel's fixed
writing style (see test_channel_writing_style.py).

Run: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

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
        wr_tmp.cleanup(self.tmp)

    def test_prompt_uses_title_channel_and_notes(self):
        p = studio.style_prompt_from_scratch(
            "Morning rituals", "general", "Calm Habits", "Gentle tips",
            "warm")
        for bit in ("Morning rituals", "Calm Habits", "Gentle tips", "warm",
                    "## Voice & Tone"):
            self.assertIn(bit, p)
        self.assertNotIn("TRANSCRIPT TO ANALYZE", p)


if __name__ == "__main__":
    unittest.main()
