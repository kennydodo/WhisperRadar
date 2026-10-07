"""The images consecutive-failure-stop threshold and the waits before an
auto-resume after it / after a 'still busy' wave are settings
(images_max_consecutive_failures, images_resume_wait_minutes,
images_still_busy_wait_minutes) instead of hardcoded constants, shared by
manual render and auto-run and by both engines (Renderly API + FlowBatch).

Run: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

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


class ThrottleBlockTests(unittest.TestCase):
    """Flow's 'unusual activity' block: stop at the first refusal, wait the
    (much longer) throttle wait, then resume - both engines, one error."""

    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(tempfile.mkdtemp()) / "wr.db"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.pid = db.create_production(conn, "T")
        conn.commit()
        conn.close()
        self.exc = studio.throttle_error("FlowBatch", 60)

    def test_the_pattern_matches_flows_wording_only(self):
        for text in ("We noticed some unusual activity. Please visit the Help "
                     "Center", "Flow refused the generation: unusual activity"):
            self.assertTrue(studio.THROTTLE_PATTERN.search(text))
        self.assertFalse(studio.THROTTLE_PATTERN.search("still busy"))

    def test_default_wait_is_an_hour_and_the_setting_overrides_it(self):
        self.assertEqual(studio.image_throttle_wait_seconds(self.cfg, self.pid),
                         3600)
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.set_setting(conn, "images_throttle_wait_minutes", "90")
        conn.close()
        self.assertEqual(studio.image_throttle_wait_seconds(self.cfg, self.pid),
                         5400)
        self.assertEqual(autorun._resume_pause_seconds(
            self.exc, 600, 300, 5400), 5400)

    def test_unknown_production_falls_back_to_an_hour(self):
        self.assertEqual(studio.image_throttle_wait_seconds(self.cfg, 99999),
                         3600)

    def test_the_block_resumes_with_its_own_pause_not_the_refusal_one(self):
        self.assertTrue(autorun._should_resume_images(self.exc, 1, 600, 300))
        self.assertEqual(autorun._resume_pause_seconds(self.exc, 120, 90),
                         autorun.THROTTLE_PAUSE_SECONDS)
        self.assertGreaterEqual(autorun.THROTTLE_PAUSE_SECONDS, 3600)

    def test_a_zero_throttle_wait_stops_for_a_manual_resume(self):
        self.assertFalse(autorun._should_resume_images(
            self.exc, 1, 600, 300, 0))
        # ... and does not disable the other two cases
        self.assertTrue(autorun._should_resume_images(
            RuntimeError("Flow is refusing this session"), 1, 600, 300, 0))

    def test_rounds_run_out(self):
        self.assertFalse(autorun._should_resume_images(
            self.exc, autorun.IMAGE_RESUME_ROUNDS))

    def test_a_long_wait_ends_at_once_on_stop(self):
        calls = []
        with self.assertRaises(studio.BatchCancelled):
            autorun._pause(3600, cancel=lambda: len(calls) >= 1 or
                           calls.append(1))
        slept = []
        with unittest.mock.patch("time.sleep", slept.append):
            autorun._pause(12)
        self.assertEqual(slept, [5, 5, 2])

    def test_setting_defaults(self):
        from whisperradar import settings
        self.assertFalse(settings.SPEC_BY_KEY["images_stop_on_failure"]["default"])
        self.assertEqual(
            settings.SPEC_BY_KEY["images_throttle_wait_minutes"]["default"], 60)


class SettingsCheckboxTests(unittest.TestCase):
    """An unticked checkbox is posted as nothing by the browser; the page now
    posts a hidden 0 first, so unticking saves."""

    def setUp(self):
        from whisperradar.webapp import create_app
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        conn.close()
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def _value(self, key):
        from whisperradar import settings
        conn = db.connect(self.cfg.db_path)
        try:
            return settings.load(conn)[key]
        finally:
            conn.close()

    def test_every_checkbox_posts_a_hidden_zero_first(self):
        from whisperradar import settings
        page = self.client.get("/settings").get_data(as_text=True)
        for entry in settings.SPEC:
            if entry["type"] == "bool":
                zero = f'<input type="hidden" name="{entry["key"]}" value="0">'
                box = f'name="{entry["key"]}" id="f-{entry["key"]}"'
                self.assertIn(zero, page, entry["key"])
                self.assertLess(page.index(zero), page.index(box), entry["key"])

    def test_unticking_and_ticking_both_save(self):
        from whisperradar import settings
        bools = [e["key"] for e in settings.SPEC if e["type"] == "bool"]
        # all ticked: the browser posts hidden 0 then "on"
        self.client.post("/settings/save", data={k: ["0", "on"] for k in bools})
        for key in bools:
            self.assertTrue(self._value(key), key)
        # all unticked: only the hidden 0 arrives
        self.client.post("/settings/save", data={k: "0" for k in bools})
        for key in bools:
            self.assertFalse(self._value(key), key)

    def test_a_partial_post_leaves_other_checkboxes_alone(self):
        self.client.post("/settings/save", data={"autorun_resume": ["0", "on"]})
        self.client.post("/settings/save", data={"images_chunk_size": "10"})
        self.assertTrue(self._value("autorun_resume"))


class ImageDelayTests(unittest.TestCase):
    """images_delay_seconds: one pacing setting for both engines."""

    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(tempfile.mkdtemp()) / "wr.db"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        conn.close()

    def _set(self, value):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.set_setting(conn, "images_delay_seconds", value)
        conn.close()

    def test_default_is_zero_and_the_setting_is_read(self):
        from whisperradar import settings
        self.assertEqual(studio.image_delay_seconds(self.cfg), 0)
        self.assertIn("images_delay_seconds",
                      dict(settings.GROUPS)["Production & images"])
        self._set("20")
        self.assertEqual(studio.image_delay_seconds(self.cfg), 20)
        self.assertEqual(studio.image_delay_seconds(self.cfg, 5), 20)

    def test_a_bad_value_never_blocks_a_batch(self):
        self._set("abc")
        self.assertEqual(studio.image_delay_seconds(self.cfg), 0)

