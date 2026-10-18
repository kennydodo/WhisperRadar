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

    def test_skipped_stage_is_grayed_out_and_created_is_shown(self):
        pid = self._make("Skipper")
        db.update_production(self.conn, pid, stage="motion")
        plan = [{"stage": "motion", "action": "skip", "detail": "off"},
                {"stage": "merge", "action": "run", "detail": "final"}]
        with mock.patch.object(autorun, "build_plan", return_value=plan):
            html = self.client.get("/in-production").get_data(as_text=True)
        self.assertIn('badge off"', html)       # motion grayed out
        self.assertIn("motion skipped", html)
        self.assertIn("next: merge", html)
        self.assertIn(time.strftime("%Y"), html)   # created column

    def test_everything_done_shows_review(self):
        pid = self._make("Done")
        db.update_production(self.conn, pid, stage="motion")
        plan = [{"stage": "merge", "action": "skip", "detail": "done"}]
        with mock.patch.object(autorun, "build_plan", return_value=plan):
            html = self.client.get("/in-production").get_data(as_text=True)
        self.assertIn("ready for review", html)

    def test_productions_of_paused_channels_are_hidden(self):
        self._make("Visible One")
        other = db.create_own_channel(self.conn, "Paused Ch")
        pid = db.create_production(self.conn, "Hidden One", "general",
                                   None, None)
        db.update_production(self.conn, pid, own_channel_id=other)
        self.conn.execute("UPDATE own_channels SET active = 0 WHERE id = ?",
                          (other,))
        self.conn.commit()
        html = self.client.get("/in-production").get_data(as_text=True)
        self.assertIn("Visible One", html)
        self.assertNotIn("Hidden One", html)

    def test_production_page_has_an_open_folder_button(self):
        from whisperradar import studio
        pid = self._make("Inside")
        html = self.client.get(f"/studio/{pid}").get_data(as_text=True)
        self.assertIn(f"/studio/{pid}/open-folder", html)
        with mock.patch.object(studio, "open_folder") as op:
            resp = self.client.post(f"/studio/{pid}/open-folder")
        op.assert_called_once()
        self.assertEqual(Path(op.call_args[0][0]),
                         studio.prod_dir(self.cfg, pid))
        self.assertIn("msg=", resp.headers["Location"])

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


    def test_sort_dropdown_orders_by_channel_stage_and_created(self):
        other = db.create_own_channel(self.conn, "Aardvark Channel")
        a = self._make("Row A")                     # Channel A
        b = self._make("Row B")
        c = self._make("Row C")
        db.update_production(self.conn, c, own_channel_id=other)
        db.update_production(self.conn, a, stage="images")
        db.update_production(self.conn, b, stage="script")
        db.update_production(self.conn, c, stage="audio")
        self.conn.execute("UPDATE productions SET created_at=? WHERE id=?", ("2026-10-01 10:00:00", a))
        self.conn.execute("UPDATE productions SET created_at=? WHERE id=?", ("2026-10-03 10:00:00", b))
        self.conn.execute("UPDATE productions SET created_at=? WHERE id=?", ("2026-10-02 10:00:00", c))
        self.conn.commit()

        def order(q):
            html = self.client.get("/in-production" + q).get_data(as_text=True)
            pos = {t: html.index(t) for t in ("Row A", "Row B", "Row C")}
            return [t[-1] for t in sorted(pos, key=pos.get)]

        self.assertIn('name="sort"', self.client.get("/in-production").get_data(as_text=True))
        self.assertEqual(order(""), ["C", "A", "B"])                    # channel name, then id
        self.assertEqual(order("?sort=stage"), ["B", "C", "A"])        # script, audio, images
        self.assertEqual(order("?sort=created"), ["B", "C", "A"])      # newest first
        self.assertEqual(order("?sort=created_old"), ["A", "C", "B"])
        self.assertEqual(order("?sort=nonsense"), ["C", "A", "B"])


if __name__ == "__main__":
    unittest.main()
