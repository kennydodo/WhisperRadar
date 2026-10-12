"""Bulk actions on the Watched Channels list: tick several channels, apply
one change (pause / activate / genre / kind / remove) to all of them."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class WatchedBulkTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(tempfile.mkdtemp()) / "wr.db"
        self.cfg.studio_dir = Path(self.cfg.db_path).parent / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        for name, cid in (("A", "UCa"), ("B", "UCb"), ("C", "UCc")):
            db.add_channel(self.conn, name, cid, genre="tech")
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()

    def _row(self, cid):
        return db.get_channel(self.conn, cid)

    def _post(self, **form):
        return self.client.post("/channels/bulk", data=form)

    def test_page_has_checkboxes_and_bulk_bar(self):
        html = self.client.get("/watched").get_data(as_text=True)
        self.assertIn('name="ids"', html)
        self.assertIn('id="bulk-form"', html)
        self.assertIn('id="select-all"', html)

    def test_pause_and_activate_many(self):
        r = self._post(ids=["UCa", "UCb"], bulk_action="pause")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._row("UCa")["active"], 0)
        self.assertEqual(self._row("UCb")["active"], 0)
        self.assertEqual(self._row("UCc")["active"], 1)
        self._post(ids=["UCa", "UCb"], bulk_action="activate", state="paused")
        self.assertEqual(self._row("UCa")["active"], 1)
        self.assertEqual(self._row("UCb")["active"], 1)

    def test_set_genre_and_kind(self):
        self._post(ids=["UCa", "UCc"], bulk_action="genre", bulk_value="finance")
        self.assertEqual(self._row("UCa")["genre"], "finance")
        self.assertEqual(self._row("UCc")["genre"], "finance")
        self.assertEqual(self._row("UCb")["genre"], "tech")
        self._post(ids=["UCa", "UCb"], bulk_action="kind", bulk_value="similar")
        self.assertEqual(self._row("UCa")["kind"], "similar")
        self.assertEqual(self._row("UCb")["kind"], "similar")
        self.assertNotEqual(self._row("UCc")["kind"], "similar")

    def test_remove_many(self):
        self._post(ids=["UCa", "UCb"], bulk_action="remove")
        self.assertIsNone(self._row("UCa"))
        self.assertIsNone(self._row("UCb"))
        self.assertIsNotNone(self._row("UCc"))

    def test_backfill_and_views_run_each_ticked_channel(self):
        from unittest import mock
        from whisperradar import pipeline, webapp
        seen = {"history": [], "views": []}
        with mock.patch.object(pipeline, "import_history",
                               side_effect=lambda cfg, conn, ch, **k: seen["history"].append(ch["channel_id"]) or 2), \
             mock.patch.object(pipeline, "refresh_view_counts",
                               side_effect=lambda cfg, conn, ch, **k: seen["views"].append(ch["channel_id"]) or 3):
            app = create_app(self.cfg)
            client = app.test_client()
            r = client.post("/channels/bulk", data={"ids": ["UCa", "UCb"], "bulk_action": "backfill"})
            self.assertIn("msg=", r.headers["Location"])
            self._wait_idle(client)
            r = client.post("/channels/bulk", data={"ids": ["UCb", "UCc"], "bulk_action": "views"})
            self.assertIn("msg=", r.headers["Location"])
            self._wait_idle(client)
        self.assertEqual(seen["history"], ["UCa", "UCb"])
        self.assertEqual(seen["views"], ["UCb", "UCc"])

    @staticmethod
    def _wait_idle(client, timeout=10):
        import time
        end = time.time() + timeout
        while time.time() < end:
            if not client.get("/status").get_json()["running"]:
                return
            time.sleep(0.05)
        raise AssertionError("job did not finish")

    def test_invalid_requests_change_nothing(self):
        r = self._post(bulk_action="pause")  # nothing ticked
        self.assertIn("error=", r.headers["Location"])
        r = self._post(ids=["UCa"], bulk_action="genre", bulk_value="")
        self.assertIn("error=", r.headers["Location"])
        r = self._post(ids=["UCa"], bulk_action="kind", bulk_value="weird")
        self.assertIn("error=", r.headers["Location"])
        r = self._post(ids=["UCa"], bulk_action="explode")
        self.assertIn("error=", r.headers["Location"])
        self.assertEqual(self._row("UCa")["active"], 1)
        self.assertEqual(self._row("UCa")["genre"], "tech")

    def test_paused_view_redirect_keeps_state(self):
        r = self._post(ids=["UCa"], bulk_action="activate", state="paused")
        self.assertTrue(r.headers["Location"].startswith("/watched?state=paused&msg="))


if __name__ == "__main__":
    unittest.main()
