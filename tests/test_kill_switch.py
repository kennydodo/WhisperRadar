"""The image kill switch: ONE mechanism (a process-tree kill) for every
engine, no retry.

What is spawned for a production (FlowBatch's node CLI, the Renderly API
ImageGen run) is registered under that production's folder; the kill route
sets the job's cancel flag FIRST, then tree-kills what is registered, and
a kill surfaces as BatchCancelled so the resume loop never pauses and
retries. No paid API, Flow or Renderly call is made: the children are plain
python sleepers.

Run: python -m unittest tests.test_kill_switch
"""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import autorun, db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

SLEEPER = [sys.executable, "-u", "-c",
           "import time; print('up', flush=True); time.sleep(120)"]


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                             capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # a zombie still answers signal 0
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return True


def _wait_dead(pid: int, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


class KillImageBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(wr_tmp.cleanup, self.tmp)
        self.dir_a = Path(self.tmp.name) / "a"
        self.dir_b = Path(self.tmp.name) / "b"
        self.dir_a.mkdir()
        self.dir_b.mkdir()
        self.procs = []
        self.addCleanup(self._reap)

    def _reap(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=10)
            if p.stdout:
                p.stdout.close()
            studio._release_tracked(p)

    def _spawn(self, scope_dir, label="FlowBatch"):
        with studio.batch_scope(scope_dir):
            proc = studio._popen_tracked(SLEEPER, label, stdout=subprocess.PIPE,
                                         text=True)
        self.procs.append(proc)
        self.assertEqual(proc.stdout.readline().strip(), "up")
        return proc

    def test_nothing_running_is_reported_honestly(self):
        report = studio.kill_image_batch(self.dir_a)
        self.assertTrue(report["nothing_running"])
        self.assertEqual(report["killed"], [])

    def test_kills_a_registered_process_and_names_it(self):
        proc = self._spawn(self.dir_a, "FlowBatch")
        report = studio.kill_image_batch(self.dir_a)
        self.assertEqual(report["killed"], ["FlowBatch"])
        self.assertFalse(report["nothing_running"])
        self.assertTrue(_wait_dead(proc.pid))
        # the runner learns it was a user kill (and the registry is clean)
        proc.wait(timeout=10)
        self.assertTrue(studio._release_tracked(proc))
        self.assertTrue(studio.kill_image_batch(self.dir_a)["nothing_running"])

    def test_another_productions_process_is_left_alone(self):
        mine = self._spawn(self.dir_a)
        other = self._spawn(self.dir_b)
        studio.kill_image_batch(self.dir_a)
        self.assertTrue(_wait_dead(mine.pid))
        self.assertTrue(_alive(other.pid))
        studio.kill_image_batch(self.dir_b)
        self.assertTrue(_wait_dead(other.pid))

    def test_the_whole_tree_dies_not_just_the_parent(self):
        """FlowBatch's node process owns Chrome: killing only the parent
        would orphan the browser."""
        code = ("import subprocess, sys, time;"
                "c = subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(120)']);"
                "print(c.pid, flush=True); time.sleep(120)")
        with studio.batch_scope(self.dir_a):
            parent = studio._popen_tracked([sys.executable, "-u", "-c", code],
                                           "FlowBatch", stdout=subprocess.PIPE,
                                           text=True)
        self.procs.append(parent)
        child_pid = int(parent.stdout.readline().strip())
        self.assertTrue(_alive(child_pid))
        studio.kill_image_batch(self.dir_a)
        self.assertTrue(_wait_dead(parent.pid))
        self.assertTrue(_wait_dead(child_pid),
                        "the child process survived the tree kill")

    def test_a_kill_during_a_flowbatch_stream_raises_batch_cancelled(self):
        result = {}

        def run():
            try:
                with studio.batch_scope(self.dir_a):
                    studio._flowbatch_stream(SLEEPER, self.dir_a,
                                             lambda m: None, None)
                result["outcome"] = "returned"
            except studio.BatchCancelled:
                result["outcome"] = "cancelled"
            except Exception as exc:  # noqa: BLE001
                result["outcome"] = f"other: {exc!r}"

        worker = threading.Thread(target=run)
        worker.start()
        end = time.time() + 15
        while time.time() < end and not studio.kill_image_batch(
                self.dir_a)["killed"]:
            time.sleep(0.1)
        worker.join(timeout=15)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result.get("outcome"), "cancelled")

    def test_a_kill_during_a_captured_run_raises_batch_cancelled(self):
        """The Renderly API ImageGen run (and FlowBatch prepare) go through
        _run_tracked, which used to be an uncancellable subprocess.run."""
        result = {}

        def run():
            try:
                with studio.batch_scope(self.dir_a):
                    studio._run_tracked(SLEEPER, "Renderly API (ImageGen)",
                                        timeout=60)
                result["outcome"] = "returned"
            except studio.BatchCancelled as exc:
                result["outcome"] = "cancelled"
                result["message"] = str(exc)

        worker = threading.Thread(target=run)
        worker.start()
        end = time.time() + 15
        while time.time() < end and not studio.kill_image_batch(
                self.dir_a)["killed"]:
            time.sleep(0.1)
        worker.join(timeout=15)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result.get("outcome"), "cancelled")
        self.assertIn("Renderly API", result.get("message", ""))

    def test_a_normal_exit_is_not_mistaken_for_a_kill(self):
        with studio.batch_scope(self.dir_a):
            done = studio._run_tracked(
                [sys.executable, "-c", "print('ok')"], "FlowBatch", timeout=30)
        self.assertEqual(done.returncode, 0)
        self.assertIn("ok", done.stdout)


