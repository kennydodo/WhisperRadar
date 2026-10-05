"""Two efficiency fixes for the shots-stage attempt loop in _run_shots,
confirmed against a real, slow production (812-cue narration taking 45m50s
over 5 full-regeneration attempts) and a real regression (a different
production's attempt 2 came back as 282 shots at 13% detailed, down from a
healthy attempt 1 - a full re-plan does not reliably improve on a prior
attempt, it can swing sharply worse):

1. shotlist_max_tokens() sizes an explicit output-token budget for the
   generation call from the narration itself, so a normal-sized plan
   finishes in one LLM reply instead of needing several sequential
   continuation round trips to finish a reply that was cut off mid-JSON.
   llm_generate() falls back to no cap at all if a provider rejects the
   budget outright (same narrow-match pattern as its existing temperature
   fallback), so a provider with a lower hard ceiling still gets the old
   truncate-and-continue behavior instead of a hard failure.

2. _run_shots stops the attempt loop early once a couple of attempts in a
   row fail to beat the best this run has produced, instead of always
   burning every one of shotlist_max_attempts - `best = max(attempts,
   key=_rank)` already keeps whichever attempt was actually best regardless
   of when the loop stops, so a stalled run loses nothing by stopping early.

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


# --------------------------------------------------------------------------
# 1. shotlist_max_tokens() sizing
# --------------------------------------------------------------------------

class ShotlistMaxTokensTests(unittest.TestCase):
    def test_scales_with_narration_length(self):
        short = studio.shotlist_max_tokens(total_narration_seconds=120,
                                           cue_count=20, max_hold_seconds=12)
        long = studio.shotlist_max_tokens(total_narration_seconds=1200,
                                          cue_count=200, max_hold_seconds=12)
        self.assertLess(short, long)

    def test_never_below_the_fixed_overhead_floor(self):
        tiny = studio.shotlist_max_tokens(total_narration_seconds=1,
                                          cue_count=1, max_hold_seconds=12)
        self.assertGreaterEqual(tiny, studio.SHOTLIST_TOKENS_OVERHEAD)

    def test_never_exceeds_the_safety_ceiling(self):
        # An extreme narration (far beyond anything seen so far) must not ask
        # a provider for an unbounded value.
        huge = studio.shotlist_max_tokens(total_narration_seconds=20000,
                                          cue_count=5000, max_hold_seconds=12)
        self.assertEqual(huge, studio.SHOTLIST_TOKENS_CEILING)

    def test_covers_the_real_812_cue_production_with_headroom(self):
        # Measured directly on the Cheetah production (812 cues, 1072s
        # narration, 178 shots): combined shotlist.json + batch_sheet.txt
        # output was ~149KB, i.e. roughly 37k-45k tokens depending on the
        # chars-per-token assumption. The budget must clear that with margin.
        budget = studio.shotlist_max_tokens(total_narration_seconds=1072.16,
                                            cue_count=812,
                                            max_hold_seconds=12.0)
        self.assertGreaterEqual(budget, 48000)

    def test_estimate_does_not_exceed_one_shot_per_cue(self):
        # Fragmentation (one shot per cue) is the absolute ceiling on shot
        # count - a narration with very few, very long cues must not inflate
        # the estimate past that.
        few_cues_long_hold = studio.shotlist_max_tokens(
            total_narration_seconds=600, cue_count=3, max_hold_seconds=12)
        many_cues_short = studio.shotlist_max_tokens(
            total_narration_seconds=600, cue_count=300, max_hold_seconds=12)
        self.assertLess(few_cues_long_hold, many_cues_short)


class RejectsMaxTokensTests(unittest.TestCase):
    def test_matches_a_max_tokens_specific_error(self):
        self.assertTrue(studio._rejects_max_tokens(
            RuntimeError("Invalid 'max_tokens': exceeds the maximum value")))

    def test_does_not_match_an_unrelated_error(self):
        self.assertFalse(studio._rejects_max_tokens(
            RuntimeError("connection reset by peer")))

    def test_does_not_match_a_temperature_error(self):
        self.assertFalse(studio._rejects_max_tokens(
            RuntimeError("'temperature' is not supported with this model")))


class LLMGenerateMaxTokensFallbackTests(unittest.TestCase):
    """llm_generate must retry on the SAME provider without max_tokens when
    the provider's error is specifically about that parameter, mirroring the
    existing _rejects_temperature fallback - never for an unrelated error."""

    def test_retries_without_max_tokens_on_a_matching_rejection(self):
        calls = []

        def _fake_chat(p, prompt, timeout=600, max_tokens=None,
                       temperature=1.0):
            calls.append(max_tokens)
            if max_tokens:
                raise RuntimeError("max_tokens exceeds the model's maximum")
            return "ok"

        cfg = mock.Mock()
        with mock.patch.object(studio, "CHAT_APIS", {"openai": _fake_chat}), \
             mock.patch.object(studio, "_resolve_provider",
                               return_value={"name": "p", "api": "openai"}):
            result = studio.llm_generate(cfg, "prompt", max_tokens=99999)

        self.assertEqual(result, "ok")
        self.assertEqual(calls, [99999, None])

    def test_does_not_swallow_an_unrelated_runtime_error(self):
        def _fake_chat(p, prompt, timeout=600, max_tokens=None,
                       temperature=1.0):
            raise RuntimeError("no API key")

        cfg = mock.Mock()
        with mock.patch.object(studio, "CHAT_APIS", {"openai": _fake_chat}), \
             mock.patch.object(studio, "_resolve_provider",
                               return_value={"name": "p", "api": "openai"}), \
             mock.patch.object(studio, "_fallback_provider",
                               return_value=None):
            with self.assertRaises(RuntimeError):
                studio.llm_generate(cfg, "prompt", max_tokens=99999)


# --------------------------------------------------------------------------
# 2. _run_shots stops early once attempts stall
# --------------------------------------------------------------------------

def _valid_shotlist_text(n=2):
    shots = ",".join(f'{{"asset": "S{i:02d}.png", "cues": "{i}"}}'
                     for i in range(1, n + 1))
    images = ",".join(f'{{"file": "S{i:02d}.png", "prompt": "p{i}"}}'
                      for i in range(1, n + 1))
    return f'{{"shots": [{shots}], "images": [{images}]}}'


def _review(faults=None, ratio=0.0, matched=0, total=2, weak=None):
    return {"faults": faults or [], "ratio": ratio, "matched": matched,
           "total": total, "weak": weak or [], "warnings": [],
           "unreviewed": 0, "error": None, "verdicts": {}}


class ShotlistStallStopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.pid = db.create_production(conn, "A test production")
        conn.commit()
        conn.close()
        pdir = studio.prod_dir(self.cfg, self.pid)
        (pdir / "subtitles.srt").write_text(
            "1\n00:00:00,000 --> 00:00:02,000\nhello there\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\nworld\n",
            encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, review_sequence):
        # these tests are about the stall counter, so the fault-fix round is
        # switched off (it falls back to a full re-plan, the old behaviour);
        # tests/test_shotlist_fix_loop.py covers the fix round itself
        with mock.patch.object(autorun, "style_bible",
                               return_value=("style", "channel",
                                            "a bible", "channel")), \
             mock.patch.object(studio, "seed_production", return_value={}), \
             mock.patch.object(studio, "load_manifest_brief",
                               return_value="BRIEF"), \
             mock.patch.object(db, "stage_provider",
                               return_value="test-judge"), \
             mock.patch.object(studio, "llm_generate",
                               return_value=_valid_shotlist_text()), \
             mock.patch.object(autorun, "_attempt_shotlist_fix",
                               return_value=None), \
             mock.patch.object(studio, "review_shotlist",
                               side_effect=review_sequence) as review_mock:
            autorun._run_shots(self.cfg, self.pid, provider="test-provider")
        return review_mock

    def test_stops_after_two_consecutive_non_improving_attempts(self):
        # attempt 1: good (no faults, below the alignment bar so it doesn't
        # outright pass). attempts 2 and 3: worse. Default
        # shotlist_max_attempts is 4, so attempt 4 must be skipped.
        review_mock = self._run([
            _review(faults=[], ratio=0.80, matched=16, total=20),
            _review(faults=["a structural fault"], ratio=0.50,
                   matched=10, total=20, weak=[]),
            _review(faults=["a structural fault"], ratio=0.60,
                   matched=12, total=20, weak=[]),
        ])
        self.assertEqual(review_mock.call_count, 3,
                         "a 4th attempt ran despite 2 non-improving in a row")

        conn = db.connect(self.cfg.db_path)
        detail = conn.execute(
            "SELECT detail FROM production_steps WHERE production_id = ? "
            "AND stage = 'shots' ORDER BY id DESC LIMIT 1",
            (self.pid,)).fetchone()["detail"]
        conn.close()
        # the best attempt (attempt 1, 80% detailed) must still be what was
        # kept, even though the loop stopped before using every attempt
        self.assertIn("best of 3 attempt(s)", detail)
        self.assertIn("80%", detail)

    def test_an_improving_attempt_resets_the_stall_counter(self):
        # attempt 1 good, attempt 2 improves on it (resets the counter),
        # attempt 3 regresses once (not yet 2 in a row) - all 4 default
        # attempts must run; nothing stops early.
        review_mock = self._run([
            _review(faults=[], ratio=0.70, matched=14, total=20),
            _review(faults=[], ratio=0.85, matched=17, total=20),
            _review(faults=["a fault"], ratio=0.40, matched=8, total=20),
            _review(faults=["a fault"], ratio=0.45, matched=9, total=20),
        ])
        self.assertEqual(review_mock.call_count, 4,
                         "stopped early despite only one non-improving "
                         "attempt in a row")


if __name__ == "__main__":
    unittest.main()
