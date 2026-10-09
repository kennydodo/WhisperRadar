"""The keyword brief that replaces the research notes.

The writer must never see the original's facts, names or wording (scripts came
out as copies of it). It gets a <=100-word brief plus keywords, cached per
production, and researches the rest itself.

Run: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import autorun, studio  # noqa: E402

GOOD = ("BRIEF: How compound interest quietly builds wealth over decades and "
        "why starting early matters.\n"
        "KEYWORDS: compound interest, wealth, saving early, retirement, "
        "investing, index funds")


class BriefTests(unittest.TestCase):
    def test_generated_once_and_cached(self):
        with wr_tmp.tempdir() as d:
            calls = []

            def fake_llm(cfg, prompt, provider=None, max_tokens=None):
                calls.append(prompt)
                return GOOD

            with mock.patch.object(studio, "llm_generate", fake_llm):
                a = autorun._research_notes(object(), Path(d), "T", "g",
                                            "raw transcript text", "x")
                b = autorun._research_notes(object(), Path(d), "T", "g",
                                            "raw transcript text", "x")
            self.assertEqual(len(calls), 1)
            self.assertEqual(a, b)
            self.assertIn("KEYWORDS:", a)

    def test_failure_gives_nothing_never_the_transcript(self):
        def boom(*a, **k):
            raise RuntimeError("provider down")

        with wr_tmp.tempdir() as d, mock.patch.object(studio, "llm_generate", boom):
            out = autorun._research_notes(object(), Path(d), "T", "g",
                                          "raw transcript text", None)
        self.assertEqual(out, "")

    def test_old_fact_notes_are_rebuilt_manual_ones_kept(self):
        with wr_tmp.tempdir() as d:
            pdir = Path(d)
            (pdir / autorun.RESEARCH_NOTES_FILE).write_text(
                "- Daniel Hargrove ran Apex Corp\n", encoding="utf-8")
            calls = []

            def fake_llm(cfg, prompt, provider=None, max_tokens=None):
                calls.append(1)
                return GOOD

            with mock.patch.object(studio, "llm_generate", fake_llm):
                out = autorun._research_notes(object(), pdir, "T", "g", "src", None)
            self.assertEqual(len(calls), 1)
            self.assertNotIn("Hargrove", out)
            autorun.save_manual_notes(pdir, "my own notes")
            with mock.patch.object(studio, "llm_generate", fake_llm):
                out = autorun._research_notes(object(), pdir, "T", "g", "src", None)
            self.assertEqual(out, "my own notes")

    def test_changed_source_rebuilds(self):
        with wr_tmp.tempdir() as d:
            calls = []

            def fake_llm(cfg, prompt, provider=None, max_tokens=None):
                calls.append(1)
                return GOOD

            with mock.patch.object(studio, "llm_generate", fake_llm):
                autorun._research_notes(object(), Path(d), "T", "g", "one", None)
                autorun._research_notes(object(), Path(d), "T", "g",
                                        "a different source text", None)
            self.assertEqual(len(calls), 2)

    def test_overlong_or_copying_brief_is_retried_then_cut(self):
        src = "the quick brown fox jumps over the lazy dog every single morning"
        replies = iter([
            "BRIEF: the quick brown fox jumps over it\nKEYWORDS: a, b, c, d, e",
            "BRIEF: " + "word " * 140 + "\nKEYWORDS: a, b, c, d, e, f",
            "BRIEF: " + "word " * 140 + "\nKEYWORDS: a, b, c, d, e, f"])
        out = autorun.make_brief(lambda p: next(replies), src, "T", "g")
        summary = studio.parse_brief(out)["summary"]
        self.assertLessEqual(len(summary.split()), studio.BRIEF_WORDS)

    def test_prompts(self):
        p = studio.notes_prompt("T", "g", "transcript")
        self.assertIn("100 words", p)
        self.assertIn("KEYWORDS", p)
        self.assertIn("No names of the video's characters", p)
        w = studio.script_prompt("T", "g", GOOD)
        self.assertIn("You have NOT been given any existing script", w)
        self.assertIn("compound interest", w)

    def test_parse_and_faults(self):
        b = studio.parse_brief(GOOD)
        self.assertEqual(len(b["keywords"]), 6)
        self.assertEqual(studio.brief_faults(b), [])
        self.assertTrue(studio.brief_faults({"summary": "", "keywords": []}))


if __name__ == "__main__":
    unittest.main()
