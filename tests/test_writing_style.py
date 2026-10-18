"""Stage 1 (style) is the source video's WRITING style, kept in
writing_style.md. The channel's ART style (style.md) is for images only and
must never satisfy stage 1 or reach the script writer/judge."""
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


class WritingStyleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.pid = db.create_production(conn, "P", "general", None, None)
        conn.commit()
        conn.close()
        self.pdir = studio.prod_dir(self.cfg, self.pid)
        self.pdir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def test_art_style_does_not_count_as_writing_style(self):
        (self.pdir / "style.md").write_text("Art style: watercolor\n",
                                            encoding="utf-8")
        self.assertIsNone(studio.find_writing_style(self.pdir))
        self.assertNotEqual(
            autorun.stage_action(self.cfg, self.pid, "style")["action"],
            "skip")

    def test_writing_style_skips_stage_one(self):
        (self.pdir / "writing_style.md").write_text("## Voice & Tone\n",
                                                    encoding="utf-8")
        self.assertEqual(
            autorun.stage_action(self.cfg, self.pid, "style")["action"],
            "skip")

    def _channel_prod(self, writing_style=None):
        conn = db.connect(self.cfg.db_path)
        try:
            chan = db.create_own_channel(conn, "C", writing_style=writing_style) \
                if writing_style else db.create_own_channel(conn, "C")
            db.update_production(conn, self.pid, own_channel_id=chan)
        finally:
            conn.close()

    def test_run_style_loads_the_channel_style_and_never_generates(self):
        (self.pdir / "style.md").write_text("Art style: watercolor\n",
                                            encoding="utf-8")
        self._channel_prod("## Voice & Tone\ncalm")
        with mock.patch.object(studio, "llm_generate") as llm:
            autorun._run_style(self.cfg, self.pid, provider="x")
        llm.assert_not_called()
        self.assertIn("Voice & Tone",
                      (self.pdir / "writing_style.md").read_text("utf-8"))
        self.assertEqual((self.pdir / "style.md").read_text("utf-8").strip(),
                         "Art style: watercolor")

    def test_run_style_pauses_when_the_channel_has_no_style(self):
        self._channel_prod()
        with mock.patch.object(studio, "llm_generate") as llm:
            with self.assertRaises(autorun._Paused):
                autorun._run_style(self.cfg, self.pid, provider="x")
        llm.assert_not_called()
        self.assertIsNone(studio.find_writing_style(self.pdir))

if __name__ == "__main__":
    unittest.main()
