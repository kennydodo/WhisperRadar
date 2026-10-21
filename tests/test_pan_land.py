"""PU/PD `land`: the tilt ends on the subject, not on the picture's edge."""
import unittest

from whisperradar import briefs
from whisperradar.video import models as m
from whisperradar.video import shotlist as sl
from whisperradar.video.motion_engine import MotionEngine
from whisperradar.video.options import MotionOptions, OutputOptions

W = H = 1024


def _plan(motion, land):
    eng = MotionEngine(MotionOptions(), OutputOptions(width=1376, height=768))
    return eng.plan_clip(motion, m.EXPLICIT_CODE, True, W, H, duration_seconds=4.0, land=land)


class EngineTests(unittest.TestCase):
    def test_no_land_sweeps_the_whole_picture(self):
        p = _plan(m.PAN_DOWN, None)
        self.assertEqual(0, p.start.y)
        self.assertAlmostEqual(H - p.start.height, p.end.y)

    def test_pd_land_ends_with_the_subject_centred(self):
        p = _plan(m.PAN_DOWN, 60)
        self.assertAlmostEqual(0.60 * H, p.end.y + p.end.height / 2, places=3)
        self.assertLess(p.end.y, H - p.end.height)       # stopped before the edge

    def test_pu_land_ends_with_the_subject_centred(self):
        p = _plan(m.PAN_UP, 35)
        self.assertAlmostEqual(0.35 * H, p.end.y + p.end.height / 2, places=3)
        self.assertGreater(p.end.y, 0)

    def test_land_past_the_edge_is_clamped_to_the_edge(self):
        p = _plan(m.PAN_DOWN, 95)
        self.assertAlmostEqual(H - p.end.height, p.end.y)

    def test_land_against_the_direction_is_ignored_with_a_warning(self):
        p = _plan(m.PAN_UP, 90)                    # a PU cannot land low
        self.assertEqual(0, p.end.y)
        self.assertTrue(p.warnings)


class ParseTests(unittest.TestCase):
    def test_land_is_a_sibling_of_motion_or_inside_it(self):
        a = sl._parse_motion("PD", 70)
        b = sl._parse_motion({"type": "PD", "land": 70})
        self.assertEqual((70.0, 70.0), (a.land, b.land))

    def test_bad_values_are_ignored(self):
        for bad in (-1, 101, "high", True, None):
            self.assertIsNone(sl._parse_motion("PD", bad).land)


class BriefTests(unittest.TestCase):
    def test_writer_brief_and_judge_know_the_field_only_where_tilts_exist(self):
        self.assertIn("`land`", briefs.STANDARD.slot_values()["MOTION_FIELD_RULE"])
        zooms = briefs.ZOOMS_ONLY
        self.assertNotIn("`land`", zooms.slot_values()["MOTION_FIELD_RULE"])


if __name__ == "__main__":
    unittest.main()
