"""A detailed image prompt or a multi-cue narration must reach the judge in
full - not clipped at a small fixed length.

Confirmed as a real bug: the manifest brief lets a single image prompt run
up to ~2400 characters (the batch app's card box), so alignment_prompt used
to clip both the narration and the prompt at [:700], hiding anything past
that point from the judge. A fully detailed prompt that put a required
element (a prop, an action, a spatial detail) after character 700 was
marked "missing" for something that was actually there.

Run: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import studio  # noqa: E402


class AlignmentPromptNoTruncationTests(unittest.TestCase):
    def test_a_long_image_prompt_is_not_clipped(self):
        long_prompt = "detail-" + ("x" * 2400) + "-tail-marker"
        chunk = [{"asset": "S01_01.png", "cues": "1-2", "prompt": long_prompt}]
        prompt = studio.alignment_prompt(chunk, {1: "a", 2: "b"})
        self.assertIn("tail-marker", prompt)

    def test_a_long_multi_cue_narration_is_not_clipped(self):
        cues = {i: f"cue-{i}-text " * 20 for i in range(1, 10)}
        cues[9] = cues[9] + "tail-marker"
        chunk = [{"asset": "S01_01.png", "cues": "1-9", "prompt": "p"}]
        prompt = studio.alignment_prompt(chunk, cues)
        self.assertIn("tail-marker", prompt)


if __name__ == "__main__":
    unittest.main()
