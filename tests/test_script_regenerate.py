"""Regenerating a script: angle rotation, notes refresh, judge-error surfacing.

Run: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import autorun, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402


class VariationNudgeTests(unittest.TestCase):
    def test_angle_rotates_by_version(self):
        first = studio.variation_nudge(attempt=1, version=0)
        second = studio.variation_nudge(attempt=1, version=1)
        self.assertIn(studio.VARIATION_ANGLES[0], first)
        self.assertIn(studio.VARIATION_ANGLES[1], second)

    def test_attempt_advances_the_angle_within_a_run(self):
        a1 = studio.variation_nudge(attempt=1, version=0)
        a2 = studio.variation_nudge(attempt=2, version=0)
        self.assertIn(studio.VARIATION_ANGLES[0], a1)
        self.assertIn(studio.VARIATION_ANGLES[1], a2)

    def test_regenerate_adds_an_explicit_directive(self):
        self.assertNotIn("previous version of this script",
                         studio.variation_nudge(attempt=1, version=0))
        self.assertIn("previous version of this script",
                      studio.variation_nudge(attempt=1, version=1))


class ResearchNotesTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        self.pdir = Path(tempfile.mkdtemp())
        (self.pdir / autorun.RESEARCH_NOTES_FILE).write_text(
            "cached facts\n", encoding="utf-8")

    def test_uses_cache_by_default(self):
        with mock.patch("whisperradar.studio.llm_generate") as gen:
            notes = autorun._research_notes(
                self.cfg, self.pdir, "T", "general", "source", None)
        self.assertEqual(notes, "cached facts")
        gen.assert_not_called()

    def test_refresh_rebuilds_the_notes(self):
        with mock.patch("whisperradar.studio.llm_generate",
                        return_value="fresh facts\n") as gen:
            notes = autorun._research_notes(
                self.cfg, self.pdir, "T", "general", "source", None,
                refresh=True)
        self.assertEqual(notes, "fresh facts")
        gen.assert_called_once()
        self.assertEqual(
            (self.pdir / autorun.RESEARCH_NOTES_FILE).read_text(
                encoding="utf-8"), "fresh facts\n")


class RateScriptErrorTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")

    def test_no_score_reply_surfaces_the_reason(self):
        with mock.patch("whisperradar.studio.llm_generate",
                        return_value="I cannot rate this, sorry."):
            rating = studio.rate_script(self.cfg, "T", "general", "script",
                                        "source", "style", None)
        self.assertIsNone(rating["score"])
        self.assertIn("no usable score", rating["error"])

    def test_valid_reply_scores_without_error(self):
        reply = ('{"score": 8.5, "criteria": {}, "feedback": [], '
                 '"weak_spans": []}')
        with mock.patch("whisperradar.studio.llm_generate",
                        return_value=reply):
            rating = studio.rate_script(self.cfg, "T", "general", "script",
                                        "source", "style", None)
        self.assertEqual(rating["score"], 8.5)
        self.assertIsNone(rating["error"])

    def test_trailing_commentary_with_a_stray_brace_does_not_lose_the_score(self):
        # A real production's judge replies came back "no usable score" over
        # and over - but the raw reply, truncated in the log, plainly showed
        # a real score ({"score":5.2,...}). The old `re.search(r"\{.*\}")`
        # matched from the first "{" to the LAST "}" anywhere in the text, so
        # one sentence of commentary after the JSON (reasoning-style models
        # add this constantly despite being told to reply with ONLY JSON)
        # with a stray brace in it broke json.loads and threw the whole
        # verdict away - score included. This is the exact failure shape.
        reply = ('{"score": 5.2, "criteria": {"hook": 7}, '
                '"feedback": ["Keep the low-overlap rewrite, but tighten '
                'the hook."], "weak_spans": []}\n\n'
                'Note: I nearly scored this higher {but the pacing dragged}.')
        with mock.patch("whisperradar.studio.llm_generate",
                        return_value=reply):
            rating = studio.rate_script(self.cfg, "T", "general", "script",
                                        "source", "style", None)
        self.assertEqual(rating["score"], 5.2)
        self.assertIsNone(rating["error"])

    def test_a_stray_brace_in_a_shotlist_judge_reply_is_also_tolerated(self):
        # review_shotlist parses its per-chunk verdicts through the same
        # _parse_json_object - the fix must cover both judges.
        reply = ('{"shots": [{"asset": "a.png", "verdict": "ok"}]}\n\n'
                'Aside: a couple of these felt thin {barely}.')
        data = {"shots": [{"cues": "1-1", "asset": "a.png"}],
               "images": [{"file": "a.png", "prompt": "p"}]}
        cues = [{"index": 1, "start": "00:00:00,000", "end": "00:00:02,000",
                "text": "hi"}]
        with mock.patch.object(studio, "llm_generate",
                               return_value=reply):
            review = studio.review_shotlist(self.cfg, data, cues, None)
        self.assertEqual(review["matched"], 1)
        self.assertEqual(review["weak"], [])


if __name__ == "__main__":
    unittest.main()
