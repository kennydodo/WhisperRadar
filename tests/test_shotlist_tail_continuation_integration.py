"""End-to-end: a shotlist reply that closes cleanly but stops before the last
cue gets a cheap tail continuation merged in, instead of burning a whole
extra attempt on a full re-plan (see tests/test_shotlist_tail_gap.py for the
helper functions' own unit tests).

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
00:00:00,000 --> 00:00:01,000
One.

2
00:00:01,000 --> 00:00:02,000
Two.

3
00:00:02,000 --> 00:00:03,000
Three.
"""

TRUNCATED_PLAN = json.dumps({
    "shots": [{"cues": "1-2", "asset": "S01_01_SCN_ZI.png"}],
    "images": [{"file": "S01_01_SCN_ZI.png",
               "prompt": "a detailed prompt covering cues one and two"}],
})

TAIL_ADDITION = json.dumps({
    "shots": [{"cues": "3", "asset": "S01_02_SCN_ZI.png"}],
    "images": [{"file": "S01_02_SCN_ZI.png",
               "prompt": "a detailed prompt covering the third cue"}],
})


class ShotlistTailContinuationIntegrationTests(unittest.TestCase):
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

    def test_a_clean_early_stop_is_completed_by_a_tail_continuation(self):
        calls = {"planner": 0, "tail": 0, "judge": 0}

        def fake(cfg, prompt, provider=None, max_tokens=None, temperature=1.0):
            if provider == "planner-test":
                if "validly-closed" in prompt:
                    calls["tail"] += 1
                    return TAIL_ADDITION
                calls["planner"] += 1
                return TRUNCATED_PLAN
            calls["judge"] += 1
            return json.dumps([
                {"asset": "S01_01_SCN_ZI.png", "verdict": "ok"},
                {"asset": "S01_02_SCN_ZI.png", "verdict": "ok"},
            ])

        with mock.patch.object(studio, "load_manifest_brief",
                               return_value="BRIEF"), \
                mock.patch.object(studio, "llm_generate", fake):
            result = autorun.run_stage(self.cfg, self.pid, "shots",
                                       params={"provider": "planner-test"})

        self.assertEqual(result, "ok", result)
        self.assertEqual(calls["planner"], 1)
        self.assertEqual(calls["tail"], 1)
        shotlist = json.loads((studio.prod_dir(self.cfg, self.pid)
                              / "shotlist.json").read_text(encoding="utf-8"))
        self.assertEqual(len(shotlist["shots"]), 2)
        self.assertEqual(len(shotlist["images"]), 2)
        # the fault-detection gate itself confirms full, gapless coverage
        self.assertEqual(studio.shotlist_structural_faults(shotlist, 3), [])


if __name__ == "__main__":
    unittest.main()
