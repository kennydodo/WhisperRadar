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


class ChunkedNotesTests(unittest.TestCase):
    """Notes are built part by part so a long transcript can never be cut off
    partway (an 11-rule transcript once produced notes that stopped at rule 7)."""

    def _transcript(self, sentences=400):
        return " ".join(f"Rule {i} says something about habit {i}."
                        for i in range(sentences))

    def test_split_covers_every_word_in_order(self):
        text = self._transcript()
        parts = studio.split_for_notes(text)
        self.assertGreater(len(parts), 1)
        self.assertEqual(" ".join(parts).split(), text.split())
        for part in parts[:-1]:
            self.assertTrue(part.endswith("."), "cuts at a sentence end")

    def test_short_text_is_one_part(self):
        self.assertEqual(studio.split_for_notes("a short transcript."),
                         ["a short transcript."])

    def test_long_source_builds_notes_for_every_part(self):
        text = self._transcript()
        n_parts = len(studio.split_for_notes(text))
        prompts = []

        def fake_llm(cfg, prompt, provider=None, max_tokens=None):
            prompts.append(prompt)
            return f"- fact from call {len(prompts)}"

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(studio, "llm_generate", fake_llm):
            notes = autorun._research_notes(object(), Path(d), "T", "g",
                                            text, None)
        self.assertEqual(len(prompts), n_parts)
        self.assertIn(f"part 1 of {n_parts}", prompts[0])
        for i in range(1, n_parts + 1):
            self.assertIn(f"- fact from call {i}", notes)

    def test_one_empty_part_falls_back_to_the_transcript(self):
        text = self._transcript()
        replies = iter(["- ok", ""])

        def fake_llm(cfg, prompt, provider=None, max_tokens=None):
            return next(replies, "- more")

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(studio, "llm_generate", fake_llm):
            out = autorun._research_notes(object(), Path(d), "T", "g",
                                          text, None)
            self.assertEqual(out, text)
            self.assertFalse((Path(d) / autorun.RESEARCH_NOTES_FILE).exists())

    def test_old_cut_off_notes_are_rebuilt_once(self):
        text = self._transcript()          # thousands of words
        with tempfile.TemporaryDirectory() as d:
            pdir = Path(d)
            (pdir / autorun.RESEARCH_NOTES_FILE).write_text(
                "- a handful of old notes cut off mid-wo\n", encoding="utf-8")
            calls = []

            def fake_llm(cfg, prompt, provider=None, max_tokens=None):
                calls.append(1)
                return "- fresh complete notes for this part"

            with mock.patch.object(studio, "llm_generate", fake_llm):
                autorun._research_notes(object(), pdir, "T", "g", text, None)
                first_calls = len(calls)
                autorun._research_notes(object(), pdir, "T", "g", text, None)
            self.assertGreater(first_calls, 0, "legacy notes must be rebuilt")
            self.assertEqual(len(calls), first_calls,
                             "once rebuilt (meta written) the cache is trusted")

    def test_notes_for_a_changed_source_are_rebuilt(self):
        with tempfile.TemporaryDirectory() as d:
            pdir = Path(d)
            calls = []

            def fake_llm(cfg, prompt, provider=None, max_tokens=None):
                calls.append(1)
                return "- fact"

            with mock.patch.object(studio, "llm_generate", fake_llm):
                autorun._research_notes(object(), pdir, "T", "g", "one text", None)
                autorun._research_notes(object(), pdir, "T", "g",
                                        "a different source text", None)
            self.assertEqual(len(calls), 2)



if __name__ == "__main__":
    unittest.main()
