"""Similar channels: term-profile overlap between watched channels."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import similar  # noqa: E402


def vid(i, ch, title):
    return {"video_id": f"v{ch}{i}", "channel_id": ch, "channel_name": ch,
            "genre": "g", "title": title, "url": "u", "views": 1000,
            "multiplier": 1.0, "age_days": 10.0, "breakout": False}


def channel(cid, titles):
    return [vid(i, cid, t) for i, t in enumerate(titles)]


HOME_A = ["10 japanese home habits", "why japanese homes stay clean",
          "japanese declutter rules that work", "the box rule for a "
          "clutter free home", "how to keep a small japanese home tidy",
          "morning routines in a japanese home"]
HOME_B = ["japanese habits for a clean home", "declutter like the japanese",
          "keep your home clutter free the japanese way", "small home "
          "storage habits from japan", "japanese cleaning routine for a "
          "tidy home", "home habits that stop clutter"]
FIN = ["dividend investing for beginners", "compound interest explained",
       "how to start investing with little money", "best index funds "
       "for passive income", "recession proof your portfolio",
       "passive income ideas that still work"]


class SimilarTests(unittest.TestCase):
    def setUp(self):
        self.items = (channel("A", HOME_A) + channel("B", HOME_B)
                      + channel("F", FIN))

    def test_home_channel_matches_home_channel_not_finance(self):
        got = similar.similar_to(self.items, "A")
        self.assertEqual(got[0]["channel_id"], "B")
        self.assertTrue(0.0 < got[0]["score"] <= 1.0)
        self.assertTrue(set(got[0]["shared"]) & {"japanese", "home",
                                                 "habits", "clutter"})

    def test_a_channel_is_never_similar_to_itself(self):
        ids = [s["channel_id"] for s in similar.similar_to(self.items, "A")]
        self.assertNotIn("A", ids)

    def test_small_channels_are_not_profiles(self):
        items = self.items + channel("S", ["japanese home habits",
                                            "japanese home rules"])
        self.assertEqual(similar.similar_to(items, "S"), [])
        self.assertNotIn("S", similar.all_similar(items))

    def test_all_similar_covers_every_big_channel(self):
        got = similar.all_similar(self.items)
        self.assertEqual(set(got), {"A", "B", "F"})
        self.assertEqual(got["B"][0]["channel_id"], "A")

    def test_unknown_channel_or_tiny_set_gives_nothing(self):
        self.assertEqual(similar.similar_to(self.items, "ZZ"), [])
        self.assertEqual(similar.all_similar(channel("A", HOME_A)), {})

    def test_limit_is_respected(self):
        items = (self.items + channel("C", HOME_B) + channel("D", HOME_B))
        got = similar.similar_to(items, "A", limit=2)
        self.assertEqual(len(got), 2)


if __name__ == "__main__":
    unittest.main()
