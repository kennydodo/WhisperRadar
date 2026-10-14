"""The generated-reference naming gate accepts what the writer instructions
tell the writer to produce (CH_/BG_/OBJ_ + CAPS/digits, several words ok)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from whisperradar import studio  # noqa: E402


class RefNameTests(unittest.TestCase):
    def test_accepts_multi_word_names(self):
        for n in ("CH_MAYA", "OBJ_COIN_JAR_02", "CH_HOUSE_CAT",
                  "CH_PAINTED_DOG", "BG_SAVANNA_DUSK", "CH_HOUSECAT"):
            self.assertTrue(studio.REF_NAME_RE.match(n), n)

    def test_still_rejects_bad_names(self):
        for n in ("MAYA", "CH_", "ch_maya", "CH_Maya", "CH__X", "CH_MAYA_",
                  "OBJ coin", "XX_THING", "CH_HOUSE-CAT"):
            self.assertFalse(studio.REF_NAME_RE.match(n), n)


if __name__ == "__main__":
    unittest.main()