class ResumeLoopTests(unittest.TestCase):
    """A kill must end the images stage, not pause for an hour and retry."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(wr_tmp.cleanup, self.tmp)
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.pid = db.create_production(conn, "P", "general", None, None)
        conn.close()
        pdir = studio.prepare_project_folder(self.cfg, self.pid)
        (pdir / "shotlist.json").write_text(
            '{"images": [{"file": "S01_01_SCN_ST.png", "prompt": "a cat"}],'
            ' "shots": []}', encoding="utf-8")

    def _run_images(self, boom):
        with mock.patch.object(studio, "run_imagegen_flowbatch",
                               side_effect=boom), \
             mock.patch.object(autorun.services.MANAGER, "ensure"), \
             mock.patch.object(autorun.services.MANAGER, "release"), \
             mock.patch.object(autorun, "_pause") as pause:
            try:
                autorun._run_images(self.cfg, self.pid, mode="flow",
                                    engine="flowbatch", log=lambda m: None)
            except Exception as exc:  # noqa: BLE001
                return exc, pause
            return None, pause

    def test_a_killed_batch_ends_the_stage_without_pausing_or_retrying(self):
        exc, pause = self._run_images(studio.BatchCancelled("killed"))
        self.assertIsInstance(exc, studio.BatchCancelled)
        pause.assert_not_called()

    def test_run_stage_reports_a_kill_as_stopped(self):
        with mock.patch.object(studio, "run_imagegen_flowbatch",
                               side_effect=studio.BatchCancelled("killed")), \
             mock.patch.object(autorun.services.MANAGER, "ensure"), \
             mock.patch.object(autorun.services.MANAGER, "release"), \
             mock.patch.object(autorun, "_pause") as pause:
            result = autorun.run_stage(
                self.cfg, self.pid, "images",
                {"mode": "flow", "engine": "flowbatch", "log": lambda m: None})
        self.assertEqual(result, "stopped")
        pause.assert_not_called()

    def test_a_real_flow_refusal_still_pauses_and_resumes(self):
        """The kill path must not have broken the normal resume behaviour."""
        refusal = RuntimeError("FlowBatch stopped - Flow is refusing this "
                               "session. It resumes after the configured wait.")
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise refusal
            return 0

        with mock.patch.object(studio, "run_imagegen_flowbatch",
                               side_effect=flaky), \
             mock.patch.object(autorun.services.MANAGER, "ensure"), \
             mock.patch.object(autorun.services.MANAGER, "release"), \
             mock.patch.object(studio, "image_batch_limits",
                               return_value=(0, False, 5, 600, 300)), \
             mock.patch.object(studio, "image_throttle_wait_seconds",
                               return_value=600), \
             mock.patch.object(studio, "run_flowbatch_recover",
                               return_value={"recovered": [],
                                             "still_missing": []}), \
             mock.patch.object(autorun, "_pause") as pause:
            autorun._run_images(self.cfg, self.pid, mode="flow",
                                engine="flowbatch", log=lambda m: None)
        pause.assert_called_once()
        self.assertEqual(calls["n"], 2)


class KillRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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
        self.cfg = cfg
        self.app = create_app(cfg)
        self.client = self.app.test_client()
        sjob = self.app.view_functions["studio_images_stop"]
        sjob = sjob.__closure__[sjob.__code__.co_freevars.index("sjob")
                                ].cell_contents
        with self.app.test_request_context(f"/studio/{self.pid}"):
            self.slot = sjob._real()

    def _start_job(self, kind="image rendering (FlowBatch)"):
        release = threading.Event()
        self.addCleanup(release.set)

        def worker():
            while not self.slot.cancel and not release.wait(0.02):
                pass

        self.assertTrue(self.slot.start(worker, kind))

    def test_sets_the_cancel_flag_before_killing_and_reports_what_died(self):
        self._start_job()
        order = []

        def fake_kill(pdir):
            order.append(("kill", self.slot.cancel))
            return {"killed": ["FlowBatch"], "nothing_running": False}

        with mock.patch.object(studio, "kill_image_batch", fake_kill):
            r = self.client.post(f"/studio/{self.pid}/images/stop")
        self.assertEqual(order, [("kill", True)],
                         "the cancel flag must be set BEFORE the kill so the "
                         "resume loop exits instead of retrying")
        msg = unquote(r.headers["Location"])
        self.assertIn("Killed: FlowBatch", msg)

    def test_says_so_when_there_was_nothing_to_kill(self):
        self._start_job()
        with mock.patch.object(studio, "kill_image_batch",
                               return_value={"killed": [],
                                             "nothing_running": True}):
            r = self.client.post(f"/studio/{self.pid}/images/stop")
        self.assertIn("no image process was running",
                      unquote(r.headers["Location"]))
        self.assertTrue(self.slot.cancel)

    def test_kills_a_real_registered_process_end_to_end(self):
        self._start_job()
        pdir = studio.prod_dir(self.cfg, self.pid)
        pdir.mkdir(parents=True, exist_ok=True)
        with studio.batch_scope(pdir):
            proc = studio._popen_tracked(SLEEPER, "FlowBatch",
                                         stdout=subprocess.PIPE, text=True)
        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            proc.stdout.close()
            studio._release_tracked(proc)

        self.addCleanup(cleanup)
        self.assertEqual(proc.stdout.readline().strip(), "up")
        r = self.client.post(f"/studio/{self.pid}/images/stop")
        self.assertIn("Killed: FlowBatch", unquote(r.headers["Location"]))
        self.assertTrue(_wait_dead(proc.pid))

    def test_the_manual_render_job_is_cancellable(self):
        """Render images used to pass no cancel callable at all."""
        import inspect
        src = inspect.getsource(self.app.view_functions["studio_images_render"])
        self.assertIn('"cancel"', src)


if __name__ == "__main__":
    unittest.main()
