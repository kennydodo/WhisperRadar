"""The source-transcript wait only blocks a script that still has to run, and
the Run till finish popup shows its LLM picker only while a text stage runs.

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

WAIT = {"state": "waiting", "video_id": "v1",
        "text": "waiting for the source transcript (new)"}


class SourceWaitScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        chan = db.create_own_channel(conn, "Chan")
        self.pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, self.pid, own_channel_id=chan)
        conn.close()
        self.pdir = studio.prod_dir(self.cfg, self.pid)

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def _wait(self, stage="script", lean=False):
        with mock.patch.object(autorun.source_prep, "state",
                               return_value=WAIT), \
                mock.patch.object(autorun, "lean_script", return_value=lean):
            return autorun.source_wait(self.cfg, self.pid, stage)

    def test_script_still_to_run_waits(self):
        self.assertEqual(self._wait(), WAIT)

    def test_existing_script_does_not_wait(self):
        (self.pdir / "script.md").write_text("a script", encoding="utf-8")
        self.assertIsNone(self._wait())

    def test_lean_script_does_not_wait(self):
        self.assertIsNone(self._wait(lean=True))

    def test_other_stages_never_wait(self):
        for stage in ("style", "audio", "srt", "shots", "merge"):
            self.assertIsNone(self._wait(stage), stage)

    def test_done_script_lets_later_stages_run(self):
        (self.pdir / "script.md").write_text("a script", encoding="utf-8")
        with mock.patch.object(autorun.source_prep, "state",
                               return_value=WAIT):
            for stage in autorun.RUN_STAGES:
                entry = autorun.stage_action(self.cfg, self.pid, stage)
                self.assertNotEqual(entry["detail"], WAIT["text"], stage)

    def test_popup_shows_llm_picker_only_while_a_text_stage_runs(self):
        client = create_app(self.cfg).test_client()
        html = client.get(f"/studio/{self.pid}").get_data(as_text=True)
        self.assertIn("needsLlm", html)
        self.assertIn("Are you sure you want to continue?", html)


if __name__ == "__main__":
    unittest.main()
