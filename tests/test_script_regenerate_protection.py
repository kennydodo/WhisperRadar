"""A script regenerate must never silently replace a better script with a
worse one.

Before this fix, `_run_script` always picked the best of THIS run's own
attempts and unconditionally overwrote script.md - there was no comparison
against the script already on disk. Combined with the JSON-parsing bug (see
test_script_regenerate.py's RateScriptErrorTests), which made the judge's
score come back None on nearly every real call, "best of this run's
attempts" degenerated to "lowest overlap wins" - a tiebreak with no
relationship to writing quality - and a regenerate could easily leave you
with something worse than what you started with.

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

# ~100 distinct words each, so overlap with the source stays low and the
# length gate (0.6x-1.15x of source word count) never trips either draft.
# Trailing periods so these fixtures read as a COMPLETE draft, not a
# mid-sentence cutoff - script_looks_truncated (added for the continuation
# fix) would otherwise trigger an extra llm_generate() call on NEW_ATTEMPT,
# which this module's tests mock to always return the same fixture text.
EXISTING_SCRIPT = " ".join(f"existingword{i}" for i in range(100)) + "."
NEW_ATTEMPT = " ".join(f"newword{i}" for i in range(100)) + "."
SOURCE_TRANSCRIPT = " ".join(f"sourceword{i}" for i in range(100)) + "."


class ScriptRegenerateProtectionTests(unittest.TestCase):
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
        db.set_setting(conn, "script_judge_provider", "judge-test")
        db.set_setting(conn, "script_max_attempts", 1)
        db.set_setting(conn, "script_min_rating", 0)
        conn.commit()
        conn.close()

        self.pdir = studio.prod_dir(self.cfg, self.pid)
        self.pdir.mkdir(parents=True, exist_ok=True)
        (self.pdir / "source_transcript.txt").write_text(
            SOURCE_TRANSCRIPT, encoding="utf-8")
        (self.pdir / "script.md").write_text(EXISTING_SCRIPT + "\n",
                                             encoding="utf-8")

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def _rate(self, existing_score, new_score):
        def fake_rate(cfg, title, genre, script, source, style_guide,
                     provider, temperature=1.0, extra_direction="", original=""):
            score = existing_score if script.strip() == EXISTING_SCRIPT \
                else new_score
            return {"score": score, "criteria": {}, "feedback": [],
                    "weak_spans": [], "error": None}
        return fake_rate

    def test_a_worse_new_attempt_does_not_overwrite_the_existing_script(self):
        with mock.patch.object(studio, "llm_generate",
                               return_value=NEW_ATTEMPT), \
                mock.patch.object(studio, "rate_script",
                                  self._rate(existing_score=9.5, new_score=3.0)):
            result = autorun.run_stage(self.cfg, self.pid, "script",
                                       params={"provider": "writer-test"})
        self.assertEqual(result, "ok")
        self.assertEqual((self.pdir / "script.md").read_text(encoding="utf-8"),
                         EXISTING_SCRIPT + "\n")
        # the rejected new attempt is still preserved for reference
        self.assertTrue((self.pdir / "versions" / "script"
                        / "attempt-1.md").exists())
        review = json.loads((self.pdir / "versions" / "script"
                            / "review.json").read_text(encoding="utf-8"))
        baseline_entries = [r for r in review if r["is_baseline"]]
        self.assertEqual(len(baseline_entries), 1)
        self.assertEqual(baseline_entries[0]["score"], 9.5)

    def test_a_better_new_attempt_does_replace_the_existing_script(self):
        with mock.patch.object(studio, "llm_generate",
                               return_value=NEW_ATTEMPT), \
                mock.patch.object(studio, "rate_script",
                                  self._rate(existing_score=3.0, new_score=9.5)):
            result = autorun.run_stage(self.cfg, self.pid, "script",
                                       params={"provider": "writer-test"})
        self.assertEqual(result, "ok")
        self.assertEqual((self.pdir / "script.md").read_text(encoding="utf-8"),
                         NEW_ATTEMPT + "\n")
        # the old script is archived, not lost
        archived = list((self.pdir / "versions" / "script").glob("auto-*.md"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_text(encoding="utf-8"),
                         EXISTING_SCRIPT + "\n")

    def test_the_first_run_with_no_existing_script_is_unaffected(self):
        (self.pdir / "script.md").unlink()
        with mock.patch.object(studio, "llm_generate",
                               return_value=NEW_ATTEMPT), \
                mock.patch.object(studio, "rate_script",
                                  self._rate(existing_score=9.9, new_score=1.0)):
            result = autorun.run_stage(self.cfg, self.pid, "script",
                                       params={"provider": "writer-test"})
        self.assertEqual(result, "ok")
        self.assertEqual((self.pdir / "script.md").read_text(encoding="utf-8"),
                         NEW_ATTEMPT + "\n")


if __name__ == "__main__":
    unittest.main()
