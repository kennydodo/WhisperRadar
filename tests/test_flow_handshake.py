"""The Flow prepare handshake and project-URL precedence.

These are the parts that broke today (a dead/foreign project producing an empty
handshake, and the channel URL overriding the production row). They run without
a network or a real Flow project.

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

from whisperradar.config import load_config  # noqa: E402
from whisperradar import db, studio  # noqa: E402

PROJECT = "https://flow.google.com/project/11111111-2222-3333-4444-555555555555"
DEAD = "https://flow.google.com/404?reason=project"


def _cfg(db_path, global_url=None):
    cfg = load_config(ROOT / "config.yaml")
    cfg.db_path = Path(db_path)
    cfg.flowbatch_project_url = global_url
    return cfg


class UsableProjectTests(unittest.TestCase):
    """What counts as a real handshake."""

    def test_a_project_url_is_usable(self):
        self.assertEqual(studio._usable_flow_project({"projectUrl": PROJECT}), PROJECT)

    def test_missing_or_dead_or_placeholder_is_not(self):
        for report in (
            None,
            {},
            {"projectUrl": ""},
            {"projectUrl": "   "},
            {"projectUrl": DEAD},  # deleted / other-account project
            {"projectUrl": "https://flow.google.com/asb/placeholder"},
        ):
            self.assertEqual(studio._usable_flow_project(report), "", repr(report))


class StripJobTests(unittest.TestCase):
    """Clearing a job's stored project is what makes prepare CREATE a new one."""

    def test_strips_only_the_project_url(self):
        with tempfile.TemporaryDirectory() as d:
            job = Path(d) / "job.json"
            job.write_text(json.dumps({"name": "wr-9", "projectUrl": "http://x",
                                       "images": [{"file": "a.png"}],
                                       "refs": {"MAYA": "refs/MAYA.jpeg"}}),
                           encoding="utf-8")
            studio._strip_job_project_url(job)
            data = json.loads(job.read_text(encoding="utf-8"))
            self.assertNotIn("projectUrl", data)
            self.assertEqual(data["name"], "wr-9")
            self.assertEqual(data["images"], [{"file": "a.png"}])
            self.assertEqual(data["refs"], {"MAYA": "refs/MAYA.jpeg"})

    def test_a_missing_file_is_safe(self):
        studio._strip_job_project_url(Path("definitely-not-here.json"))


