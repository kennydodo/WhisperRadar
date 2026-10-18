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
        for hold in (5, 6):
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


class ChannelToleranceTests(unittest.TestCase):
    """The channel's custom motion box sets the tolerances; 0 means strict."""

    def _prof(self, **tol):
        spec = {"allowed": ["ST", "ZI", "ZO"], "st_max_hold": 5, **tol}
        return briefs.custom_profile(spec)

    def test_empty_fields_use_the_standard_tolerances(self):
        prof = self._prof()
        self.assertEqual((prof.max_hold_tol, prof.st_hold_tol, prof.static_hold_tol),
                         (0.2, 0.2, 0.2))

    def test_percent_values_are_stored_as_shares_and_zero_is_kept(self):
        prof = self._prof(tol_max="10", tol_st="0", tol_static="30")
        self.assertEqual((prof.max_hold_tol, prof.st_hold_tol, prof.static_hold_tol),
                         (0.1, 0.0, 0.3))

    def test_strict_st_tolerance_flags_a_six_second_still(self):
        loose, strict = self._prof(), self._prof(tol_st="0")
        flag = lambda p: [f for f in _one(6, "ST", profile=p)
                          if "ST shots hold longer" in f]
        self.assertEqual(flag(loose), [])
        self.assertEqual(len(flag(strict)), 1)

    def test_the_skeleton_follows_the_channel_tolerance(self):
        from whisperradar import skeleton
        self.assertTrue(skeleton.st_fits(5.9, self._prof()))
        self.assertFalse(skeleton.st_fits(6.5, self._prof()))
        self.assertTrue(skeleton.st_fits(6.5, self._prof(tol_st="40")))
        self.assertFalse(skeleton.st_fits(5.9, self._prof(tol_st="10")))
