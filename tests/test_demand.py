import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import demand  # noqa: E402


class DemandTests(unittest.TestCase):
    def test_suggestions_parse_and_fail_soft(self):
        raw = json.dumps(["coin jar", ["coin jar", "coin jar ideas"]])
        self.assertEqual(demand.suggestions("coin jar", lambda u: raw),
                         ["coin jar", "coin jar ideas"])
        def boom(u):
            raise OSError("offline")
        self.assertEqual(demand.suggestions("x", boom), [])
        self.assertEqual(demand.suggestions("  ", lambda u: raw), [])

    def test_title_counts(self):
        items = [{"title": "The coin jar rule", "multiplier": 6},
                 {"title": "Coin jar tips", "multiplier": 1},
                 {"title": "Other", "multiplier": 9}]
        self.assertEqual(demand.title_counts(items, "coin jar"), (1, 2))

    def test_score_grows_with_evidence(self):
        none = demand.score("zzz", [], [])
        self.assertEqual((none["score"], none["label"]), (0, "no signal"))
        sugg = ["coin jar"] + [f"coin jar {i}" for i in range(9)]
        items = [{"title": f"coin jar {i}", "multiplier": 5}
                 for i in range(7)]
        strong = demand.score("coin jar", sugg, items)
        self.assertEqual(strong["label"], "strong")
        self.assertLessEqual(strong["score"], 100)
        weak = demand.score("coin jar", ["coin jar x"], [])
        self.assertLess(weak["score"], strong["score"])


if __name__ == "__main__":
    unittest.main()
