"""The images consecutive-failure-stop threshold and the waits before an
auto-resume after it / after a 'still busy' wave are settings
(images_max_consecutive_failures, images_resume_wait_minutes,
images_still_busy_wait_minutes) instead of hardcoded constants, shared by
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
        chunk, stop, max_conc, refusal_wait, busy_wait = (
            studio.image_batch_limits(self.cfg, self.pid))
        self.assertEqual(max_conc, 5)
        self.assertEqual(refusal_wait, 600)   # 10 minutes, in seconds
        self.assertEqual(busy_wait, 300)      # 5 minutes, in seconds

    def test_a_saved_setting_is_picked_up(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.set_setting(conn, "images_max_consecutive_failures", "7")
        db.set_setting(conn, "images_resume_wait_minutes", "0")
        db.set_setting(conn, "images_still_busy_wait_minutes", "3")
        conn.close()
        chunk, stop, max_conc, refusal_wait, busy_wait = (
            studio.image_batch_limits(self.cfg, self.pid))
        self.assertEqual(max_conc, 7)
        self.assertEqual(refusal_wait, 0)
        self.assertEqual(busy_wait, 180)

    def test_an_unknown_pid_still_returns_safe_defaults(self):
        chunk, stop, max_conc, refusal_wait, busy_wait = (
            studio.image_batch_limits(self.cfg, 999999))
        self.assertEqual(max_conc, 5)
        self.assertEqual(refusal_wait, 600)
        self.assertEqual(busy_wait, 300)


class ImageResumeSettingTests(unittest.TestCase):
    """The resume-wait settings feed _should_resume_images /
    _resume_pause_seconds (the refusal and 'still busy' cases separately)."""

    REFUSAL = RuntimeError("3 cards failed in a row after 40 rendered - "
                           "Flow is refusing this session")
    BUSY = RuntimeError("3 of 45 image(s) were not produced - see debug")

    def test_refusal_wait_overrides_the_refusal_pause(self):
        self.assertEqual(autorun._resume_pause_seconds(self.REFUSAL, 120), 120)
        # no setting given: falls back to the fixed constant, unchanged
        self.assertEqual(autorun._resume_pause_seconds(self.REFUSAL),
                         autorun.FLOW_REFUSAL_PAUSE_SECONDS)

    def test_still_busy_wait_overrides_its_pause(self):
        self.assertEqual(
            autorun._resume_pause_seconds(self.BUSY, None, 90), 90)
        self.assertEqual(autorun._resume_pause_seconds(self.BUSY),
                         autorun.IMAGE_RESUME_PAUSE_SECONDS)

    def test_a_zero_refusal_wait_disables_only_the_refusal_case(self):
        self.assertFalse(autorun._should_resume_images(self.REFUSAL, 1, 0))
        self.assertTrue(autorun._should_resume_images(self.REFUSAL, 1, 120))
        self.assertTrue(autorun._should_resume_images(self.REFUSAL, 1))
        # the refusal wait of 0 must not disable the 'still busy' case
        self.assertTrue(autorun._should_resume_images(self.BUSY, 1, 0))

    def test_a_zero_still_busy_wait_disables_only_that_case(self):
        self.assertFalse(autorun._should_resume_images(self.BUSY, 1, None, 0))
        self.assertTrue(autorun._should_resume_images(self.BUSY, 1, None, 300))
        # the still-busy wait of 0 must not disable the refusal case
        self.assertTrue(autorun._should_resume_images(self.REFUSAL, 1, 120, 0))


if __name__ == "__main__":
    unittest.main()
