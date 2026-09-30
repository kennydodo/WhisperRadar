"""Cheap-continuation fix for a script that stops mid-sentence/mid-word.

On a long single-pass generation the model sometimes stops before reaching a
real ending - independent of script_max_tokens's headroom (confirmed on real
attempts that cut off at word counts LOWER than other attempts that finished
cleanly). The judge's `ending` criterion scores that a flat 1 regardless of
how good the rest of the draft is, which craters the whole attempt for a
reason that has nothing to do with writing quality.

script_looks_truncated detects it; script_continuation_prompt asks for just
the rest, mirroring the shotlist's own tail-continuation approach.

Run: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import studio  # noqa: E402


class ScriptLooksTruncatedTests(unittest.TestCase):
    def test_a_sentence_ending_in_a_period_is_complete(self):
        self.assertFalse(studio.script_looks_truncated("And that's the end."))

    def test_a_sentence_ending_in_a_question_mark_is_complete(self):
        self.assertFalse(studio.script_looks_truncated(
            "So why does this even matter?"))

    def test_a_sentence_ending_in_an_exclamation_mark_is_complete(self):
        self.assertFalse(studio.script_looks_truncated("What a story!"))

    def test_a_quoted_sentence_end_is_complete(self):
        self.assertFalse(studio.script_looks_truncated('He said "no."'))

    def test_trailing_markdown_emphasis_does_not_hide_a_real_ending(self):
        self.assertFalse(studio.script_looks_truncated("*Subscribe now.*"))

    def test_a_mid_word_cutoff_is_truncated(self):
        self.assertTrue(studio.script_looks_truncated(
            "the domestication window might be catast"))

    def test_a_mid_sentence_cutoff_is_truncated(self):
        self.assertTrue(studio.script_looks_truncated(
            "Cheetahs have never crossed that"))

    def test_trailing_whitespace_is_ignored(self):
        self.assertFalse(studio.script_looks_truncated("Done.\n\n  "))

    def test_empty_text_is_truncated(self):
        self.assertTrue(studio.script_looks_truncated(""))
        self.assertTrue(studio.script_looks_truncated("   "))

    def test_a_trailing_comma_is_truncated(self):
        self.assertTrue(studio.script_looks_truncated(
            "and that changes everything,"))


class ScriptContinuationPromptTests(unittest.TestCase):
    def test_carries_the_base_prompt_and_the_cut_off_tail(self):
        prompt = studio.script_continuation_prompt(
            "BASE PROMPT", "...and then it might be catast")
        self.assertIn("BASE PROMPT", prompt)
        self.assertIn("might be catast", prompt)
        self.assertIn("CUT OFF", prompt)

    def test_asks_for_one_ending_only(self):
        prompt = studio.script_continuation_prompt("BASE", "partial")
        self.assertIn("ONE", prompt)

    def test_only_the_tail_of_a_long_partial_is_sent(self):
        long_partial = "x" * 5000
        prompt = studio.script_continuation_prompt("BASE", long_partial)
        # the base prompt already carries the facts/style guide - resending
        # the whole partial script every continuation round would blow up
        # the prompt on a long draft, so only a bounded tail is included
        self.assertLess(prompt.count("x"), 5000)


if __name__ == "__main__":
    unittest.main()
