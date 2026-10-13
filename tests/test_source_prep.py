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
        for stage in ("style", "script", "audio"):
            e = autorun.stage_action(self.cfg, self.pid, stage)
            self.assertEqual(e["action"], "pause", stage)
            self.assertIn("source transcript", e["detail"])

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

    def test_production_without_source_never_waits(self):
        pid = db.create_production(self.conn, "Q", "general", None, None)
        self.assertIsNone(source_prep.state(self.cfg, pid))


if __name__ == "__main__":
    unittest.main()
