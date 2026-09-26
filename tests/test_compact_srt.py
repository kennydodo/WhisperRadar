"""compact_srt: the LLM-only narration projection (timestamps dropped).

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
    def test_drops_timestamps_keeps_numbers_and_text(self):
        out = studio.compact_srt(SRT)
        self.assertEqual(
            out,
            "1: Hello world.\n"
            "2: This is a second cue that wraps a line.\n"
            "3: Final cue.")
        self.assertNotIn("-->", out)
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


if __name__ == "__main__":
    unittest.main()
