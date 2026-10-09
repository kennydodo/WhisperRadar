import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import timing  # noqa: E402


def it(day, hour, mult, short=False):
    return {"published_at": f"2026-09-{day:02d}T{hour:02d}:00:00+00:00",
            "multiplier": mult, "is_short": short}


class TimingTests(unittest.TestCase):
    def test_best_weekday_by_median_and_min_sample(self):
        # 2026-09-07 is a Monday, 09-08 Tuesday
        items = [it(7, 10, 1.0), it(14, 10, 1.2), it(21, 10, 0.9),
                 it(8, 15, 5.0), it(15, 15, 6.0), it(22, 15, 4.0),
                 it(9, 3, 50.0)]                       # lone Wed: left out
        wd = timing.slots(items)
        self.assertEqual([s["label"] for s in wd], ["Tue", "Mon"])
        self.assertEqual(wd[0]["median"], 5.0)
        self.assertEqual(timing.slots(items, "hour")[0]["label"], "15:00")

    def test_shorts_and_undated_are_ignored_and_not_ready_is_honest(self):
        items = [it(7, 10, 9.0, short=True)] * 5 + [
            {"published_at": None, "multiplier": 5}] * 5
        out = timing.best(items)
        self.assertFalse(out["ready"])
        self.assertIn("Not enough", out["note"])


if __name__ == "__main__":
    unittest.main()
