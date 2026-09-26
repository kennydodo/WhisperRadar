"""compact_srt: the LLM-only narration projection (timestamps dropped,
per-cue durations kept).

The SRT file on disk must stay untouched - it is what the pacing gate and the
ImgToVideo assembler read for frame-exact cue times.

Run: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import studio  # noqa: E402

SRT = """1
00:00:01,000 --> 00:00:03,500
Hello world.

2
00:00:03,500 --> 00:00:07,000
This is a second cue
that wraps a line.

3
00:00:07,000 --> 00:00:09,000
Final cue.
"""


class CompactSrtTests(unittest.TestCase):
    def test_drops_timestamps_keeps_numbers_durations_and_text(self):
        out = studio.compact_srt(SRT)
        self.assertEqual(
            out,
            "1: Hello world. (3s)\n"
            "2: This is a second cue that wraps a line. (4s)\n"
            "3: Final cue. (2s)")

        self.assertNotIn("00:00", out)

    def test_smaller_than_the_raw_srt(self):
        self.assertLess(len(studio.compact_srt(SRT)), len(SRT))

    def test_cue_numbers_match_parse_srt_cues(self):
        numbers = [int(line.split(":", 1)[0])
                   for line in studio.compact_srt(SRT).splitlines()]
        self.assertEqual(numbers, [c["index"]
                                   for c in studio.parse_srt_cues(SRT)])

    def test_malformed_srt_falls_back_to_raw_text(self):
        self.assertEqual(studio.compact_srt("no cues here"), "no cues here")
        self.assertEqual(studio.compact_srt(""), "")


class PacingNoteTests(unittest.TestCase):
    """The pacing arithmetic is injected into the planning prompt so the
    planner cannot under-plan a long narration (34 shots for ~960s)."""

    def test_prompt_carries_the_pacing_note(self):
        prompt = studio.shotlist_prompt("BRIEF", "1: hello",
                                        pacing_note="narration ~960s; "
                                                    "roughly 81+ shots")
        self.assertIn("PACING MATH", prompt)
        self.assertIn("roughly 81+ shots", prompt)
        # the block is advisory input appended after the brief - the brief
        # itself stays verbatim at the top
        self.assertLess(prompt.index("BRIEF"), prompt.index("PACING MATH"))

    def test_prompt_without_pacing_note(self):
        self.assertNotIn("PACING MATH",
                         studio.shotlist_prompt("BRIEF", "1: hello"))


if __name__ == "__main__":
    unittest.main()
