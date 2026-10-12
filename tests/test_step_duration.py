"""History steps record how long they took (db.add_step / start_step_clock)."""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402,F401

from whisperradar import db  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class StepDuration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        self.pid = db.create_production(self.conn, "P", "general", None, None)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_no_clock_means_no_duration(self):
        def t():
            conn = db.connect(self.cfg.db_path)
            db.add_step(conn, self.pid, "script", "manual")
            conn.close()
        th = threading.Thread(target=t)
        th.start()
        th.join()
        self.assertIsNone(db.step_history(self.conn, self.pid)[0]["duration_seconds"])

    def test_steps_measure_since_job_start_and_previous_step(self):
        def job():
            conn = db.connect(self.cfg.db_path)
            db.start_step_clock()
            time.sleep(0.2)
            db.add_step(conn, self.pid, "script", "auto")
            time.sleep(0.3)
            db.add_step(conn, self.pid, "audio", "auto")
            conn.close()
        th = threading.Thread(target=job)
        th.start()
        th.join()
        rows = {r["stage"]: r["duration_seconds"] for r in db.step_history(self.conn, self.pid)}
        self.assertAlmostEqual(rows["script"], 0.2, delta=0.15)
        self.assertAlmostEqual(rows["audio"], 0.3, delta=0.15)

    def test_history_shows_took(self):
        self.conn.execute(
            "INSERT INTO production_steps (production_id, stage, method, status,"
            " detail, duration_seconds) VALUES (?, 'merge', 'auto', 'done', 'x', 484)",
            (self.pid,))
        self.conn.commit()
        page = create_app(self.cfg).test_client().get(f"/studio/{self.pid}").data.decode()
        self.assertIn("took 8m 04s", page)


if __name__ == "__main__":
    unittest.main()
