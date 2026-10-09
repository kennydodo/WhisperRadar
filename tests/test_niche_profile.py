import unittest

from whisperradar import niche_profile as np_


def item(title, ch, mult=6.0, genre="Personal Finance"):
    return {"title": title, "channel_id": ch, "channel_name": ch,
            "multiplier": mult, "genre": genre, "is_short": False}


class PoolTests(unittest.TestCase):
    def test_source_channel_first_and_per_channel_cap(self):
        items = [item(f"POV: You did thing {i}", "pov", 100 - i)
                 for i in range(10)]
        items += [item("10 Things Wealthy People Notice", "alicia", 5)]
        pool = np_.pool(items, "Personal Finance", "alicia")
        self.assertEqual(pool[0]["channel_id"], "alicia")
        self.assertEqual(sum(i["channel_id"] == "pov" for i in pool),
                         np_.PER_CHANNEL)

    def test_source_title_is_excluded(self):
        items = [item("Nobody Wants Your Stuff", "a"), item("Other one", "a")]
        pool = np_.pool(items, "Personal Finance", "a", "nobody wants your stuff!")
        self.assertEqual([i["title"] for i in pool], ["Other one"])

    def test_other_genres_stay_out_when_the_genre_has_enough(self):
        items = [item(f"Money title {i}", f"c{i}") for i in range(8)]
        items.append(item("Cat story", "z", 50, genre="Human & Animal"))
        pool = np_.pool(items, "Personal Finance")
        self.assertNotIn("Cat story", [i["title"] for i in pool])


class MeasureTests(unittest.TestCase):
    def test_measure(self):
        m = np_.measure(["POV: You quit", "POV: You win", "10 Things you miss",
                         "Why money works"])
        self.assertEqual(m["n"], 4)
        self.assertEqual(m["openers"], ["pov: you"])
        self.assertEqual(m["pct_count"], 25)
        self.assertEqual(m["pct_split"], 50)

    def test_finance_playbook_matches_the_genre(self):
        self.assertTrue(np_.playbook_for("Personal Finance"))
        self.assertIsNone(np_.playbook_for("Human & Animal"))


class PromptTests(unittest.TestCase):
    def test_finance_block_names_core_values(self):
        prof = np_.build([item("A title", "x")], "Personal Finance")
        block = np_.prompt_block(prof)
        self.assertIn("Core values", block)
        self.assertIn("Avoid:", block)

    def test_one_channels_signature_is_not_the_niche(self):
        items = [item(f"POV: You win {i}", "pov", 50 - i) for i in range(4)]
        items += [item(f"Other money {i}", "b", 9 - i) for i in range(4)]
        block = np_.prompt_block(np_.build(items, "Personal Finance"))
        self.assertNotIn("pov: you", block)   # only 2 channels

    def test_no_playbook_no_block_for_unknown_niche(self):
        self.assertEqual(np_.prompt_block(np_.build([], "Human & Animal")), "")


if __name__ == "__main__":
    unittest.main()
