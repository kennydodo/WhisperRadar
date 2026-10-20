"""Channel transition mix: cut / crossfade / slide shares for the cuts inside scenes."""
import unittest

from tests.test_video_planning import _plan, mplan_ok, mp_options
from tests.video_golden_support import TempProject
from whisperradar.video import models as m
from whisperradar.video import transition_mix as tm
from whisperradar.video import settings_spec as ss
from whisperradar.video.options import ProjectOptions


def _timeline(n_scenes=2, per_scene=20, motions=None):
    scenes = []
    f = 0
    for s in range(n_scenes):
        clips = []
        for i in range(per_scene):
            motion = (motions[(s * per_scene + i) % len(motions)] if motions else m.ZOOM_IN)
            clips.append(m.VideoClip(
                file_path=f"S{s+1:02d}_{i+1:02d}.png", scene_id=f"S{s+1:02d}",
                start_frame=f, duration_frames=90, motion=motion,
                transition=m.TransitionIn("crossfade", 15) if (s or i) else None))
            f += 90
        scenes.append(m.Scene(f"S{s+1:02d}", clips[0].start_frame, f, clips))
    tl = m.Timeline(fps=30.0)
    tl.scenes = scenes
    return tl


def _kinds(tl):
    return [(c.transition.kind if c.transition else None)
            for s in tl.scenes for c in s.clips]


def _slides(o, total):
    t = o.transitions
    t.slide_left_percent = t.slide_right_percent = total / 4
    t.slide_up_percent = t.slide_down_percent = total / 4


def _opts(cut, soft, slide):
    o = ProjectOptions()
    o.transitions.cut_percent, o.transitions.crossfade_percent = cut, soft
    _slides(o, slide)
    return o


class MixTests(unittest.TestCase):
    def test_default_mix_changes_nothing(self):
        tl = _timeline()
        before = _kinds(tl)
        counts = tm.apply(tl, ProjectOptions())
        self.assertEqual(before, _kinds(tl))
        self.assertEqual(0, counts["candidates"])

    def test_shares_are_met_inside_scenes_only(self):
        tl = _timeline()
        c = tm.apply(tl, _opts(30, 40, 30))
        kinds = _kinds(tl)
        # 38 cuts inside scenes (40 clips - first shot - 1 scene boundary)
        self.assertEqual(38, c["candidates"])
        self.assertEqual(c["cut"], sum(1 for k in kinds if k is None) - 1)  # minus the first shot
        slides = [k for k in kinds if k and k.startswith("slide")]
        self.assertEqual(c["slide"], len(slides))
        self.assertAlmostEqual(0.30, c["slide"] / 38, delta=0.04)
        self.assertAlmostEqual(0.30, c["cut"] / 38, delta=0.04)
        self.assertAlmostEqual(0.40, c["soft"] / 38, delta=0.04)
        # the first shot and the scene boundary are untouched
        self.assertIsNone(tl.scenes[0].clips[0].transition)
        self.assertEqual("crossfade", tl.scenes[1].clips[0].transition.kind)

    def test_percentages_are_scaled_not_required_to_sum_to_100(self):
        a, b = _timeline(), _timeline()
        tm.apply(a, _opts(1, 2, 1))
        tm.apply(b, _opts(25, 50, 25))
        self.assertEqual(_kinds(a), _kinds(b))

    def test_slides_rotate_directions_and_skip_still_shots(self):
        tl = _timeline(motions=[m.STATIC, m.ZOOM_IN, m.ZOOM_OUT, m.PAN_UP])
        tm.apply(tl, _opts(0, 50, 50))
        for s in tl.scenes:
            for c in s.clips:
                if c.transition and c.transition.kind.startswith("slide"):
                    self.assertNotEqual(m.STATIC, c.motion)
        kinds = [k for k in _kinds(tl) if k and k.startswith("slide")]
        self.assertEqual({"slide_left", "slide_up", "slide_right", "slide_down"}, set(kinds))

    def test_no_slides_back_to_back_at_a_small_share(self):
        tl = _timeline()
        tm.apply(tl, _opts(0, 60, 40))
        k = [x and x.startswith("slide") for x in _kinds(tl)]
        self.assertFalse(any(a and b for a, b in zip(k, k[1:])))

    def test_fixed_and_reveal_clips_are_left_alone(self):
        tl = _timeline()
        for c in tl.scenes[0].clips[1:6]:
            c.transition_fixed = True
        tl.scenes[0].clips[7].reveal = m.RevealInfo(layout=m.REVEAL_ROW, count=3, visible=1,
                                                     base_shot_id="x")
        tm.apply(tl, _opts(50, 0, 50))
        for c in tl.scenes[0].clips[1:6]:
            self.assertEqual("crossfade", c.transition.kind)
        self.assertEqual("crossfade", tl.scenes[0].clips[7].transition.kind)

    def test_transitions_off_means_no_mix(self):
        tl = _timeline()
        o = _opts(50, 0, 50)
        o.transitions.enabled = False
        before = _kinds(tl)
        tm.apply(tl, o)
        self.assertEqual(before, _kinds(tl))


