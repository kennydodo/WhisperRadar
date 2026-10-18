"""Hold limits (seconds) have a tolerance: with an ST maximum of 5 s a 6-7 s ST
shot passes, 7.5 s does not. The same idea covers the per-shot maximum hold and
the long-static floor. Share caps have their own tolerance (test_share_tolerance)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from whisperradar import studio, briefs  # noqa: E402


def _ts(t):
    m, s = divmod(int(t), 60)
    return f"00:{m:02d}:{s:02d},000"


def _one(hold, motion, max_hold=12.0, profile=None):
    """One shot of `hold` seconds with `motion`, plus a filler ZI shot."""
    cues = [{"index": 1, "start": _ts(0), "end": _ts(hold), "text": "a"},
            {"index": 2, "start": _ts(hold), "end": _ts(hold + 3), "text": "b"}]
    shots = [{"cues": "1-1", "asset": f"S01_01_SCN_{motion}.png",
              "scene": "S01", "motion": motion},
             {"cues": "2-2", "asset": "S01_02_SCN_ZI.png", "scene": "S01",
              "motion": "ZI"}]
    images = [{"file": s["asset"], "prompt": "p"} for s in shots]
    faults, _ = studio.shotlist_pacing(
        {"shots": shots, "images": images}, cues, max_hold, profile)
    return faults


def _profile(st_max_hold=5.0, static_long_hold=None):
    prof = briefs.get_profile("standard")
    return prof.__class__(**{**prof.__dict__, "st_max_hold": st_max_hold,
                             "static_long_hold": static_long_hold,
                             "st_max_share": None, "code_max_share": None,
                             "min_hold": None})


class HoldToleranceTests(unittest.TestCase):
    def test_limit_helper(self):
        self.assertAlmostEqual(briefs.hold_limit(5, 0.4), 7.0)
        self.assertAlmostEqual(briefs.hold_limit(12, 0.2), 14.4)

    def test_st_hold_within_tolerance_passes(self):
        prof = _profile(5.0)
        for hold in (5, 6, 7):
            self.assertEqual(
                [f for f in _one(hold, "ST", profile=prof)
                 if "ST shots hold longer" in f], [], hold)

    def test_st_hold_beyond_tolerance_is_a_fault(self):
        prof = _profile(5.0)
        found = [f for f in _one(8, "ST", profile=prof)
                 if "ST shots hold longer" in f]
        self.assertEqual(len(found), 1)
        self.assertIn("5s", found[0])        # the writer is told the nominal limit

    def test_max_hold_tolerance(self):
        prof = _profile(None)
        self.assertEqual(
            [f for f in _one(7, "ZI", max_hold=6.0, profile=prof)
             if "maximum" in f], [])           # 7 <= 6 * 1.2
        self.assertTrue(
            [f for f in _one(8, "ZI", max_hold=6.0, profile=prof)
             if "maximum" in f])

    def test_long_static_floor_has_tolerance(self):
        prof = _profile(None, static_long_hold=6.0)
        self.assertEqual([f for f in _one(7, "ST", profile=prof)
                          if "long STATIC" in f], [])    # 7 < 6 * 1.2
        self.assertTrue([f for f in _one(8, "ST", profile=prof)
                         if "long STATIC" in f])


if __name__ == "__main__":
    unittest.main()
