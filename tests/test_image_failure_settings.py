"""The images consecutive-failure-stop threshold and the wait before an
auto-resume after it are now settings (images_max_consecutive_failures,
images_resume_wait_minutes) instead of a hardcoded constant, shared by
manual render and auto-run and by both engines (Flow Driver + FlowBatch).

Run: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar.config import load_config  # noqa: E402
from whisperradar import autorun, db, studio  # noqa: E402


class ImageBatchLimitsTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(tempfile.mkdtemp()) / "wr.db"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.pid = db.create_production(conn, "T")
        conn.commit()
        conn.close()

    def test_defaults_match_the_settings_spec(self):
        chunk, stop_on_failure, max_consecutive, resume_wait = (
            studio.image_batch_limits(self.cfg, self.pid))
        self.assertEqual(max_consecutive, 3)
        self.assertEqual(resume_wait, 600)  # 10 minutes, in seconds

    def test_a_saved_setting_is_picked_up(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.set_setting(conn, "images_max_consecutive_failures", "7")
        db.set_setting(conn, "images_resume_wait_minutes", "0")
        conn.close()
        chunk, stop_on_failure, max_consecutive, resume_wait = (
            studio.image_batch_limits(self.cfg, self.pid))
        self.assertEqual(max_consecutive, 7)
        self.assertEqual(resume_wait, 0)

    def test_an_unknown_pid_still_returns_safe_defaults(self):
        chunk, stop_on_failure, max_consecutive, resume_wait = (
            studio.image_batch_limits(self.cfg, 999999))
        self.assertEqual(max_consecutive, 3)
        self.assertEqual(resume_wait, 600)


class ImageResumeSettingTests(unittest.TestCase):
    """Extends the existing ImageResumeTests (tests/test_shotlist_pacing.py)
    with the new resume_wait_seconds parameter."""

    def test_resume_wait_seconds_overrides_the_refusal_pause(self):
        exc = RuntimeError("3 cards failed in a row after 40 rendered - "
                            "Flow is refusing this session")
        self.assertEqual(autorun._resume_pause_seconds(exc, 120), 120)
        # no setting given: falls back to the fixed constant, unchanged
        self.assertEqual(autorun._resume_pause_seconds(exc),
                         autorun.FLOW_REFUSAL_PAUSE_SECONDS)

    def test_a_wait_of_zero_disables_auto_resume_for_the_refusal_case(self):
        exc = RuntimeError("3 cards failed in a row after 40 rendered - "
                           "Flow is refusing this session")
        self.assertFalse(autorun._should_resume_images(exc, 1, 0))
        # a non-zero (or unset) wait keeps the normal resumable behaviour
        self.assertTrue(autorun._should_resume_images(exc, 1, 120))
        self.assertTrue(autorun._should_resume_images(exc, 1))

    def test_a_wait_of_zero_does_not_affect_the_still_busy_case(self):
        # "were not produced" is a different, shorter-lived wave; 0 only
        # disables auto-resume for the harder "is refusing this session" stop
        exc = RuntimeError("3 of 45 image(s) were not produced - see debug")
        self.assertTrue(autorun._should_resume_images(exc, 1, 0))


if __name__ == "__main__":
    unittest.main()
