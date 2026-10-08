"""Manual "Run refs stage" runs ONLY refs; it must not roll on into image
generation. Auto Run (run_pipeline) still chains refs -> images."""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402,F401

from whisperradar import autorun, db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class ManualRefsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = t / "wr.db"
        self.cfg.studio_dir = t / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.pid = db.create_production(conn, "T")
        conn.close()
        self.pdir = studio.prepare_project_folder(self.cfg, self.pid)
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_manual_refs_runs_only_the_refs_stage(self):
        (self.pdir / "shotlist.json").write_text("{}", encoding="utf-8")
        calls = []
        with mock.patch.object(
                autorun, "run_stage_and_advance",
                side_effect=lambda cfg, pid, stage, params=None:
                calls.append(stage) or "ok"), \
             mock.patch.object(autorun, "run_pipeline") as pipeline:
            r = self.client.post(f"/studio/{self.pid}/refs/run")
            time.sleep(0.5)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(calls, ["refs"])
        pipeline.assert_not_called()

    def test_needs_a_shotlist(self):
        r = self.client.post(f"/studio/{self.pid}/refs/run")
        self.assertIn("shotlist", r.headers["Location"].lower())

    def test_refs_button_posts_to_the_refs_route_not_auto_run(self):
        (self.pdir / "shotlist.json").write_text(
            '{"refs": {"hero": "refs/hero.png"}, "refPrompts": '
            '{"hero": "a hero"}, "images": [{"asset": "a_ST.png", '
            '"refs": ["hero"]}]}', encoding="utf-8")
        html = self.client.get(
            f"/studio/{self.pid}?stage=refs").get_data(as_text=True)
        self.assertIn(f"/studio/{self.pid}/refs/run", html)

    def test_auto_run_still_has_refs_before_images(self):
        stages = autorun.RUN_STAGES
        self.assertLess(stages.index("refs"), stages.index("images"))


if __name__ == "__main__":
    unittest.main()
