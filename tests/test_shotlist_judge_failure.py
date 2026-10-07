"""A total shotlist-judge failure (every judge call errors out, e.g. gpt-6-luna's
"Unsupported parameter: 'temperature'" 400) must not look like a clean pass.

Faults come from structural/pacing checks that run independent of the judge,
so a dead judge and a genuinely perfect plan both show "0 faults" - the only
tell is that nothing got judged at all. Before this fix, `_run_shots` never
looked at `review["error"]`/`unreviewed`, so this silently completed the
stage with "0 fault(s)" and moved straight to (costly) image rendering on a
shotlist that was never actually checked. It must pause instead, the same
way a missing bible already pauses rather than guessing.

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

from tests import wr_tmp  # noqa: E402

from whisperradar import autorun, db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402

BIBLE = "MAYA - the host, woman in her 30s, black hair, mustard scarf."
SRT = """1
00:00:00,000 --> 00:00:02,000
Hello world.
"""

SHOTLIST_JSON = json.dumps({
    "shots": [{"cues": "1-1", "asset": "S01_01_SCN_ZI.png"}],
    "images": [{"file": "S01_01_SCN_ZI.png", "prompt": "a cat at a bus stop"}],
})

UNSUPPORTED_TEMPERATURE = ("Unsupported parameter: 'temperature' is not "
                          "supported with this model.")


class ShotlistJudgeFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.chan = db.create_own_channel(conn, "Ch")
        self.pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, self.pid, own_channel_id=self.chan)
        db.set_setting(conn, "shotlist_judge_provider", "judge-test")
        db.set_setting(conn, "shotlist_max_attempts", 1)
        conn.commit()
        conn.close()

        pdir = studio.prod_dir(self.cfg, self.pid)
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "bible.md").write_text(BIBLE, encoding="utf-8")
        (pdir / "subtitles.srt").write_text(SRT, encoding="utf-8")

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def _fake_llm(self, judge_error=UNSUPPORTED_TEMPERATURE):
        def fake(cfg, prompt, provider=None, max_tokens=None, temperature=1.0):
            if provider == "planner-test":
                return SHOTLIST_JSON
            raise RuntimeError(judge_error)
        return fake

    def test_a_total_judge_failure_pauses_instead_of_completing_silently(self):
        with mock.patch.object(studio, "load_manifest_brief",
                               return_value="BRIEF"), \
                mock.patch.object(studio, "llm_generate", self._fake_llm()):
            result = autorun.run_stage(self.cfg, self.pid, "shots",
                                       params={"provider": "planner-test"})
        self.assertTrue(result.startswith("paused:"), result)
        self.assertIn("could not review", result)
        self.assertIn(UNSUPPORTED_TEMPERATURE, result)
        # nothing was written as the accepted shotlist - a paused stage must
        # not hand an unverified plan on to the (costly) images stage
        self.assertFalse((studio.prod_dir(self.cfg, self.pid)
                          / "shotlist.json").exists())

    def test_a_healthy_judge_still_completes_normally(self):
        def fake(cfg, prompt, provider=None, max_tokens=None, temperature=1.0):
            if provider == "planner-test":
                return SHOTLIST_JSON
            return '{"shots": [{"asset": "S01_01_SCN_ZI.png", "verdict": "ok"}]}'

        with mock.patch.object(studio, "load_manifest_brief",
                               return_value="BRIEF"), \
                mock.patch.object(studio, "llm_generate", fake):
            result = autorun.run_stage(self.cfg, self.pid, "shots",
                                       params={"provider": "planner-test"})
        self.assertEqual(result, "ok")
        self.assertTrue((studio.prod_dir(self.cfg, self.pid)
                        / "shotlist.json").exists())


if __name__ == "__main__":
    unittest.main()
