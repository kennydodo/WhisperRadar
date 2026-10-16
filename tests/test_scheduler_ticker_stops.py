"""The scheduler's ticker thread ends when the database folder disappears."""
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import scheduler  # noqa: E402


class TickerStops(unittest.TestCase):
    def test_loop_returns_when_the_database_folder_is_gone(self):
        tmp = tempfile.mkdtemp()
        cfg = SimpleNamespace(db_path=Path(tmp) / "wr.db")
        sched = scheduler.Scheduler(cfg, SimpleNamespace(running=False),
                                    lambda *a: None)
        shutil.rmtree(tmp)
        with mock.patch.object(scheduler, "TICK_SECONDS", 0.01), \
                mock.patch.object(sched, "tick",
                                  side_effect=AssertionError("must not tick")):
            t = threading.Thread(target=sched._loop, daemon=True)
            t.start()
            t.join(5)
            self.assertFalse(t.is_alive())

    def test_loop_keeps_ticking_while_the_folder_exists(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cfg = SimpleNamespace(db_path=Path(tmp) / "wr.db")
        sched = scheduler.Scheduler(cfg, SimpleNamespace(running=False),
                                    lambda *a: None)
        ticked = threading.Event()

        def tick():
            ticked.set()
            sched.stop()
            return {"action": "off"}

        with mock.patch.object(scheduler, "TICK_SECONDS", 0.01), \
                mock.patch.object(sched, "tick", side_effect=tick):
            t = threading.Thread(target=sched._loop, daemon=True)
            t.start()
            t.join(5)
        self.assertTrue(ticked.is_set())


if __name__ == "__main__":
    unittest.main()
