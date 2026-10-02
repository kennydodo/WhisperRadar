"""A too-short draft must not win "best of attempts" on rating alone.

Confirmed on a real production: a 1685-word attempt (44% of a 3827-word
target, well under the 60% too_short floor) rated HIGHER than four longer
attempts and was picked as "best" purely by score - a too-short draft has
less surface area for the judge to find repetition or overreach in, so
brevity itself can inflate the rating. _run_script now ranks any
non-too-short candidate above every too-short one first, falling back to a
too-short candidate only when nothing else is available.

Run: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import autorun, db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402

# target_words is fixed at 1000 via cfg.studio_script_words in setUp, so the
# too_short floor (0.6x) sits at 600 words.
SHORT_HIGH_SCORE = " ".join(f"short{i}" for i in range(100)) + "."  # too short
FULL_LOW_SCORE = " ".join(f"full{i}" for i in range(1000)) + "."   # on target


class ScriptBestOfAttemptsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.cfg.studio_script_words = 1000
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        chan = db.create_own_channel(conn, "Ch")
        self.pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, self.pid, own_channel_id=chan)
        db.set_setting(conn, "script_judge_provider", "judge-test")
        # both attempts must fail the rating gate so the run uses the full
        # attempt budget instead of breaking early on a pass
        db.set_setting(conn, "script_max_attempts", 2)
        db.set_setting(conn, "script_min_rating", 9.5)
        conn.commit()
        conn.close()

        self.pdir = studio.prod_dir(self.cfg, self.pid)
        self.pdir.mkdir(parents=True, exist_ok=True)
        # left EMPTY on purpose: a non-empty transcript makes _run_script call
        # llm_generate an EXTRA time up front to build research notes, which
        # would throw off the fixed [attempt1, attempt2] side_effect list
        # below. An empty transcript skips that call entirely (facts = "").
        (self.pdir / "source_transcript.txt").write_text("", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _rate(self, cfg, title, genre, script, source, style_guide,
             provider, temperature=1.0, extra_direction=""):
        score = 9.0 if script.strip() == SHORT_HIGH_SCORE else 7.0
        return {"score": score, "criteria": {}, "feedback": [],
                "weak_spans": [], "error": None}

    def test_a_fuller_lower_scoring_draft_beats_a_too_short_higher_scoring_one(self):
        with mock.patch.object(studio, "llm_generate",
                               side_effect=[SHORT_HIGH_SCORE, FULL_LOW_SCORE]), \
                mock.patch.object(studio, "rate_script", self._rate):
            result = autorun.run_stage(self.cfg, self.pid, "script",
                                       params={"provider": "writer-test"})
        self.assertEqual(result, "ok")
        self.assertEqual((self.pdir / "script.md").read_text(encoding="utf-8"),
                         FULL_LOW_SCORE + "\n")

    def test_a_too_short_draft_is_used_only_when_nothing_else_is_available(self):
        with mock.patch.object(studio, "llm_generate",
                               side_effect=[SHORT_HIGH_SCORE, SHORT_HIGH_SCORE]), \
                mock.patch.object(studio, "rate_script", self._rate):
            result = autorun.run_stage(self.cfg, self.pid, "script",
                                       params={"provider": "writer-test"})
        self.assertEqual(result, "ok")
        self.assertEqual((self.pdir / "script.md").read_text(encoding="utf-8"),
                         SHORT_HIGH_SCORE + "\n")


if __name__ == "__main__":
    unittest.main()
