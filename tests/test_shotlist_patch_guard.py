"""Helpers for a shotlist patch writer that works from the CURRENT prompt, and
the check that a patch reply only names assets that were asked for. Used by
the web-chat writer loop; the built-in API loop does not call them."""
import unittest

from whisperradar import studio


DATA = {"images": [
    {"file": "S03_04_SCN_PL.png",
     "prompt": "DAY. Genghis Khan on a horse releasing a cheetah."},
    {"file": "S04_01_SCN_ZO.png",
     "prompt": "DAY. Akbar on a throne before ranks of cheetahs."},
    {"file": "S01_01_SCN_ZI.png", "prompt": "DAY. A person."}]}
WEAK = [{"asset": "S03_04_SCN_PL.png", "reason": "names a real person",
         "narration": "Genghis Khan reportedly used them."},
        {"asset": "S04_01_SCN_ZO.png", "reason": "names a real person"}]


class CurrentPromptTests(unittest.TestCase):
    def test_with_current_prompts_adds_text_and_keeps_input(self):
        out = studio.with_current_prompts(WEAK, DATA)
        self.assertIn("releasing a cheetah", out[0]["prompt"])
        self.assertIn("ranks of cheetahs", out[1]["prompt"])
        self.assertNotIn("prompt", WEAK[0])

    def test_unknown_asset_gets_no_prompt(self):
        out = studio.with_current_prompts([{"asset": "X.png"}], DATA)
        self.assertNotIn("prompt", out[0])

    def test_patch_prompt_shows_current_prompt_and_keep_rule(self):
        text = studio.shotlist_patch_prompt(
            studio.with_current_prompts(WEAK, DATA))
        self.assertIn("CURRENT PROMPT: DAY. Genghis Khan", text)
        self.assertIn("START FROM IT", text)
        self.assertIn("Never swap a subject", text)

    def test_patch_prompt_unchanged_without_current_prompts(self):
        text = studio.shotlist_patch_prompt(WEAK)
        self.assertNotIn("CURRENT PROMPT", text)
        self.assertNotIn("START FROM IT", text)


class CheckPatchTests(unittest.TestCase):
    def test_all_good(self):
        good, unk, miss = studio.check_shotlist_patch(
            {"A.png": "x", "B.png": "y"}, ["A.png", "B.png"])
        self.assertEqual(good, {"A.png": "x", "B.png": "y"})
        self.assertEqual((unk, miss), ([], []))

    def test_mislabelled_key_is_unknown_and_asset_missing(self):
        good, unk, miss = studio.check_shotlist_patch(
            {"S03_04_SCN_PL.png": "x", "S03_04_SCN_ZO.png": "y"},
            ["S03_04_SCN_PL.png", "S04_01_SCN_ZO.png"])
        self.assertEqual(list(good), ["S03_04_SCN_PL.png"])
        self.assertEqual(unk, ["S03_04_SCN_ZO.png"])
        self.assertEqual(miss, ["S04_01_SCN_ZO.png"])

    def test_empty_reply_everything_missing(self):
        good, unk, miss = studio.check_shotlist_patch({}, ["A.png"])
        self.assertEqual((good, unk, miss), ({}, [], ["A.png"]))


if __name__ == "__main__":
    unittest.main()
