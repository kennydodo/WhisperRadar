"""Share caps (ST, a code, a code group) have a 2 point tolerance; ZI/ZO split
stays strict. 10.05% ST against a ~10% cap is not a fault."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from whisperradar import studio, briefs  # noqa: E402


def _ts(t):
    m, s = divmod(int(t), 60)
    return f"00:{m:02d}:{s:02d},000"


def _plan(n_st, n_total=100):
    cues = [{"index": i + 1, "start": _ts(i * 3), "end": _ts((i + 1) * 3),
             "text": f"c{i + 1}"} for i in range(n_total)]
    shots, images = [], []
    for i in range(n_total):
        m = "ST" if i < n_st else ("ZI" if i % 2 else "ZO")
        a = f"S01_{i + 1:02d}_SCN_{m}.png"
        shots.append({"cues": f"{i + 1}-{i + 1}", "asset": a, "scene": "S01",
                      "motion": m})
        images.append({"file": a, "prompt": f"p{i}"})
    return {"shots": shots, "images": images}, cues


def _st_faults(n_st):
    data, cues = _plan(n_st)
    faults, _ = studio.shotlist_pacing(data, cues)
    return [f for f in faults if f.startswith("ST is ")]


class ToleranceTests(unittest.TestCase):
    def test_within_two_points_is_not_a_fault(self):
        self.assertEqual(briefs.SHARE_TOLERANCE, 0.02)
        self.assertEqual(_st_faults(10), [])
        self.assertEqual(_st_faults(12), [])

    def test_beyond_two_points_still_is(self):
        self.assertTrue(_st_faults(13))


if __name__ == "__main__":
    unittest.main()
