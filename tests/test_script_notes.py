"""The research-notes pass that stops the script echoing the source.

The first live run produced a script with 93.5% 5-gram overlap with the source
transcript (the copycat gate rejects over 20%), because the writer was handed the
transcript prose as "facts". The writer now composes from cached, neutral notes.

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


class ResearchNotesTests(unittest.TestCase):
    def test_notes_are_generated_once_and_cached(self):
        with tempfile.TemporaryDirectory() as d:
            pdir = Path(d)
            calls = []

            def fake_llm(cfg, prompt, provider=None, max_tokens=None):
                calls.append(prompt)
                return "- the woman lives in Nagoya\n- the routine takes 15 minutes"

            with mock.patch.object(studio, "llm_generate", fake_llm):
                first = autorun._research_notes(object(), pdir, "T", "Lifestyle",
                                                "raw transcript text", "deepseek")
                second = autorun._research_notes(object(), pdir, "T", "Lifestyle",
                                                 "raw transcript text", "deepseek")

            self.assertEqual(len(calls), 1, "the second call must use the cache")
            self.assertEqual(first, second)
            self.assertTrue((pdir / autorun.RESEARCH_NOTES_FILE).exists())

    def test_a_failed_notes_call_falls_back_to_the_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            def boom(*a, **k):
                raise RuntimeError("provider down")

            with mock.patch.object(studio, "llm_generate", boom):
                out = autorun._research_notes(object(), Path(d), "T", "g",
                                              "raw transcript text", None)
            self.assertEqual(out, "raw transcript text")

    def test_both_prompts_forbid_five_word_reuse(self):
        self.assertIn("five or more consecutive words",
                      studio.notes_prompt("T", "g", "transcript"))
        self.assertIn("five or more consecutive words",
                      studio.script_prompt("T", "g", "facts"))


if __name__ == "__main__":
    unittest.main()
