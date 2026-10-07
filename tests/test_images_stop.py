"""Stage 7 (images) has a Kill button for a running image job.

Run: python -m unittest tests.test_images_stop
"""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import db  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


def _closure(fn, name):
    return fn.__closure__[fn.__code__.co_freevars.index(name)].cell_contents


class ImagesStopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(wr_tmp.cleanup, self.tmp)
        cfg = load_config(ROOT / "config.yaml")
        cfg.db_path = Path(self.tmp.name) / "wr.db"
        cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        chan = db.create_own_channel(conn, "Chan")
        self.pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, self.pid, own_channel_id=chan)
        conn.close()
        self.app = create_app(cfg)
        self.client = self.app.test_client()
        self.sjob = _closure(self.app.view_functions["studio_images_stop"],
                             "sjob")

    def _page(self):
        return self.client.get(f"/studio/{self.pid}?stage=images").get_data(
            as_text=True)

    def _run_job(self, kind):
        """Start a job in the production's own channel slot (the slot the
        page and the stop route resolve to)."""
        seen = {}
        release = threading.Event()
        with self.app.test_request_context(f"/studio/{self.pid}"):
            self.slot = self.sjob._real()

        def worker():
            while not self.slot.cancel and not release.wait(0.02):
                pass
            seen["cancelled"] = self.slot.cancel

        self.assertTrue(self.slot.start(worker, kind))
        self.addCleanup(release.set)
        return seen, release

    def test_no_button_and_a_clear_error_when_nothing_runs(self):
        self.assertNotIn("Kill image generation", self._page())
        r = self.client.post(f"/studio/{self.pid}/images/stop")
        self.assertEqual(r.status_code, 302)
        self.assertIn("error=", r.headers["Location"])

    def test_button_shows_for_a_render_and_stop_sets_the_cancel_flag(self):
        seen, release = self._run_job("image rendering (Renderly API + FlowBatch)")
        self.assertIn("Kill image generation", self._page())
        r = self.client.post(f"/studio/{self.pid}/images/stop")
        self.assertIn("msg=", r.headers["Location"])
        for _ in range(100):
            if "cancelled" in seen:
                break
            time.sleep(0.02)
        self.assertTrue(seen.get("cancelled"))

    def test_other_jobs_are_not_stoppable_from_here(self):
        self._run_job("shotlist planning")
        self.assertNotIn("Kill image generation", self._page())
        r = self.client.post(f"/studio/{self.pid}/images/stop")
        self.assertIn("error=", r.headers["Location"])
        self.assertFalse(self.slot.cancel)


if __name__ == "__main__":
    unittest.main()
