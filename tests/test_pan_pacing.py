"""Two kinds of pan:
- AUTO-selected pans (no filename code) are ACCENTS: travel grows with shot
  length but stops at pan_max_travel_percent of the output dimension, so a
  short shot doesn't sprint.
- FILENAME-CODED pans (the images composed for a pan) sweep the WHOLE
  overscan band edge-to-edge - the composition travels out of frame - and are
  shaped by EASING, not a distance cap: short pans ease OUT (start fast,
  decelerate to rest) so they "speed in a bit fast and slow down till the
  frame is out of sight" instead of surging mid-shot (ease_in_out).
Pan reveals sweep the whole panorama by design."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar.video import models as m
from whisperradar.video.motion_engine import MotionEngine
from whisperradar.video.options import MotionOptions, OutputOptions, ProjectOptions

OUT_W, OUT_H = 1376, 768
OUT_ASPECT = OUT_W / OUT_H
# a 1:1 canvas (what Flow generates for PU/PD)
SQUARE_FIT_H = 1024 / OUT_ASPECT
SQUARE_SPARE_Y = 1024 - SQUARE_FIT_H
SQUARE_SCALE = SQUARE_FIT_H / OUT_H
# a 3024x1296 wide canvas (what Renderly generates for PL/PR/PV)
WIDE_FIT_W = 1296 * OUT_ASPECT
WIDE_SPARE_X = 3024 - WIDE_FIT_W
WIDE_SCALE = WIDE_FIT_W / OUT_W


def plan(seconds, mo=None, w=1024, h=1024, motion=m.PAN_UP, source=m.AUTO_SELECTED):
    engine = MotionEngine(mo or MotionOptions(), OutputOptions(width=OUT_W, height=OUT_H))
    return engine.plan_clip(motion, source, True, w, h, duration_seconds=seconds)


class AutoPanPacingTests(unittest.TestCase):
    """Auto-selected pans: paced accent clamped at pan_max_travel_percent of
    the OUTPUT dimension (20 px/s x a 3s floor already exceeds 4% of a 768
    frame, so in practice the cap holds - the point is it never sweeps)."""

    def setUp(self):
        self.mo = MotionOptions()

    def _v(self, p):
        return abs(p.end.y - p.start.y)

    def test_auto_travel_is_the_output_accent_cap_in_screen_pixels(self):
        # 1024x2100: fit height 571.5 -> image px per output px = 0.7442
        for dur in (2.0, 3.0, 30.0):
            p = plan(dur, self.mo, w=1024, h=2100)
            self.assertAlmostEqual(self._v(p), 0.04 * OUT_H * SQUARE_SCALE, places=2)

    def test_auto_travel_never_exceeds_the_overscan(self):
        # 1200x900: spare_y = 230 > the cap; a shallow-band image stays inside
        self.assertLessEqual(self._v(plan(30.0, self.mo, w=1200, h=900)), 230.0)


class ExplicitPanSweepTests(unittest.TestCase):
    """Filename-coded pans: full-band edge-to-edge, direction from the code."""

    def setUp(self):
        self.mo = MotionOptions()

    def _v(self, p):
        return abs(p.end.y - p.start.y)

    def _h(self, p):
        return abs(p.end.x - p.start.x)

    def test_explicit_pu_sweeps_the_whole_band_from_the_bottom_edge(self):
        p = plan(3.0, self.mo, source=m.EXPLICIT_CODE)
        self.assertAlmostEqual(p.start.y, SQUARE_SPARE_Y)
        self.assertAlmostEqual(p.end.y, 0.0)
        self.assertAlmostEqual(self._v(p), SQUARE_SPARE_Y)

    def test_explicit_pd_sweeps_the_whole_band_from_the_top_edge(self):
        p = plan(6.0, self.mo, source=m.EXPLICIT_CODE, motion=m.PAN_DOWN)
        self.assertAlmostEqual(p.start.y, 0.0)
        self.assertAlmostEqual(p.end.y, SQUARE_SPARE_Y)
        self.assertAlmostEqual(self._v(p), SQUARE_SPARE_Y)

    def test_explicit_sweep_is_duration_independent(self):
        # distance is fixed by the composition; easing decides the feel
        self.assertAlmostEqual(self._v(plan(3.0, self.mo, source=m.EXPLICIT_CODE)),
                               self._v(plan(20.0, self.mo, source=m.EXPLICIT_CODE)))

    def test_explicit_pr_sweeps_the_whole_horizontal_band(self):
        p = plan(3.0, self.mo, w=3024, h=1296, motion=m.PAN_RIGHT, source=m.EXPLICIT_CODE)
        self.assertAlmostEqual(p.start.x, 0.0)
        self.assertAlmostEqual(p.end.x, WIDE_SPARE_X)

    def test_explicit_pl_starts_on_the_right_edge(self):
        p = plan(3.0, self.mo, w=3024, h=1296, motion=m.PAN_LEFT, source=m.EXPLICIT_CODE)
        self.assertAlmostEqual(p.start.x, WIDE_SPARE_X)
        self.assertAlmostEqual(p.end.x, 0.0)

    def test_pan_reveal_keeps_the_full_sweep(self):
        p = plan(3.0, self.mo, w=3024, h=1296, motion=m.PAN_REVEAL)
        self.assertAlmostEqual(self._h(p), WIDE_SPARE_X)

    def test_no_duration_auto_pan_keeps_legacy_cap(self):
        # the no-duration path stays byte-compatible with C#: the raw 4%
        # output number applied against image pixels, unconverted
        p = plan(None, self.mo, w=1024, h=2100)
        self.assertAlmostEqual(self._v(p), 0.04 * OUT_H, places=2)


class PanEasingTests(unittest.TestCase):
    def _easing(self, seconds, motion=m.PAN_UP):
        from whisperradar.video.manifest_planner import _resolve_easing
        return _resolve_easing(motion, seconds, MotionOptions())

    def test_short_vertical_pan_cruises_then_brakes(self):
        self.assertEqual(self._easing(5.0), m.EASE_OUT_HOLD)
        self.assertEqual(self._easing(9.9, m.PAN_DOWN), m.EASE_OUT_HOLD)

    def test_short_horizontal_pan_cruises_then_brakes(self):
        self.assertEqual(self._easing(5.0, m.PAN_LEFT), m.EASE_OUT_HOLD)
        self.assertEqual(self._easing(9.9, m.PAN_RIGHT), m.EASE_OUT_HOLD)

    def test_hold_curve_shape(self):
        """Constant speed for the first 35%, then decelerate to rest;
        position AND velocity continuous at the switch."""
        from whisperradar.video.render_plan import _easing_value
        from whisperradar.video.models import PAN_HOLD_FRACTION as s
        e = m.EASE_OUT_HOLD
        self.assertAlmostEqual(_easing_value(e, 0.0), 0.0)
        self.assertAlmostEqual(_easing_value(e, 1.0), 1.0)
        # constant speed through the cruise segment: position grows
        # linearly with time up to the switch
        for u in (0.2, 0.5, 1.0):
            self.assertAlmostEqual(_easing_value(e, s * u), u * _easing_value(e, s), places=9)
        # no kink: symmetric difference around the switch
        d0 = (_easing_value(e, s + 1e-5) - _easing_value(e, s - 1e-5)) / 2e-5
        self.assertAlmostEqual(d0, 2.0 / (1.0 + s), places=3)
        # faster than ease_in_out at the start, ends at rest
        self.assertGreater(_easing_value(e, s / 2), _easing_value(m.EASE_IN_OUT, s / 2))
        tail = 1.0 - _easing_value(e, 0.95)
        self.assertLess(tail, 0.05 * (2.0 / (1.0 + s)))  # velocity well below cruise

    def test_long_pan_stays_linear(self):
        self.assertEqual(self._easing(10.1), m.LINEAR)
        self.assertEqual(self._easing(10.1, m.PAN_RIGHT), m.LINEAR)

    def test_pan_reveal_keeps_the_default_easing(self):
        self.assertEqual(self._easing(5.0, m.PAN_REVEAL), m.EASE_IN_OUT)

    def test_other_motions_keep_the_default(self):
        self.assertEqual(self._easing(5.0, m.ZOOM_IN), m.EASE_IN_OUT)
        self.assertEqual(self._easing(5.0, m.STATIC), m.EASE_IN_OUT)


class ManifestFallbackSurfacingTests(unittest.TestCase):
    """A pan whose image has no overscan in that direction must not pretend
    to be a pan: the manifest planner reports the engine's push-in fallback."""

    def test_zero_overscan_pan_degrades_loudly(self):
        from whisperradar.video import manifest_planner
        import types

        shot = types.SimpleNamespace(shot_id="shot-042")
        image = m.ImageInfo("P:\\x\\S01_01_SCN_PL.png",
                            m.ParsedImageName("S01_01_SCN_PL", 1, 1, "PL"),
                            OUT_W, OUT_H)   # exactly the output size: spare_x = 0
        options = ProjectOptions()
        options.output.width, options.output.height = OUT_W, OUT_H
        issues = []
        motion, start, end = manifest_planner._resolve_viewports(
            shot, None, m.PAN_LEFT, m.EXPLICIT_CODE, image, options, issues,
            duration_seconds=4.0)
        self.assertEqual(motion, m.ZOOM_IN)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].code, "VIEWPORT")
        self.assertEqual(issues[0].severity, m.WARNING)
        self.assertIn("shot-042", issues[0].message)
        self.assertIn("No horizontal overscan", issues[0].message)
        self.assertAlmostEqual(start.width, OUT_W)
        self.assertLess(end.width, start.width)

    def test_overscanned_pan_still_pans(self):
        from whisperradar.video import manifest_planner
        import types

        shot = types.SimpleNamespace(shot_id="shot-043")
        image = m.ImageInfo("P:\\x\\S01_01_SCN_PL.png",
                            m.ParsedImageName("S01_01_SCN_PL", 1, 1, "PL"),
                            3024, 1296)
        options = ProjectOptions()
        options.output.width, options.output.height = OUT_W, OUT_H
        issues = []
        motion, start, end = manifest_planner._resolve_viewports(
            shot, None, m.PAN_LEFT, m.EXPLICIT_CODE, image, options, issues,
            duration_seconds=4.0)
        self.assertEqual(motion, m.PAN_LEFT)
        self.assertEqual(issues, [])
        # explicit PL: full-band sweep ends at the left edge (x=0)
        self.assertAlmostEqual(end.x, 0.0)
        self.assertAlmostEqual(start.x, WIDE_SPARE_X)


if __name__ == "__main__":
    unittest.main()