class DirectionTests(unittest.TestCase):
    def test_each_direction_gets_its_own_value(self):
        o = ProjectOptions()
        o.transitions.crossfade_percent = 50
        o.transitions.slide_left_percent = 30
        o.transitions.slide_up_percent = 20
        tl = _timeline()
        counts = tm.apply(tl, o)
        kinds = [k for k in _kinds(tl) if k]
        self.assertNotIn("slide_right", kinds)
        self.assertNotIn("slide_down", kinds)
        self.assertGreaterEqual(counts["by_kind"]["slide_left"], counts["by_kind"]["slide_up"])
        self.assertGreater(counts["by_kind"]["slide_up"], 0)


class ValidationAndSettingsTests(unittest.TestCase):
    def test_negative_or_all_zero_is_refused(self):
        self.assertTrue(_opts(-1, 50, 50).validate())
        self.assertTrue(_opts(0, 0, 0).validate())
        self.assertFalse(_opts(0, 100, 0).validate())

    def test_settings_page_lists_the_three_shares(self):
        for key in ("cut_percent", "crossfade_percent", "slide_left_percent",
                    "slide_right_percent", "slide_up_percent", "slide_down_percent"):
            self.assertIn("transitions." + key, ss.BY_PATH)
        self.assertEqual(100.0, ss.BY_PATH["transitions.crossfade_percent"]["default"])


class PlannerIntegrationTests(unittest.TestCase):
    def test_manifest_flow_applies_the_mix_and_reports_it(self):
        o = mp_options()
        o.transitions.cut_percent, o.transitions.crossfade_percent = 50, 0
        _slides(o, 50)
        tl, _, issues = mplan_ok(options=o)
        self.assertIn("TRANSITION_MIX", [i.code for i in issues])

    def test_manifest_flow_default_is_unchanged(self):
        _, _, issues = mplan_ok()
        self.assertNotIn("TRANSITION_MIX", [i.code for i in issues])


class FilenameFlowTests(unittest.TestCase):
    def test_filename_flow_applies_the_mix(self):
        p = TempProject()
        self.addCleanup(p.cleanup)
        for name in ("S01_01.png", "S01_02_ZI.png", "S01_03_ZO.png", "S01_04_ZI.png",
                     "S01_05_ZO.png", "S01_06_ZI.png"):
            p.write_image(name, 2304, 1296)
        p.write_srt()
        p.write_audio()
        o = _opts(0, 0, 100)
        res = _plan(p, options=o)
        kinds = _kinds(res.timeline)
        self.assertTrue(any(k and k.startswith("slide") for k in kinds))


if __name__ == "__main__":
    unittest.main()
