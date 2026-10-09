import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from whisperradar import studio  # noqa: E402

SOURCE = " ".join([
    "The old pennies from 1943 were made of steel because copper went to war.",
    "Collectors pay high prices when a steel penny is in good condition.",
    "A magnet will quickly show whether a penny is steel or copper.",
    "Mint marks tell you which factory struck the coin.",
    "Rare errors can turn an ordinary penny into a small fortune.",
    "Always store valuable coins in a dry place away from moisture.",
    "Grading services rate the condition of a coin from poor to perfect.",
    "Most people never check the coins in their jars at all."])

COPY = " ".join([
    "Pennies minted in 1943 were steel because copper was needed for the war.",
    "Collectors pay big prices if a steel penny is in good condition.",
    "A magnet quickly shows if a penny is steel or copper.",
    "Mint marks show which factory struck the coin.",
    "Rare errors can make an ordinary penny a small fortune.",
    "Valuable coins belong in a dry place away from moisture.",
    "Grading services rate a coin's condition from poor to perfect.",
    "Most people never check the coins sitting in their jars."])

OWN = " ".join([
    "Your grandfather's jar sits on a shelf and nobody has opened it in years.",
    "Inside, one coin might pay for a weekend trip somewhere warm.",
    "Start with the cheapest tool you own, a kitchen magnet from the fridge.",
    "Sorting by colour takes ten minutes and tells you almost everything.",
    "Then look for tiny letters below the date, because they matter a lot.",
    "Dry storage matters more than any fancy album you could buy online.",
    "If something looks odd, photograph it before you touch it again.",
    "Most jars hold nothing special, yet the checking costs you nothing."])


class StructureCopyTests(unittest.TestCase):
    def test_reworded_retelling_in_the_same_order_is_flagged(self):
        r = studio.structure_copy(COPY, SOURCE)
        self.assertTrue(r["flag"], r)
        self.assertIn("same order", studio.structure_reason(r))

    def test_an_original_script_on_the_same_topic_passes(self):
        self.assertFalse(studio.structure_copy(OWN, SOURCE)["flag"])

    def test_short_or_empty_input_is_never_flagged(self):
        self.assertFalse(studio.structure_copy("", SOURCE)["flag"])
        self.assertFalse(studio.structure_copy(COPY, "")["flag"])

    def test_same_sentences_in_shuffled_order_are_not_flagged(self):
        sents = COPY.split(". ")
        shuffled = ". ".join(sents[::-1])
        self.assertFalse(studio.structure_copy(shuffled, SOURCE)["flag"])


if __name__ == "__main__":
    unittest.main()
