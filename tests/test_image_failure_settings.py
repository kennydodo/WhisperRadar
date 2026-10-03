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
import unittest.mock
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
        self.exc = studio.throttle_error("Flow Driver", 60)

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
        self.tmp.cleanup()

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


class FlowDriverLoopTests(unittest.TestCase):
    """The Flow Driver polling loop: first-failure stop (setting) and the
    'unusual activity' throttle stop, both calling flow_stop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.pid = db.create_production(conn, "T")
        conn.commit()
        conn.close()
        self.pdir = Path(self.tmp.name) / "p"
        self.pdir.mkdir()
        (self.pdir / "shotlist.json").write_text("{}", encoding="utf-8")
        self.driver = Path(self.tmp.name) / "drv"
        (self.driver / "node_modules" / "playwright").mkdir(parents=True)
        (self.driver / "flow.js").write_text("", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, statuses, stop_on_failure=False, throttle_minutes=60):
        stopped = []
        seq = iter(statuses)
        last = [statuses[-1]]

        def status(cfg, timeout=4):
            try:
                last[0] = next(seq)
            except StopIteration:
                pass
            return last[0]

        limits = (0, stop_on_failure, 5, 600, 300)
        mocks = unittest.mock.patch.multiple(
            studio, flow_driver_dir=lambda c: self.driver,
            ensure_flow_services=lambda c, log=None: None,
            run_flowdriver_prepare=lambda *a, **k: {},
            _apply_flowdriver_report=lambda *a, **k: None,
            flow_project_url_for=lambda c, p, o=None: ("", "none"),
            missing_flow_images=lambda c, d: 3,
            flow_service_status=status,
            _driver_api=lambda *a, **k: {},
            flow_stop=lambda c: stopped.append(1),
            image_batch_limits=lambda c, p: limits,
            image_throttle_wait_seconds=lambda c, p: throttle_minutes * 60)
        with mocks, unittest.mock.patch("time.sleep", lambda s: None):
            try:
                studio.run_imagegen_flow(self.cfg, self.pdir, pid=self.pid,
                                         log=lambda m: None)
            except RuntimeError as exc:
                return exc, stopped
        return None, stopped

    def test_unusual_activity_stops_at_the_first_refusal(self):
        st = [{"running": False, "log": [], "counts": {"ok": 0}},   # prepare
              {"running": False, "log": [], "counts": {"ok": 0}},   # attach
              {"running": False, "log": [], "counts": {"ok": 0}},   # base ok
              {"running": False, "log": [], "counts": {"ok": 0}},   # base log
              {"running": True, "counts": {"ok": 1, "failed": 0},
               "log": ["saved a", "Flow refused the generation for \"b\": "
                       "We noticed some unusual activity"]}]
        exc, stopped = self._run(st)
        self.assertIn(studio.THROTTLE_MARKER, str(exc))
        self.assertIn("60 min", str(exc))
        self.assertEqual(len(stopped), 1)

    def test_an_old_unusual_activity_line_is_ignored(self):
        old = ["earlier batch: We noticed some unusual activity"]
        st = [{"running": False, "log": old, "counts": {"ok": 0}},
              {"running": False, "log": old, "counts": {"ok": 0}},
              {"running": False, "log": old, "counts": {"ok": 0}},
              {"running": False, "log": old + ["saved a"],
               "counts": {"ok": 1}}]
        exc, _ = self._run(st)
        self.assertNotIn(studio.THROTTLE_MARKER, str(exc))

    def test_the_first_failure_stops_the_batch_when_ticked(self):
        base = {"running": False, "log": [], "counts": {"ok": 0}}
        st = [base, base, base,
              {"running": True, "log": [], "counts": {"ok": 2, "failed": 0}},
              {"running": True, "log": [], "counts": {"ok": 2, "failed": 1}}]
        exc, stopped = self._run(st, stop_on_failure=True)
        self.assertIn("first failure", str(exc))
        self.assertEqual(len(stopped), 1)

    def test_a_single_failure_is_tolerated_when_unticked(self):
        base = {"running": False, "log": [], "counts": {"ok": 0}}
        st = [base, base, base,
              {"running": True, "log": [], "counts": {"ok": 2, "failed": 1}},
              {"running": False, "log": [], "counts": {"ok": 2, "failed": 1}}]
        exc, stopped = self._run(st, stop_on_failure=False)
        self.assertNotIn("first failure", str(exc))
        self.assertEqual(stopped, [])
