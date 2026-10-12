"""The Motion clips stage page and its manual run button."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import autorun, db, studio  # noqa: E402
from tests.test_refs_manual import ManualRefsTests  # noqa: E402


class MotionPanelTests(ManualRefsTests):
    def _page(self):
        return self.client.get(
            f"/studio/{self.pid}?stage=motion").get_data(as_text=True)

    def test_manual_run_runs_only_the_motion_stage(self):
        (self.pdir / "shotlist.json").write_text("{}", encoding="utf-8")
        calls = []
        with mock.patch.object(
                autorun, "run_stage_and_advance",
                side_effect=lambda cfg, pid, stage, params=None:
                calls.append(stage) or "ok"), \
             mock.patch.object(autorun, "run_pipeline") as pipeline:
            r = self.client.post(f"/studio/{self.pid}/motion/run")
            time.sleep(0.5)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(calls, ["motion"])
        pipeline.assert_not_called()

    def test_run_needs_a_shotlist(self):
        r = self.client.post(f"/studio/{self.pid}/motion/run")
        self.assertIn("shotlist", r.headers["Location"].lower())

    def test_page_says_off_when_the_channel_has_it_off(self):
        html = self._page()
        self.assertIn("animated opening is off", html)
        self.assertNotIn("Generate motion clips", html)

    def test_page_lists_clips_and_shows_the_button_when_on(self):
        summary = {"enabled": True, "seconds": 30, "model": "Veo 3.1 - Lite",
                   "clips": [{"asset": "S01_01_SCN_ZO.png", "image": True,
                              "done": False, "prompt": "slow pull back"}]}
        with mock.patch.object(autorun, "motion_summary", return_value=summary):
            html = self._page()
        self.assertIn("S01_01_SCN_ZO.png", html)
        self.assertIn("Generate motion clips", html)
        self.assertIn(f"/studio/{self.pid}/motion/run", html)


if __name__ == "__main__":
    unittest.main()
