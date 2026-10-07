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

    def test_run_style_writes_writing_style_not_art_style(self):
        (self.pdir / "style.md").write_text("Art style: watercolor\n",
                                            encoding="utf-8")
        with mock.patch.object(autorun, "_source_transcript_text",
                               return_value="some transcript words"), \
             mock.patch.object(studio, "llm_generate",
                               return_value="## Voice & Tone\ncalm"):
            autorun._run_style(self.cfg, self.pid, provider="x")
        self.assertIn("Voice & Tone",
                      (self.pdir / "writing_style.md").read_text("utf-8"))
        self.assertEqual((self.pdir / "style.md").read_text("utf-8").strip(),
                         "Art style: watercolor")


if __name__ == "__main__":
    unittest.main()
