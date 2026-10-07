"""Manual "Recover from Flow gallery" for the Studio IMAGES stage.

A batch that stopped on consecutive failures often left its already-generated
images sitting in the Flow project gallery, never downloaded - re-running the
prompts paid twice. Recovery is MANUAL only, dispatches by the production's
own engine (flowbatch, or renderly+flow -> the FlowBatch CLI's recover
command; renderly+api -> refused: no gallery), adopts
ONLY files still missing, and NEVER generates. These tests stub every
subprocess/HTTP call - no browser, no paid API, no network.

Run: python -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import autorun, db, services, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402

SHOTLIST = {
    "shots": [{"asset": "S01_01.png", "cues": "1-1"}],
    "images": [{"file": "S01_01.png", "prompt": "a red fox"},
               {"file": "S02_01.png", "prompt": "an owl"}],
    "refs": {},
}


class FlowbatchRecoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.pdir = Path(self.tmp.name) / "prod"
        (self.pdir / "images").mkdir(parents=True)
        (self.pdir / "images" / "S02_01.png").write_bytes(b"have")
        (self.pdir / "shotlist.json").write_text(
            json.dumps(SHOTLIST), encoding="utf-8")
        self.repo = Path(self.tmp.name) / "flowbatch"
        self.repo.mkdir()

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def _run(self, stream_result, files_to_write=()):
        cmds = {}

        def fake_stream(cmd, repo, log, cancel):
            cmds["cmd"] = cmd
            for name in files_to_write:
                (self.pdir / "flow_images").mkdir(exist_ok=True)
                (self.pdir / "flow_images" / name).write_bytes(b"recovered")
            return stream_result

        with mock.patch.object(studio, "flowbatch_ready", lambda c: True), \
                mock.patch.object(studio, "flowbatch_dir",
                                  lambda c: self.repo), \
                mock.patch.object(studio, "flow_project_url_for",
                                  lambda c, p, o=None: ("https://flow.google.com/project/x", "production")), \
                mock.patch.object(studio, "prepare_flowbatch_job",
                                  return_value=(self.pdir / "flowbatch.json",
                                                ["S01_01.png"])), \
                mock.patch.object(studio, "set_flowbatch_tier",
                                  return_value="off"), \
                mock.patch.object(studio, "_flowbatch_stream", fake_stream):
            result = studio.run_flowbatch_recover(self.cfg, self.pdir, 42)
        return result, cmds

    def test_recovered_files_are_adopted_into_images(self):
        result, cmds = self._run((0, ["done"]), ["S01_01.png"])
        self.assertEqual(result, {"recovered": ["S01_01.png"],
                                  "still_missing": []})
        self.assertTrue((self.pdir / "images" / "S01_01.png").is_file())
        cmd = " ".join(cmds["cmd"])
        self.assertIn("recover --job", cmd)
        self.assertIn("--project-url https://flow.google.com/project/x", cmd)
        self.assertNotIn("generate", cmds["cmd"])

    def test_nothing_recovered_reports_the_gap(self):
        result, _ = self._run((0, ["no tiles matched"]))
        self.assertEqual(result, {"recovered": [],
                                  "still_missing": ["S01_01.png"]})
        self.assertFalse((self.pdir / "images" / "S01_01.png").exists())

    def test_old_flowbatch_without_recover_is_said_plainly(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run((1, ['Unknown command "recover". Available: generate']))
        self.assertIn("no 'recover' command", str(ctx.exception))

    def test_without_a_project_url_recovery_refuses(self):
        """Reading Flow's MOST RECENT gallery could adopt another video's
        images under these names - no stored URL must stop before the CLI."""
        with mock.patch.object(studio, "flowbatch_ready", lambda c: True), \
                mock.patch.object(studio, "flowbatch_dir",
                                  lambda c: self.repo), \
                mock.patch.object(studio, "flow_project_url_for",
                                  lambda c, p, o=None: (None, "none")), \
                mock.patch.object(studio, "_flowbatch_stream") as stream:
            with self.assertRaises(RuntimeError) as ctx:
                studio.run_flowbatch_recover(self.cfg, self.pdir, 42)
        self.assertIn("Flow project URL", str(ctx.exception))
        stream.assert_not_called()

    def test_complete_shotlist_never_opens_the_gallery(self):
        (self.pdir / "images" / "S01_01.png").write_bytes(b"have")
        with mock.patch.object(studio, "flowbatch_ready", lambda c: True), \
                mock.patch.object(studio, "_flowbatch_stream") as stream:
            result = studio.run_flowbatch_recover(self.cfg, self.pdir, 42)
        self.assertEqual(result, {"recovered": [], "still_missing": []})
        stream.assert_not_called()


class RecoverDispatchTests(unittest.TestCase):
    """recover_images() must honor the SAME engine/mode the images stage
    would use, record an honest step, and touch no other engine."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        chan = db.create_own_channel(conn, "Ch")
        self.pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, self.pid, own_channel_id=chan)
        conn.commit()
        conn.close()
        pdir = studio.prod_dir(self.cfg, self.pid)
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "shotlist.json").write_text(json.dumps(SHOTLIST),
                                            encoding="utf-8")

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def _set(self, **kw):
        conn = db.connect(self.cfg.db_path)
        for key, value in kw.items():
            if key == "default_engine":
                db.set_setting(conn, key, value)
            else:
                db.update_production(conn, self.pid, **{key: value})
        conn.commit()
        conn.close()

    def _steps(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        try:
            return [s for s in db.step_history(conn, self.pid)
                    if s["stage"] == "images"]
        finally:
            conn.close()

    def test_flowbatch_engine_recovers_via_flowbatch(self):
        self._set(default_engine="flowbatch")
        with mock.patch.object(studio, "run_flowbatch_recover",
                               return_value={"recovered": ["S01_01.png"],
                                             "still_missing": []}) as fig, \
                mock.patch.object(services.MANAGER, "ensure"), \
                mock.patch.object(services.MANAGER, "release"):
            result = autorun.recover_images(self.cfg, self.pid)
        self.assertEqual(result["recovered"], ["S01_01.png"])
        fig.assert_called_once()
        steps = self._steps()
        self.assertEqual(steps[0]["status"], "done")
        self.assertIn("gallery recovery via FlowBatch", steps[0]["detail"])

    def test_renderly_flow_mode_recovers_via_flowbatch(self):
        self._set(render_mode="flow")
        with mock.patch.object(studio, "run_flowbatch_recover",
                               return_value={"recovered": [],
                                             "still_missing": ["S01_01.png",
                                                               "S02_01.png"]}) as fig, \
                mock.patch.object(services.MANAGER, "ensure"), \
                mock.patch.object(services.MANAGER, "release"):
            autorun.recover_images(self.cfg, self.pid)
        fig.assert_called_once()
        steps = self._steps()
        # A partial recovery must NOT mark the images stage complete.
        self.assertEqual(steps[0]["status"], "failed")
        self.assertIn("2 still missing", steps[0]["detail"])

    def test_renderly_api_engine_is_refused(self):
        self._set(render_mode="api")
        with mock.patch.object(studio, "run_flowbatch_recover") as fig:
            with self.assertRaises(RuntimeError) as ctx:
                autorun.recover_images(self.cfg, self.pid)
        self.assertIn("no gallery to recover from", str(ctx.exception))
        fig.assert_not_called()


class MissingImagesHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdir = Path(self.tmp.name)
        (self.pdir / "images").mkdir()
        (self.pdir / "shotlist.json").write_text(json.dumps(SHOTLIST),
                                                 encoding="utf-8")

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def test_exact_name_missing_list(self):
        self.assertEqual(studio.shotlist_missing_images(self.pdir),
                         ["S01_01.png", "S02_01.png"])
        (self.pdir / "images" / "S01_01.png").write_bytes(b"x")
        self.assertEqual(studio.shotlist_missing_images(self.pdir),
                         ["S02_01.png"])
        (self.pdir / "images" / "S02_01.png").write_bytes(b"x")
        self.assertEqual(studio.shotlist_missing_images(self.pdir), [])


if __name__ == "__main__":
    unittest.main()
