"""A judge's reply counts as a verdict only with that stage's fields, so an
older verdict still on a continued chat is never taken for the new answer."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from whisperradar import webstages as w  # noqa: E402

SCRIPT = '{"score": 9.5, "criteria": {}, "claims": [], "must_fix": []}'
SHOTLIST = ('{"faults": [], "shots": [], "detailed_ratio": 1.0, '
            '"pass": true, "fixes": []}')


class VerdictShapeTests(unittest.TestCase):
    def test_shotlist_verdict_needs_faults_and_pass(self):
        self.assertTrue(w._is_shotlist_verdict(SHOTLIST))
        self.assertFalse(w._is_shotlist_verdict(SCRIPT))
        self.assertFalse(w._is_shotlist_verdict('{"faults": []}'))
        self.assertFalse(w._is_shotlist_verdict("no json here"))

    def test_script_verdict_needs_a_score(self):
        self.assertTrue(w._is_script_verdict(SCRIPT))
        self.assertFalse(w._is_script_verdict(SHOTLIST))


if __name__ == "__main__":
    unittest.main()
