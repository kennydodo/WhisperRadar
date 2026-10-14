"""PU/PD still pans must pace with the shot: travel grows with duration
instead of one fixed 4% sweep (short shots sprinting), and short vertical
pans ease OUT - start immediately, land gently - instead of the
ease_in_out mid-shot surge."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar.video import models as m
from whisperradar.video.motion_engine import MotionEngine
from whisperradar.video.options import MotionOptions, OutputOptions


def plan(seconds, mo=None, w=1024, h=1024, motion=m.PAN_UP, source=m.AUTO_SELECTED):
    engine = MotionEngine(mo or MotionOptions(), OutputOptions(width=1376, height=768))
    return engine.plan_clip(motion, source, True, w, h, duration_seconds=seconds)


class PanTravelPacingTests(unittest.TestCase):
    def setUp(self):
        self.mo = MotionOptions()

    def _travel(self, p):
        return abs(p.end.y - p.start.y)

    def test_short_shot_travels_less_not_the_full_4_percent(self):
        """A 3s PU/PD must crawl (~12px at 4px/s), not sweep the fixed 30.7px."""
        self.assertEqual(self._travel(plan(3.0, self.mo)), 12.0)

    def test_travel_grows_with_duration_until_the_cap(self):
        t6 = self._travel(plan(6.0, self.mo))
        t7 = self._travel(plan(7.0, self.mo))
        self.assertAlmostEqual(t6, 24.0)
        self.assertAlmostEqual(t7, self.mo.pan_target_speed_px_per_second * 7.0)
        self.assertGreater(t7, t6)

    def test_travel_is_capped_at_pan_max_travel_percent(self):
        """A 20s shot stops at the 4% cap (30.7px), like the old behavior."""
        self.assertAlmostEqual(self._travel(plan(20.0, self.mo)), 30.72)

    def test_travel_never_exceeds_the_overscan(self):
        """A tall-but-shallow image: the cap exceeds the spare - stay inside."""
        # 1200x900: fit height 670 -> spare_y = 230 > MIN_TRAVEL, below the cap
        self.assertLessEqual(self._travel(plan(6.0, self.mo, w=1200, h=900)), 230.0)

    def test_explicit_code_keeps_the_full_edge_to_edge_travel(self):
        """A filename-coded PU/PD composes the image for a full sweep - keep it."""
        self.assertAlmostEqual(self._travel(plan(3.0, self.mo, source=m.EXPLICIT_CODE)),
                               452.4651, places=3)

    def test_horizontal_pans_keep_the_fixed_travel(self):
        """The pacing rule is for PU/PD only; PL/PR stay as before (a wide
        2000x1000 image has real horizontal overscan to pan across; their
        cap is 4% of the output WIDTH = 55px)."""
        engine = MotionEngine(self.mo, OutputOptions(width=1376, height=768))
        p = engine.plan_clip(m.PAN_RIGHT, m.AUTO_SELECTED, True, 2000, 1000,
                             duration_seconds=3.0)
        self.assertEqual(p.motion, m.PAN_RIGHT)
        self.assertAlmostEqual(abs(p.end.x - p.start.x),
                               self.mo.pan_max_travel_percent / 100.0 * 1376)


class PanEasingTests(unittest.TestCase):
    def _easing(self, seconds, motion=m.PAN_UP):
        from whisperradar.video.manifest_planner import _resolve_easing
        return _resolve_easing(motion, seconds, MotionOptions())

    def test_short_vertical_pan_eases_out(self):
        self.assertEqual(self._easing(5.0), m.EASE_OUT)
        self.assertEqual(self._easing(9.9, m.PAN_DOWN), m.EASE_OUT)

    def test_long_vertical_pan_stays_linear(self):
        self.assertEqual(self._easing(10.1), m.LINEAR)

    def test_other_motions_keep_the_default(self):
        self.assertEqual(self._easing(5.0, m.ZOOM_IN), m.EASE_IN_OUT)
        self.assertEqual(self._easing(5.0, m.PAN_LEFT), m.EASE_IN_OUT)


if __name__ == "__main__":
    unittest.main()
