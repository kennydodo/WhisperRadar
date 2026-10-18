"""A production made from a video with no transcript waits for its own job."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import autorun, db, source_prep, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402


class SourceWaitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = t / "wr.db"
        self.cfg.studio_dir = t / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        db.add_channel(self.conn, "Src", "UC1")
        db.upsert_video(self.conn, "UC1", {"video_id": "v1", "title": "T",
                                               "url": "u"})
        self.pid = db.create_production(self.conn, "P", "general", "v1", None)
        self.t = t

    def tearDown(self):
        source_prep._active.discard("v1")
        source_prep._failed.pop("v1", None)
        self.conn.close()
        self.tmp.cleanup()

    def test_no_transcript_means_the_pipeline_pauses_on_every_stage(self):
        self.assertIsNotNone(source_prep.state(self.cfg, self.pid))
        for stage in ("script", "audio"):
            e = autorun.stage_action(self.cfg, self.pid, stage)
            self.assertEqual(e["action"], "pause", stage)
            self.assertIn("source transcript", e["detail"])
        # stage 1 loads the channel's fixed writing style: it never waits for
        # (or reads) the source, and with no style saved it asks for one
        e = autorun.stage_action(self.cfg, self.pid, "style")
        self.assertEqual(e["action"], "pause")
        self.assertIn("writing style", e["detail"])

    def test_running_job_reads_as_waiting_and_failure_as_error(self):
        source_prep._active.add("v1")
        self.assertEqual(source_prep.state(self.cfg, self.pid)["state"], "waiting")
        source_prep._active.discard("v1")
        source_prep._failed["v1"] = "boom"
        st = source_prep.state(self.cfg, self.pid)
        self.assertEqual(st["state"], "error")
        self.assertIn("boom", st["text"])

    def test_transcript_arrives_and_is_copied_into_the_production(self):
        tr = self.t / "v1.txt"
        tr.write_text("hello source", encoding="utf-8")
        db.set_video(self.conn, "v1", status="transcribed",
                     transcript_path=str(tr))
        self.assertIsNone(source_prep.state(self.cfg, self.pid))
        pdir = studio.prod_dir(self.cfg, self.pid)
        self.assertEqual((pdir / "source_transcript.txt").read_text(
            encoding="utf-8"), "hello source")

    def test_callback_fires_after_a_successful_fetch_only(self):
        import time
        calls = []
        orig = source_prep.prepare
        try:
            source_prep.prepare = lambda cfg, vid, log_fn=None: None
            source_prep.when_ready("v1", lambda: calls.append("go"))
            source_prep.start_background(self.cfg, "v1")
            for _ in range(50):
                if calls:
                    break
                time.sleep(0.05)
            self.assertEqual(calls, ["go"])

            def boom(cfg, vid, log_fn=None):
                raise RuntimeError("x")
            source_prep.prepare = boom
            source_prep.when_ready("v1", lambda: calls.append("bad"))
            time.sleep(0.2)
            source_prep.start_background(self.cfg, "v1")
            time.sleep(0.5)
            self.assertEqual(calls, ["go"])
        finally:
            source_prep.prepare = orig
            source_prep.drop_waiting("v1")

    def test_production_without_source_never_waits(self):
        pid = db.create_production(self.conn, "Q", "general", None, None)
        self.assertIsNone(source_prep.state(self.cfg, pid))


    def test_starting_the_app_does_not_create_folders_of_missing_productions(self):
        from whisperradar.webapp import create_app
        gone = self.t / "gone-folder"
        db.update_production(self.conn, self.pid, work_dir=str(gone))
        create_app(self.cfg)
        import time
        time.sleep(0.5)
        self.assertFalse(gone.exists())


if __name__ == "__main__":
    unittest.main()


class AutoRunRouteTests(unittest.TestCase):
    setUp = SourceWaitTests.setUp
    tearDown = SourceWaitTests.tearDown

    def test_autorun_click_while_waiting_queues_instead_of_erroring(self):
        from whisperradar.webapp import create_app
        import time
        orig = source_prep.prepare
        orig_run = autorun.run_pipeline
        source_prep.prepare = lambda cfg, vid, log_fn=None: None
        autorun.run_pipeline = lambda *a, **k: "ok"
        try:
            client = create_app(self.cfg).test_client()
            r = client.post(f"/studio/{self.pid}/auto-run", follow_redirects=False)
            loc = r.headers.get("Location", "")
            self.assertIn("will+start+by+itself", loc.replace("%20", "+"))
            time.sleep(0.5)
        finally:
            source_prep.prepare = orig
            autorun.run_pipeline = orig_run
            source_prep.drop_waiting("v1")


class ResumeAfterRestartTests(unittest.TestCase):
    setUp = SourceWaitTests.setUp
    tearDown = SourceWaitTests.tearDown

    def test_a_saved_request_runs_after_a_restart_once_the_text_exists(self):
        import json
        import time
        from whisperradar.webapp import create_app
        tr = self.t / "v1.txt"
        tr.write_text("hello", encoding="utf-8")
        db.set_video(self.conn, "v1", status="transcribed", transcript_path=str(tr))
        marker = studio.prod_dir(self.cfg, self.pid) / "autorun_after_source.json"
        marker.write_text(json.dumps({"provider": None}), encoding="utf-8")
        calls = []
        orig = autorun.run_pipeline
        autorun.run_pipeline = lambda *a, **k: calls.append(a[1]) or "ok"
        try:
            create_app(self.cfg)
            for _ in range(60):
                if calls:
                    break
                time.sleep(0.1)
            self.assertEqual(calls, [self.pid])
            self.assertFalse(marker.exists())
        finally:
            autorun.run_pipeline = orig
