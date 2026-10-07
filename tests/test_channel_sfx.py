"""A channel can set its own reveal sound instead of the built-in pop."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisperradar import briefs  # noqa: E402


class ChannelSfxTests(unittest.TestCase):
    def test_default_is_pop(self):
        prof = briefs.resolve_profile(None, reveal=1)
        self.assertEqual(prof.sfx, "")
        text = briefs.reveal_block(False, prof.sfx)
        self.assertIn('"sfx": "pop"', text)
        self.assertNotIn("rather than `pop`", text)

    def test_the_channels_sound_replaces_pop_in_the_examples(self):
        prof = briefs.resolve_profile(None, reveal=1, sfx="pop_marimba")
        self.assertEqual(prof.sfx, "pop_marimba")
        text = briefs.reveal_block(False, prof.sfx)
        self.assertIn('"sfx": "pop_marimba"', text)
        self.assertIn('["ding", "pop_marimba", "pop_marimba"]', text)
        self.assertIn("rather than `pop`", text)

    def test_pop_itself_is_the_default_not_a_custom_sound(self):
        self.assertEqual(
            briefs.resolve_profile(None, reveal=1, sfx="pop").sfx, "")

    def test_the_plan_text_carries_the_sound(self):
        prof = briefs.resolve_profile(None, reveal=1, sfx="whoosh")
        self.assertIn('"sfx": "whoosh"', briefs.reveal_block(
            prof.reveal_strict, prof.sfx))


if __name__ == "__main__":
    unittest.main()


class SfxReachesTheBriefTests(unittest.TestCase):
    def test_the_sound_is_in_the_brief_without_reveal_shots(self):
        self.assertIn('"sfx": "ding"', briefs.sfx_block("ding"))
        self.assertIn("not `pop`", briefs.sfx_block("ding"))
