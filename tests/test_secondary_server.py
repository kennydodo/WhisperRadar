"""A second server on the same data: same everything, another port, and only the
first one runs the scheduler."""
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class SecondaryServerTests(unittest.TestCase):
    def setUp(self):
        from tests import wr_tmp
        from whisperradar.config import load_config
        self._wr_tmp = wr_tmp
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(__file__).resolve().parents[1]
        self.cfg = load_config(root / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.addCleanup(lambda: os.environ.pop("WR_SECONDARY", None))

    def tearDown(self):
        self._wr_tmp.cleanup(self.tmp)

    def make_app(self):
        from whisperradar.webapp import create_app
        with mock.patch("whisperradar.scheduler.Scheduler.start") as start:
            create_app(self.cfg)
        return start

    def test_the_first_server_starts_the_scheduler(self):
        os.environ.pop("WR_SECONDARY", None)
        self.make_app().assert_called_once()

    def test_a_secondary_server_does_not(self):
        os.environ["WR_SECONDARY"] = "1"
        self.make_app().assert_not_called()

    def test_serve_secondary_sets_the_switch_and_uses_the_given_port(self):
        from whisperradar import cli
        os.environ.pop("WR_SECONDARY", None)
        args = Namespace(secondary=True, remote=False, no_reload=False, host="127.0.0.1", port=8541)
        with mock.patch("flask.Flask.run") as run:
            cli.cmd_serve(self.cfg, args)
        self.assertEqual(os.environ.get("WR_SECONDARY"), "1")
        self.assertEqual(run.call_args.kwargs["port"], 8541)

    def test_serve_without_the_flag_leaves_the_switch_off(self):
        from whisperradar import cli
        os.environ.pop("WR_SECONDARY", None)
        args = Namespace(secondary=False, remote=False, no_reload=False, host="127.0.0.1", port=8540)
        with mock.patch("flask.Flask.run"):
            cli.cmd_serve(self.cfg, args)
        self.assertNotIn("WR_SECONDARY", os.environ)

    def test_the_database_waits_longer_for_a_lock(self):
        import inspect
        from whisperradar import db
        self.assertGreaterEqual(inspect.signature(db.connect).parameters["timeout"].default, 30)

    def test_the_launcher_uses_another_port_and_the_flag(self):
        text = (Path(__file__).resolve().parents[1] / "start_dashboard_2.cmd").read_text(encoding="utf-8")
        self.assertIn('set "PORT=8541"', text)
        self.assertIn("--secondary", text)


if __name__ == "__main__":
    unittest.main()
