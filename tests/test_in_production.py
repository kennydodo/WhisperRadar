"""The In production page: every active production with its channel, id,
title (100 chars), stage, and Continue / Auto-run buttons."""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import autorun, db  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class InProductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        self.chan = db.create_own_channel(self.conn, "Channel A")
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def _make(self, title, status="active"):
        pid = db.create_production(self.conn, title, "general", None, None)
        db.update_production(self.conn, pid, own_channel_id=self.chan,
                             status=status)
        return pid

    def test_lists_active_only_with_channel_id_and_stage(self):
        a = self._make("Alive One")
        self._make("Done One", status="ready")
        self._make("Out One", status="published")
        html = self.client.get("/in-production").get_data(as_text=True)
        self.assertIn("Alive One", html)
        self.assertNotIn("Done One", html)
        self.assertNotIn("Out One", html)
        self.assertIn("Channel A", html)
        self.assertIn(f"#{a}", html)
        self.assertIn(f"/studio/{a}/continue", html)
        self.assertIn(f"/studio/{a}/auto-run", html)

    def test_title_is_cut_at_100_characters(self):
        self._make("x" * 130)
        html = self.client.get("/in-production").get_data(as_text=True)
        self.assertIn("x" * 100, html)
        self.assertNotIn("x" * 101, html)

    def test_continue_runs_one_stage(self):
        pid = self._make("Step")
        seen = {}

        def fake(cfg, p, **kw):
            seen.update(kw)
            return "ok"
        plan = [{"stage": "script", "action": "run", "detail": "x"}]
        with mock.patch.object(autorun, "build_plan", return_value=plan), \
                mock.patch.object(autorun, "run_pipeline", side_effect=fake):
            resp = self.client.post(f"/studio/{pid}/continue",
                                    data={"back": "in-production"})
            time.sleep(0.3)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/in-production", resp.headers["Location"])
        self.assertTrue(seen.get("one_stage"))

    def test_continue_refuses_when_paused(self):
        pid = self._make("Stuck")
        plan = [{"stage": "audio", "action": "pause", "detail": "no audio"}]
        with mock.patch.object(autorun, "build_plan", return_value=plan):
            resp = self.client.post(f"/studio/{pid}/continue",
                                    data={"back": "in-production"})
        self.assertIn("error=", resp.headers["Location"])

    def test_run_pipeline_one_stage_stops_after_one(self):
        pid = self._make("One")
        ran = []
        entries = {"style": "run", "script": "run"}
        with mock.patch.object(autorun, "stage_action",
                               side_effect=lambda c, p, s: {
                                   "stage": s, "action": entries.get(s, "skip"),
                                   "detail": ""}), \
                mock.patch.object(autorun, "run_stage",
                                  side_effect=lambda c, p, s, params=None:
                                  ran.append(s) or "ok"), \
                mock.patch.object(autorun, "_auto_plan"):
            result = autorun.run_pipeline(self.cfg, pid, one_stage=True)
        self.assertEqual(result, "ok")
        self.assertEqual(ran, ["style"])


if __name__ == "__main__":
    unittest.main()
