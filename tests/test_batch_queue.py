"""Manual batch queue: run queued productions to merge, optional shutdown.

autorun.run_pipeline and subprocess.Popen are mocked so no production runs and
no real shutdown is scheduled.

Run: python -m unittest discover -s tests
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import autorun, db  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class BatchQueueTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(tempfile.mkdtemp()) / "wr.db"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.p1 = db.create_production(conn, "First", "general")
        self.p2 = db.create_production(conn, "Second", "general")
        conn.close()
        self.app = create_app(self.cfg)
        self.app.testing = True
        self.client = self.app.test_client()

    def _wait_idle(self, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if not self.client.get("/studio/job").get_json()["running"]:
                return True
            time.sleep(0.05)
        return False

    def test_queue_add_dedupe_and_remove(self):
        for _ in range(2):  # adding twice keeps one entry
            self.client.post("/studio/batch/queue", data={"pid": self.p1})
        page = self.client.get("/studio").get_data(as_text=True)
        self.assertIn("First (", page)
        self.assertEqual(page.count("First ("), 1)
        self.client.post("/studio/batch/unqueue", data={"pid": self.p1})
        page = self.client.get("/studio").get_data(as_text=True)
        self.assertNotIn("First (", page)

    def test_clear_queue(self):
        self.client.post("/studio/batch/queue", data={"pid": self.p1})
        self.client.post("/studio/batch/clear")
        page = self.client.get("/studio").get_data(as_text=True)
        self.assertNotIn("First (", page)

    def test_start_with_empty_queue_is_rejected(self):
        resp = self.client.post("/studio/batch/start", follow_redirects=False)
        self.assertIn("error=", resp.headers["Location"])
        self.assertIn("empty", resp.headers["Location"].lower())

    def test_batch_runs_each_job_to_merge_then_shuts_down(self):
        calls = []

        def fake_pipeline(cfg, pid, job=None, log=None, stop_before=None):
            calls.append(pid)
            return "ok"

        self.client.post("/studio/batch/queue", data={"pid": self.p1})
        self.client.post("/studio/batch/queue", data={"pid": self.p2})
        with mock.patch.object(autorun, "run_pipeline", fake_pipeline), \
                mock.patch("whisperradar.webapp.subprocess.Popen") as popen:
            resp = self.client.post("/studio/batch/start",
                                    data={"shutdown": "on"})
            self.assertIn("msg=", resp.headers["Location"])
            self.assertTrue(self._wait_idle())
            self.assertEqual(calls, [self.p1, self.p2])
            self.assertTrue(popen.called)
            argv = popen.call_args[0][0]
            self.assertIn("shutdown", argv[0])
        # the queue is drained once the batch starts
        page = self.client.get("/studio").get_data(as_text=True)
        self.assertIn("The queue is empty", page)

    def test_stop_flag_skips_shutdown(self):
        seen = []

        def fake_pipeline(cfg, pid, job=None, log=None, stop_before=None):
            seen.append(pid)
            if job is not None:
                job.cancel = True  # simulate the user stopping the batch
            return "stopped"

        self.client.post("/studio/batch/queue", data={"pid": self.p1})
        self.client.post("/studio/batch/queue", data={"pid": self.p2})
        with mock.patch.object(autorun, "run_pipeline", fake_pipeline), \
                mock.patch("whisperradar.webapp.subprocess.Popen") as popen:
            self.client.post("/studio/batch/start", data={"shutdown": "on"})
            self.assertTrue(self._wait_idle())
            self.assertFalse(popen.called)  # a stopped batch must not power off
        self.assertEqual(seen, [self.p1])


if __name__ == "__main__":
    unittest.main()
