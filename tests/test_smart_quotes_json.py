"""Claude's web page renders a reply's JSON with typographic quotes; the
judge/plan parsers must still read it."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402,F401

from whisperradar import studio  # noqa: E402

CURLY = ('{“score”: 7.5, “pass”: false, “faults”: [“Titles 1, 2 are near-'
         'duplicates; title “Secretly Won” is generic.”, “Thumbnail text '
         '“Secretly Winning” repeats the title.”], “fixes”: [“Set keyword to '
         '“rat race” and keep it first.”, “Use: ‘POV: Everyone Thinks You’re '
         'Losing’ as the lead.”]}')


class SmartQuoteTests(unittest.TestCase):
    def test_curly_quoted_judge_verdict_is_read(self):
        d = studio._parse_json_object("Here you go:\n" + CURLY)
        self.assertEqual(d["score"], 7.5)
        self.assertFalse(d["pass"])
        self.assertEqual(len(d["faults"]), 2)
        self.assertIn("rat race", d["fixes"][0])
        self.assertIn("You’re".replace("’", "'"), d["fixes"][1])

    def test_valid_json_is_untouched(self):
        d = studio._parse_json_object('{"a": "x \\"q\\" y", "b": [1, 2]}')
        self.assertEqual(d, {"a": 'x "q" y', "b": [1, 2]})


if __name__ == "__main__":
    unittest.main()
