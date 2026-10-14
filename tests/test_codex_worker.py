"""Portable runtime dispatch; all Codex inference is mocked."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from whisperradar import codex_worker as worker


class CodexWorkerTests(unittest.TestCase):
    def test_missing_runtime_has_actionable_error(self):
        with patch.dict(os.environ, {"WR_CODEX_EXE": "/missing/codex.exe"}):
            with self.assertRaisesRegex(ValueError, "installed Codex"):
                worker.discover()

    def test_path_detection(self):
        with patch.dict(os.environ, {"WR_CODEX_EXE": ""}), patch.object(
                worker.shutil, "which", return_value="/installed/codex"):
            self.assertEqual(worker.discover(), "/installed/codex")

    def test_preflight_requires_subscription_and_images(self):
        good = SimpleNamespace(returncode=0, stdout="Logged in using ChatGPT", stderr="")
        disabled = SimpleNamespace(returncode=0, stdout="image_generation stable false", stderr="")
        with patch.object(worker.subprocess, "run", side_effect=[good, disabled]):
            with self.assertRaisesRegex(ValueError, "image generation"):
                worker.preflight("codex")
        key = SimpleNamespace(returncode=0, stdout="Logged in using API key", stderr="")
        with patch.object(worker.subprocess, "run", return_value=key):
            with self.assertRaisesRegex(ValueError, "API-key"):
                worker.preflight("codex")

    def test_result_passes_to_existing_validator(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = SimpleNamespace(db_path=Path(folder) / "db")
            job = {"id": "job1", "claim_token": "private", "context": {"script": "source"}}
            result = Path(folder) / "codex_packaging/job1/result.json"
            process = Mock(returncode=0)
            process.communicate.side_effect = lambda *a, **k: result.write_text(
                json.dumps({"title": "result"}), encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_HOME": folder}), patch.object(
                    worker.subprocess, "Popen", return_value=process) as popen, patch.object(
                    worker.gpt_packaging, "complete", return_value="saved") as complete:
                self.assertEqual(worker.run_job(cfg, job, "codex"), "saved")
                complete.assert_called_once_with(cfg, "job1", "private", {"title": "result"})
                args = popen.call_args.args[0]
                self.assertIn("workspace-write", args)
                self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)
                self.assertNotIn("shell", popen.call_args.kwargs)

    def test_failure_releases_worker_and_marks_job(self):
        job = {"id": "j", "claim_token": "private"}
        worker._RUNNING.add("test")
        with patch.object(worker.gpt_packaging, "claim", side_effect=[job, None]), patch.object(
                worker, "run_job", side_effect=ValueError("tool unavailable")), patch.object(
                worker.gpt_packaging, "fail") as fail:
            worker._drain(None, "codex", "test")
            fail.assert_called_once_with(None, "j", "private", "tool unavailable")
        self.assertNotIn("test", worker._RUNNING)