class ProjectPrecedenceTests(unittest.TestCase):
    """production row -> channel -> global config."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg(Path(self.tmp.name) / "wr.db", global_url="http://global")
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.oc = db.create_own_channel(conn, "Ch")
        self.pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, self.pid, own_channel_id=self.oc)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _set(self, sql, *args):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        conn.execute(sql, args)
        conn.commit()
        conn.close()

    def test_channel_is_used_when_the_production_has_none(self):
        self._set("update own_channels set flow_project_url=? where id=?",
                  "http://channel", self.oc)
        self.assertEqual(studio.flow_project_url_for(self.cfg, self.pid),
                         ("http://channel", "channel"))

    def test_the_production_row_beats_the_channel(self):
        self._set("update own_channels set flow_project_url=? where id=?",
                  "http://channel", self.oc)
        self._set("update productions set flow_project_url=? where id=?",
                  "http://prod", self.pid)
        self.assertEqual(studio.flow_project_url_for(self.cfg, self.pid),
                         ("http://prod", "production"))

    def test_an_explicit_override_beats_everything(self):
        self._set("update productions set flow_project_url=? where id=?",
                  "http://prod", self.pid)
        self.assertEqual(studio.flow_project_url_for(self.cfg, self.pid, "http://override"),
                         ("http://override", "explicit"))

    def test_the_global_config_is_the_last_resort(self):
        self.assertEqual(studio.flow_project_url_for(self.cfg, self.pid),
                         ("http://global", "config"))

    def test_none_when_nothing_is_set(self):
        self.cfg.flowbatch_project_url = None
        self.assertEqual(studio.flow_project_url_for(self.cfg, self.pid), (None, "none"))


class ImagesPrecedenceGuard(unittest.TestCase):
    """The bug that fed prepare a dead channel URL: the images stage must let
    flow_project_url_for resolve production -> channel -> global itself."""

    def test_run_images_does_not_override_with_the_channel_url(self):
        source = (ROOT / "whisperradar" / "autorun.py").read_text(encoding="utf-8")
        self.assertNotIn('flow_project_url or eff["flow_project_url"]', source,
                         "the channel URL must not override the production row")


class FlowBatchPrepareTests(unittest.TestCase):
    """prepare writes its report BEFORE the ref work, so a failed ref step still
    leaves a usable project - generation then uploads the refs itself."""

    def test_a_failed_ref_step_keeps_a_usable_project(self):
        class _Proc:
            returncode = 1
            stdout = "boom while uploading references"
            stderr = ""

        with tempfile.TemporaryDirectory() as d:
            pid_dir = Path(d)
            (pid_dir / studio.FLOW_PREPARE_REPORT).write_text(
                json.dumps({"projectUrl": PROJECT, "refs": []}), encoding="utf-8")
            cfg = _cfg(Path(d) / "wr.db")
            cfg.flowbatch_repo = d  # exists, so flowbatch_dir() finds it
            with mock.patch.object(studio, "_flowbatch_cmd",
                                   lambda args: ["node", "cli.js"]), \
                    mock.patch.object(studio.subprocess, "run",
                                      lambda *a, **k: _Proc()):
                report = studio.run_flowbatch_prepare(
                    cfg, pid_dir, pid_dir / "job.json", log=lambda m: None)
        self.assertEqual(report.get("projectUrl"), PROJECT)

    def test_a_failure_with_no_project_is_an_error(self):
        class _Proc:
            returncode = 1
            stdout = "prompt box not found"
            stderr = ""

        with tempfile.TemporaryDirectory() as d:
            pid_dir = Path(d)
            cfg = _cfg(Path(d) / "wr.db")
            cfg.flowbatch_repo = d
            with mock.patch.object(studio, "_flowbatch_cmd",
                                   lambda args: ["node", "cli.js"]), \
                    mock.patch.object(studio.subprocess, "run",
                                      lambda *a, **k: _Proc()):
                report = studio.run_flowbatch_prepare(
                    cfg, pid_dir, pid_dir / "job.json", log=lambda m: None)
        self.assertIn("error", report)


class RequireNewProjectTests(unittest.TestCase):
    """No stored project -> the batch must CREATE one; opening a previous
    project instead is a hard failure, not a silent fallback."""

    def test_a_created_project_passes(self):
        report = {"projectUrl": PROJECT, "created": True}
        self.assertEqual(studio._require_flow_project(report, had_stored=False),
                         PROJECT)

    def test_an_opened_project_is_refused_when_none_was_stored(self):
        report = {"projectUrl": PROJECT, "created": False}
        with self.assertRaises(RuntimeError) as ctx:
            studio._require_flow_project(report, had_stored=False)
        self.assertIn("NEW Flow project", str(ctx.exception))

    def test_an_opened_project_is_fine_when_one_was_stored(self):
        report = {"projectUrl": PROJECT, "created": False}
        self.assertEqual(studio._require_flow_project(report, had_stored=True),
                         PROJECT)

    def test_no_usable_project_at_all_is_an_error(self):
        for report in (None, {}, {"error": "prepare failed"},
                       {"projectUrl": DEAD, "created": True}):
            with self.assertRaises(RuntimeError):
                studio._require_flow_project(report, had_stored=False)


class PrepareNewProjectFlagTests(unittest.TestCase):
    """run_flowbatch_prepare(new_project=True) must pass --new-project through
    to the FlowBatch CLI."""

    def test_new_project_adds_the_flag(self):
        seen = {}

        class _Proc:
            returncode = 1
            stdout = "boom"
            stderr = ""

        def fake_cmd(args):
            seen["args"] = list(args)
            return ["node", "cli.js"]

        with tempfile.TemporaryDirectory() as d:
            pid_dir = Path(d)
            cfg = _cfg(Path(d) / "wr.db")
            cfg.flowbatch_repo = d
            with mock.patch.object(studio, "_flowbatch_cmd", fake_cmd), \
                    mock.patch.object(studio.subprocess, "run",
                                      lambda *a, **k: _Proc()):
                studio.run_flowbatch_prepare(cfg, pid_dir, pid_dir / "job.json",
                                             log=lambda m: None, new_project=True)
        self.assertIn("--new-project", seen["args"])


class ImagesStageRequiresNewProject(unittest.TestCase):
    """The images stage must fail when FlowBatch opened a previous project
    instead of creating the new one it was told to create."""

    def test_no_stored_project_requires_creation(self):
        with tempfile.TemporaryDirectory() as d:
            pid_dir = Path(d)
            cfg = _cfg(Path(d) / "wr.db")
            cfg.flowbatch_repo = d
            job = pid_dir / "flowbatch.json"
            job.write_text("{}", encoding="utf-8")
            with mock.patch.object(studio, "flowbatch_ready", lambda c: True), \
                    mock.patch.object(studio, "flowbatch_dir", lambda c: Path(d)), \
                    mock.patch.object(studio, "prepare_flowbatch_job",
                                      lambda *a, **k: (job, ["a.png"])), \
                    mock.patch.object(studio, "flow_project_url_for",
                                      lambda *a, **k: (None, "none")), \
                    mock.patch.object(studio, "run_flowbatch_prepare",
                                      lambda *a, **k: {"projectUrl": PROJECT,
                                                       "created": False}):
                with self.assertRaises(RuntimeError) as ctx:
                    studio.run_imagegen_flowbatch(cfg, pid_dir, 999)
            self.assertIn("NEW Flow project", str(ctx.exception))


class NoStoredProjectWarningTests(unittest.TestCase):
    """prepare_flowbatch_job clears a production's old warning when no Flow
    project is stored (it used to hit a NameError that was swallowed)."""

    def test_warning_is_cleared_without_a_stored_project(self):
        import json
        import tempfile
        from pathlib import Path
        from whisperradar import db, studio
        from whisperradar.config import load_config
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(Path(__file__).resolve().parents[1]
                              / "config.yaml")
            cfg.db_path = Path(d) / "wr.db"
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            pid = db.create_production(conn, "t")
            db.update_production(conn, pid, warning="old project warning")
            conn.close()
            pid_dir = Path(d) / "p"
            pid_dir.mkdir()
            (pid_dir / "shotlist.json").write_text(json.dumps(
                {"style": "s", "images": [
                    {"file": "A01_ZI.png", "prompt": "x"}]}),
                encoding="utf-8")
            studio.prepare_flowbatch_job(cfg, pid_dir, pid)
            conn = db.connect(cfg.db_path)
            try:
                prod = db.get_production(conn, pid)
                self.assertFalse(prod["warning"])
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
