"""Ports of the ImgToVideo.Core C# tests: TimingEngine, MotionEngine, ProjectOverrides, EditPlanner,
ProjectLoader, ManifestPlanner and BuildReport (the C# tests are the spec)."""
import dataclasses
import json
import math
import os
import unittest

from whisperradar.video import build_report, edit_planner, manifest_planner, motion_engine
from whisperradar.video import models as m
from whisperradar.video import overrides as ov
from whisperradar.video import project_loader, timing_engine
from whisperradar.video import manifest as mf
from whisperradar.video.options import ProjectOptions
from tests.video_golden_support import TempProject, png_header


def _image(path, width=2304, height=1296, scene=1, index=1, code=None, stem=None, type_=None,
           unknown=False):
    stem = stem or os.path.splitext(os.path.basename(path))[0]
    return m.ImageInfo(path, m.ParsedImageName(stem, scene, index, code, unknown, type_), width, height)


# ====================================================================== TimingEngine
class TimingEngineTests(unittest.TestCase):
    options = ProjectOptions()

    @staticmethod
    def scene(sid, start, end, image_count):
        images = tuple(_image(f"images/{sid}_{i:02d}.png", stem=f"{sid}_{i:02d}", index=i)
                       for i in range(1, image_count + 1))
        return m.ScenePlanInput(sid, start, end, images)

    def plan(self, scenes, audio, overrides=None):
        return timing_engine.plan(scenes, audio, self.options, overrides)

    def test_even_split_when_base_in_range(self):
        result = self.plan([self.scene("S01", 0, 10, 2)], 10.0)
        self.assertEqual(1, len(result.scenes))
        self.assertEqual([150, 150], [c.duration_frames for c in result.scenes[0].clips])
        self.assertFalse([i for i in result.issues if i.severity in (m.WARNING, m.ERROR)])

    def test_sparse_scene_caps_clips_and_holds_the_last(self):
        result = self.plan([self.scene("S01", 0, 20, 2)], 20.0)
        self.assertEqual([240, 360], [c.duration_frames for c in result.scenes[0].clips])
        self.assertIn("TIMING_HOLD", [i.code for i in result.issues])

    def test_dense_scene_below_min_warns(self):
        result = self.plan([self.scene("S01", 0, 6, 3)], 6.0)
        self.assertEqual([60, 60, 60], [c.duration_frames for c in result.scenes[0].clips])
        self.assertIn("TIMING_DENSITY", [i.code for i in result.issues])
        self.assertFalse([i for i in result.issues if i.severity == m.ERROR])

    def test_scene_below_floor_errors(self):
        result = self.plan([self.scene("S01", 0, 6, 4)], 6.0)
        self.assertTrue([i for i in result.issues if i.code == "TIMING_OVERPACKED" and i.severity == m.ERROR])

    def test_scene_without_images_errors(self):
        result = self.plan([m.ScenePlanInput("S01", 0, 10, ())], 10.0)
        self.assertTrue([i for i in result.issues if i.code == "TIMING_NO_IMAGES" and i.severity == m.ERROR])

    def test_paired_full_set_overrides_shift_the_scene_boundary(self):
        scenes = [self.scene("S01", 0, 10, 2), self.scene("S02", 10, 20, 2)]
        overrides = {"images/S01_01.png": 150, "images/S01_02.png": 180,
                     "images/S02_01.png": 120, "images/S02_02.png": 150}
        result = self.plan(scenes, 20.0, overrides)
        self.assertNotIn("TIMING_OVERRIDE_IGNORED", [i.code for i in result.issues])
        s01, s02 = result.scenes
        self.assertEqual([150, 180], [c.duration_frames for c in s01.clips])
        self.assertEqual([120, 150], [c.duration_frames for c in s02.clips])
        self.assertEqual(330, s01.end_frame)
        self.assertEqual(330, s02.start_frame)
        cursor = 0
        for scene in result.scenes:
            for clip in scene.clips:
                self.assertEqual(cursor, clip.start_frame)
                cursor += clip.duration_frames
        self.assertEqual(round(20.0 * 30), cursor)

    def test_unpaired_full_set_delta_is_ignored_with_warning(self):
        scenes = [self.scene("S01", 0, 10, 2), self.scene("S02", 10, 20, 2)]
        overrides = {"images/S01_01.png": 150, "images/S01_02.png": 180}
        result = self.plan(scenes, 20.0, overrides)
        self.assertIn("TIMING_OVERRIDE_IGNORED", [i.code for i in result.issues])
        self.assertEqual([150, 150], [c.duration_frames for c in result.scenes[0].clips])

    def test_scene_boundaries_are_contiguous_and_cover_audio(self):
        result = self.plan([self.scene("S01", 0, 10.05, 2), self.scene("S02", 10.05, 20.05, 2)], 20.05)
        self.assertEqual(2, len(result.scenes))
        self.assertEqual(0, result.scenes[0].start_frame)
        self.assertEqual(result.scenes[0].end_frame, result.scenes[1].start_frame)
        self.assertEqual(round(20.05 * 30), result.scenes[1].end_frame)
        cursor = 0
        for scene in result.scenes:
            for clip in scene.clips:
                self.assertEqual(cursor, clip.start_frame)
                cursor += clip.duration_frames
        self.assertEqual(round(20.05 * 30), cursor)

    def test_remainder_frames_are_distributed_without_gaps(self):
        result = self.plan([self.scene("S01", 0, 7, 3)], 7.0)
        clips = result.scenes[0].clips
        self.assertEqual(210, sum(c.duration_frames for c in clips))
        self.assertEqual([70, 70, 70], [c.duration_frames for c in clips])

    def test_remainder_goes_to_the_first_clips(self):
        result = self.plan([self.scene("S01", 0, 10.1, 2)], 10.1)   # 303 frames
        self.assertEqual([152, 151], [c.duration_frames for c in result.scenes[0].clips])

    def test_explicit_motion_codes_are_carried_through(self):
        scene = m.ScenePlanInput("S01", 0, 10, (
            _image("images/S01_01.png", index=1),
            _image("images/S01_02_PR.png", index=2, code=m.PAN_RIGHT)))
        result = self.plan([scene], 10.0)
        self.assertIsNone(result.scenes[0].clips[0].explicit_motion)
        self.assertEqual(m.PAN_RIGHT, result.scenes[0].clips[1].explicit_motion)

    def test_duration_override_takes_frames_from_siblings(self):
        result = self.plan([self.scene("S01", 0, 10, 2)], 10.0, {"images/S01_01.png": 210})
        self.assertEqual([210, 90], [c.duration_frames for c in result.scenes[0].clips])

    def test_impossible_duration_overrides_are_ignored_with_warning(self):
        result = self.plan([self.scene("S01", 0, 10, 2)], 10.0, {"images/S01_01.png": 300})
        self.assertEqual([150, 150], [c.duration_frames for c in result.scenes[0].clips])
        self.assertIn("TIMING_OVERRIDE_IGNORED", [i.code for i in result.issues])

    def test_overrides_summing_to_exactly_the_scene_are_kept(self):
        result = self.plan([self.scene("S01", 0, 10, 2)], 10.0,
                           {"images/S01_01.png": 100, "images/S01_02.png": 200})
        self.assertEqual([100, 200], [c.duration_frames for c in result.scenes[0].clips])
        self.assertNotIn("TIMING_OVERRIDE_IGNORED", [i.code for i in result.issues])

    def test_messages_use_one_decimal(self):
        result = self.plan([self.scene("S01", 0, 20, 2)], 20.0)
        hold = next(i for i in result.issues if i.code == "TIMING_HOLD")
        self.assertEqual("Scene S01 contains 2 images in 20.0s; the last image holds past the 8.0s maximum.",
                         hold.message)


# ====================================================================== MotionEngine
def _engine():
    o = ProjectOptions()
    return motion_engine.MotionEngine(o.motion, o.output)


def _ctx(explicit, video_start, video_end, scene_start, previous, shots):
    return motion_engine.MotionContext(explicit, video_start, video_end, scene_start, previous, shots)


R = m.Rect


class MotionEngineTests(unittest.TestCase):
    def select(self, ctx):
        return _engine().select_motion(ctx)

    def test_first_shot_of_video_is_zoom_in(self):
        d = self.select(_ctx(None, True, False, True, None, 0))
        self.assertEqual((m.ZOOM_IN, m.AUTO_SELECTED), (d.motion, d.source))

    def test_explicit_code_wins_even_at_video_start(self):
        d = self.select(_ctx(m.PAN_LEFT, True, False, True, None, 0))
        self.assertEqual((m.PAN_LEFT, m.EXPLICIT_CODE), (d.motion, d.source))

    def test_closing_shot_is_static_or_zoom_out(self):
        self.assertEqual(m.STATIC, self.select(_ctx(None, False, True, False, m.PAN_LEFT, 1)).motion)
        self.assertEqual(m.ZOOM_OUT, self.select(_ctx(None, False, True, False, m.STATIC, 1)).motion)

    def test_scene_start_prefer_zoom_in_else_static(self):
        self.assertEqual(m.ZOOM_IN, self.select(_ctx(None, False, False, True, m.PAN_LEFT, 1)).motion)
        self.assertEqual(m.STATIC, self.select(_ctx(None, False, False, True, m.ZOOM_IN, 1)).motion)

    def test_after_pan_left_prefers_static(self):
        self.assertEqual(m.STATIC, self.select(_ctx(None, False, False, False, m.PAN_LEFT, 1)).motion)

    def test_after_zoom_in_prefers_static(self):
        self.assertEqual(m.STATIC, self.select(_ctx(None, False, False, False, m.ZOOM_IN, 1)).motion)

    def test_static_cadence_forces_static_at_max_shots(self):
        self.assertEqual(m.STATIC, self.select(_ctx(None, False, False, False, m.ZOOM_OUT, 6)).motion)

    def test_static_cadence_forces_static_after_pan_runs(self):
        self.assertEqual(m.STATIC, self.select(_ctx(None, False, False, False, m.PAN_RIGHT, 4)).motion)

    def test_after_static_prefers_zoom_in(self):
        self.assertEqual(m.ZOOM_IN, self.select(_ctx(None, False, False, False, m.STATIC, 1)).motion)

    def test_auto_selection_never_repeats_previous_motion(self):
        for previous in (x for x in m.MOTIONS if x != m.PAN_REVEAL):
            d = self.select(_ctx(None, False, False, False, previous, 1))
            self.assertNotEqual(previous, d.motion)

    def test_auto_selection_never_picks_pan_reveal_or_vertical_pans(self):
        for previous in (x for x in m.MOTIONS if x not in (m.PAN_REVEAL, m.PAN_UP, m.PAN_DOWN)):
            for shots in range(0, 9):
                d = self.select(_ctx(None, False, False, False, previous, shots))
                self.assertNotIn(d.motion, (m.PAN_REVEAL, m.PAN_UP, m.PAN_DOWN))

    def test_pan_up_spans_full_vertical_travel(self):
        plan = _engine().plan_clip(m.PAN_UP, m.EXPLICIT_CODE, True, 2304, 2160)
        self.assertEqual(m.PAN_UP, plan.motion)
        self.assertEqual(R(0, 864, 2304, 1296), plan.start)
        self.assertEqual(R(0, 0, 2304, 1296), plan.end)
        self.assertTrue(plan.end.is_inside(R(0, 0, 2304, 2160)))

    def test_pan_down_spans_full_vertical_travel(self):
        plan = _engine().plan_clip(m.PAN_DOWN, m.EXPLICIT_CODE, True, 2304, 2160)
        self.assertEqual(0, plan.start.y)
        self.assertEqual(864, plan.end.y)
        self.assertTrue(plan.end.is_inside(R(0, 0, 2304, 2160)))

    def test_vertical_pan_on_16x9_falls_back_to_push_in(self):
        plan = _engine().plan_clip(m.PAN_DOWN, m.EXPLICIT_CODE, True, 2304, 1296)
        self.assertEqual(m.ZOOM_IN, plan.motion)
        self.assertEqual(R(0, 0, 2304, 1296), plan.start)
        self.assertTrue([w for w in plan.warnings if "No vertical overscan" in w])
        self.assertTrue(plan.warnings[0].startswith("No vertical overscan for PanDown (image 2304x1296);"))

    def test_static_shows_full_image(self):
        plan = _engine().plan_clip(m.STATIC, m.AUTO_SELECTED, True, 2304, 1296)
        self.assertEqual(R(0, 0, 2304, 1296), plan.start)
        self.assertEqual(plan.start, plan.end)
        self.assertEqual([], plan.warnings)

    def test_static_on_16x9_without_oversize_shows_full_image(self):
        plan = _engine().plan_clip(m.STATIC, m.AUTO_SELECTED, True, 1920, 1080)
        self.assertEqual(R(0, 0, 1920, 1080), plan.start)
        self.assertEqual(plan.start, plan.end)
        self.assertEqual([], plan.warnings)

    def test_static_on_tall_image_fits_whole_image(self):
        plan = _engine().plan_clip(m.STATIC, m.AUTO_SELECTED, True, 1024, 1536)
        self.assertEqual(R(0, 0, 1024, 1536), plan.start)
        self.assertEqual(plan.start, plan.end)

    def test_zoom_in_pushes_into_full_image(self):
        plan = _engine().plan_clip(m.ZOOM_IN, m.AUTO_SELECTED, True, 2304, 1296)
        self.assertEqual(R(0, 0, 2304, 1296), plan.start)
        self.assertTrue(plan.end.is_inside(R(0, 0, 2304, 1296)))
        self.assertLess(plan.end.width, plan.start.width)
        self.assertAlmostEqual(2304 * 100.0 / 106.0, plan.end.width, 3)

    def test_zoom_on_tall_image_zooms_the_letterbox_canvas(self):
        plan = _engine().plan_clip(m.ZOOM_IN, m.EXPLICIT_CODE, True, 1024, 1536)
        self.assertEqual(2730, plan.start.width)
        self.assertEqual(1536, plan.start.height)
        self.assertEqual(853, plan.start.x)
        self.assertLess(plan.end.width, plan.start.width)
        self.assertTrue(plan.end.is_inside(plan.start))

    def test_zoom_out_ends_on_the_107_percent_window(self):
        zout = _engine().plan_clip(m.ZOOM_OUT, m.AUTO_SELECTED, True, 2304, 1296)
        # mirrors the C# (percents are swapped after the shrink): full frame first, 107% window last
        self.assertAlmostEqual(2304.0, zout.start.width, 6)
        self.assertAlmostEqual(2304 * 100.0 / 107.0, zout.end.width, 3)

    def test_explicit_pan_right_pans_within_fit_bounds(self):
        plan = _engine().plan_clip(m.PAN_RIGHT, m.EXPLICIT_CODE, True, 2880, 1296)
        self.assertEqual(m.PAN_RIGHT, plan.motion)
        self.assertEqual(R(0, 0, 2304, 1296), plan.start)
        self.assertEqual(R(576, 0, 2304, 1296), plan.end)
        self.assertTrue(plan.end.is_inside(R(0, 0, 2880, 1296)))

    def test_auto_pan_is_capped_to_the_travel_percent(self):
        plan = _engine().plan_clip(m.PAN_RIGHT, m.AUTO_SELECTED, True, 2880, 1296)
        travel = plan.end.x - plan.start.x
        self.assertAlmostEqual(0.04 * 2560, travel, 6)
        self.assertAlmostEqual(576 / 2.0, (plan.start.x + plan.end.x) / 2.0, 6)

    def test_pan_right_on_16x9_without_overscan_falls_back_to_push_in(self):
        plan = _engine().plan_clip(m.PAN_RIGHT, m.EXPLICIT_CODE, True, 1920, 1080)
        self.assertEqual(m.ZOOM_IN, plan.motion)
        self.assertEqual(R(0, 0, 1920, 1080), plan.start)
        self.assertTrue([w for w in plan.warnings if "No horizontal overscan" in w])

    def test_auto_pan_on_16x9_falls_back_to_push_in(self):
        plan = _engine().plan_clip(m.PAN_LEFT, m.AUTO_SELECTED, True, 2304, 1296)
        self.assertEqual(m.ZOOM_IN, plan.motion)
        self.assertEqual(R(0, 0, 2304, 1296), plan.start)
        self.assertTrue([w for w in plan.warnings if "No horizontal overscan" in w])

    def test_pan_reveal_direction_follows_flag(self):
        e = _engine()
        ltr = e.plan_clip(m.PAN_REVEAL, m.EXPLICIT_CODE, True, 3840, 1296)
        self.assertEqual(0, ltr.start.x)
        self.assertEqual(1536, ltr.end.x)
        rtl = e.plan_clip(m.PAN_REVEAL, m.EXPLICIT_CODE, False, 3840, 1296)
        self.assertEqual(1536, rtl.start.x)
        self.assertEqual(0, rtl.end.x)


# ====================================================================== ProjectOverrides
class ProjectOverridesTests(unittest.TestCase):
    def test_round_trip_preserves_overrides(self):
        o = ov.ProjectOverrides()
        o.clips.append(ov.ClipOverride(file="images/S01_02_CU_ZI.png", motion=m.PAN_RIGHT,
                                       easing=m.EASE_IN_OUT, duration_frames=150, exclude=False, order=1))
        o.cuts.append(ov.CutOverride("images/S02_01_SCN_ZI.png", "fade_black"))
        loaded = ov.load_from_json(ov.to_json(o))
        self.assertEqual(1, len(loaded.clips))
        c = loaded.clips[0]
        self.assertEqual(("images/S01_02_CU_ZI.png", m.PAN_RIGHT, m.EASE_IN_OUT, 150, False, 1),
                         (c.file, c.motion, c.easing, c.duration_frames, c.exclude, c.order))
        self.assertEqual(1, len(loaded.cuts))
        self.assertEqual(("images/S02_01_SCN_ZI.png", "fade_black"),
                         (loaded.cuts[0].before_file, loaded.cuts[0].transition))

    def test_snake_case_keys_are_used(self):
        o = ov.ProjectOverrides()
        o.clips.append(ov.ClipOverride(file="a.png", motion=m.STATIC, duration_frames=90))
        text = ov.to_json(o)
        self.assertIn('"schema_version": 1', text)
        self.assertIn('"duration_frames": 90', text)
        self.assertIn('"motion": "static"', text)

    def test_unknown_schema_version_is_rejected(self):
        with self.assertRaises(ValueError):
            ov.load_from_json('{ "schema_version": 99 }')

    def test_missing_schema_version_is_rejected(self):
        with self.assertRaises(ValueError):
            ov.load_from_json('{ "clips": [] }')

    def test_unknown_motion_is_rejected(self):
        with self.assertRaises(ValueError):
            ov.load_from_json('{ "schema_version": 1, "clips": [ { "file": "a.png", "motion": "spin" } ] }')

    def test_lookup_helpers_match_case_insensitively(self):
        o = ov.ProjectOverrides()
        o.clips.append(ov.ClipOverride(file="images/S01_01.png", motion=m.STATIC))
        o.cuts.append(ov.CutOverride("images/S01_02.png", "fade_black"))
        self.assertIsNotNone(o.for_clip("IMAGES\\s01_01.PNG"))
        self.assertEqual("fade_black", o.cut_for("images/S01_02.png"))
        self.assertIsNone(o.cut_for("images/other.png"))

    def test_for_shot_matches_shot_id_case_insensitively(self):
        o = ov.ProjectOverrides()
        o.clips.append(ov.ClipOverride(file="a.png", shot="SH003"))
        self.assertIsNotNone(o.for_shot("sh003"))
        self.assertIsNone(o.for_shot("SH004"))
        self.assertIsNone(o.for_shot(""))


# ====================================================================== EditPlanner
def _plan(project, audio=20.0, options=None):
    options = options or ProjectOptions()
    inventory = project_loader.load(project.path, options)
    return edit_planner.plan(inventory, audio, options)


class EditPlannerTests(unittest.TestCase):
    def setUp(self):
        self.project = TempProject()
        self.addCleanup(self.project.cleanup)

    def write_standard_project(self):
        p = self.project
        p.write_image("S01_01.png", 2304, 1296)
        p.write_image("S01_02_ZI.png", 2304, 1296)
        p.write_image("S02_01.png", 2304, 1296)
        p.write_image("S02_02_PR.png", 2880, 1296)
        p.write_srt()
        p.write_audio()

    def test_plans_complete_project_end_to_end(self):
        self.write_standard_project()
        result = _plan(self.project)
        self.assertTrue(result.success)
        tl = result.timeline
        self.assertEqual(2, len(tl.scenes))
        self.assertEqual(600, tl.audio.duration_frames)
        clips = [c for s in tl.scenes for c in s.clips]
        self.assertEqual(4, len(clips))

        c1 = clips[0]
        self.assertEqual((m.ZOOM_IN, m.AUTO_SELECTED, 0, 153), (c1.motion, c1.motion_source, c1.start_frame, c1.duration_frames))
        self.assertIsNone(c1.transition)
        c2 = clips[1]
        self.assertEqual((m.ZOOM_IN, m.EXPLICIT_CODE, 153), (c2.motion, c2.motion_source, c2.start_frame))
        self.assertEqual(15, c2.transition.duration_frames)
        c3 = clips[2]
        self.assertEqual((m.STATIC, 306, 147), (c3.motion, c3.start_frame, c3.duration_frames))
        c4 = clips[3]
        self.assertEqual((m.PAN_RIGHT, m.EXPLICIT_CODE, 453), (c4.motion, c4.motion_source, c4.start_frame))
        self.assertEqual(0, c4.start_viewport.x)
        # explicit PR sweeps the full band edge-to-edge; the pan-pacing work
        # shapes it with ease-out easing, not a shorter distance
        self.assertAlmostEqual(576.0, c4.end_viewport.x, places=1)
        self.assertFalse([i for i in result.issues if i.severity in (m.WARNING, m.ERROR)])
        self.assertEqual(2, len([i for i in result.issues if i.code == "MOTION_EXPLICIT"]))

    def test_motion_explicit_message_uses_the_csharp_motion_name(self):
        self.write_standard_project()
        result = _plan(self.project)
        msgs = [i.message for i in result.issues if i.code == "MOTION_EXPLICIT"]
        self.assertEqual(["S01_02_ZI.png uses explicit motion ZoomIn.",
                          "S02_02_PR.png uses explicit motion PanRight."], msgs)

    def test_project_name_is_the_folder_name(self):
        self.write_standard_project()
        result = _plan(self.project)
        self.assertEqual(os.path.basename(self.project.path), result.timeline.project_name)

    def test_zoom_in_end_viewport_is_magnified(self):
        self.write_standard_project()
        clip1 = _plan(self.project).timeline.scenes[0].clips[0]
        self.assertAlmostEqual(2304 * 100.0 / 106.0, clip1.end_viewport.width, 3)
        self.assertTrue(clip1.end_viewport.is_inside(R(0, 0, 2304, 1296)))

    def test_scene_map_override_wins_over_inference(self):
        self.write_standard_project()
        self.project.write_file("scenes.json", """
            [
              { "scene": "S01", "start": 0.0, "end": 8.0 },
              { "scene": "S02", "start": 8.0, "end": 20.0 }
            ]""")
        result = _plan(self.project)
        self.assertTrue(result.success)
        clips = [c for s in result.timeline.scenes for c in s.clips]
        self.assertEqual([120, 120, 180, 180], [c.duration_frames for c in clips])

    def test_overrides_apply_motion_easing_and_cut_transitions(self):
        self.write_standard_project()
        self.project.write_file("overrides.json", """
            {
              "schema_version": 1,
              "clips": [
                { "file": "images/S01_02_ZI.png", "motion": "static", "easing": "ease_in_out" }
              ],
              "cuts": [
                { "before_file": "images/S02_02_PR.png", "transition": "circle_open" }
              ]
            }""")
        inventory = project_loader.load(self.project.path)
        self.assertIsNotNone(inventory.overrides.for_clip(inventory.all_images[1].file_path))
        result = edit_planner.plan(inventory, 20.0, ProjectOptions())
        self.assertTrue(result.success)
        clips = [c for s in result.timeline.scenes for c in s.clips]
        c2 = clips[1]
        self.assertEqual((m.STATIC, m.OVERRIDE, m.EASE_IN_OUT, "crossfade"),
                         (c2.motion, c2.motion_source, c2.easing, c2.transition.kind))
        self.assertEqual("fade_black", clips[2].transition.kind)
        self.assertEqual("circle_open", clips[3].transition.kind)

    def test_even_distribution_boundaries_snap_to_sentence_pauses(self):
        p = self.project
        p.write_image("S01_01_SCN_ST.png", 2304, 1296)
        p.write_image("S02_01_SCN_ZI.png", 2304, 1296)
        p.write_image("S03_01_SCN_ST.png", 2304, 1296)
        p.write_file("narration.srt", """1
00:00:00,000 --> 00:00:05,000
One.

2
00:00:05,200 --> 00:00:11,000
Two.

3
00:00:12,000 --> 00:00:17,000
Three.

4
00:00:17,200 --> 00:00:21,000
Four.
""")
        p.write_audio()
        result = _plan(p, 30.0)
        self.assertTrue(result.success)
        scenes = result.timeline.scenes
        self.assertEqual(3, len(scenes))
        self.assertEqual([(0, 330), (330, 630), (630, 900)], [(s.start_frame, s.end_frame) for s in scenes])
        self.assertTrue([i for i in result.issues
                         if i.code == "SCENE_BOUNDARIES_SNAPPED" and "2 of 2" in i.message])

    def test_mismatched_scene_counts_distribute_images_evenly(self):
        p = self.project
        p.write_image("S01_01_SCN_ST.png", 2304, 1296)
        p.write_image("S02_01_SCN_ZI.png", 2304, 1296)
        p.write_image("S03_01_SCN_ST.png", 2304, 1296)
        p.write_srt()
        p.write_audio()
        result = _plan(p)
        self.assertTrue(result.success)
        scenes = result.timeline.scenes
        self.assertEqual([(0, 200), (200, 400), (400, 600)], [(s.start_frame, s.end_frame) for s in scenes])
        self.assertIn("SCENE_COUNT_MISMATCH", [i.code for i in result.issues])
        self.assertFalse([i for i in result.issues if i.severity == m.ERROR])

    def test_missing_audio_blocks_planning(self):
        self.project.write_image("S01_01.png", 2304, 1296)
        self.project.write_srt()
        result = _plan(self.project)
        self.assertFalse(result.success)
        self.assertIn("AUDIO_MISSING", [i.code for i in result.issues])

    def test_planning_is_deterministic(self):
        self.write_standard_project()
        first, second = _plan(self.project), _plan(self.project)
        self.assertEqual(json.dumps(first.timeline.to_dict()), json.dumps(second.timeline.to_dict()))

    def test_coverage_invariant_holds(self):
        self.write_standard_project()
        clips = [c for s in _plan(self.project).timeline.scenes for c in s.clips]
        cursor = 0
        for c in clips:
            self.assertEqual(cursor, c.start_frame)
            cursor += c.duration_frames
        self.assertEqual(600, cursor)

    def test_scene_map_gap_warnings(self):
        self.write_standard_project()
        self.project.write_file("scenes.json", """
            [
              { "scene": "S01", "start": 1.0, "end": 8.0 },
              { "scene": "S02", "start": 9.0, "end": 18.5 }
            ]""")
        result = _plan(self.project)
        self.assertTrue(result.success)
        msgs = {i.code: i.message for i in result.issues}
        self.assertEqual("First scene starts at 1.00s; extended to 0s to cover the audio start.",
                         msgs["SCENE_MAP_LEAD_GAP"])
        self.assertEqual("Gap between S01 and S02; S01 extended to cover it.", msgs["SCENE_MAP_GAP"])
        self.assertEqual("Last scene ends at 18.50s; extended to audio end 20.00s.", msgs["SCENE_MAP_TRAIL_GAP"])

    def test_scene_map_without_matching_images_is_an_error(self):
        self.write_standard_project()
        self.project.write_file("scenes.json", """
            [ { "scene": "S01", "start": 0, "end": 8 }, { "scene": "S09", "start": 8, "end": 20 } ]""")
        result = _plan(self.project)
        self.assertFalse(result.success)
        codes = [i.code for i in result.issues]
        self.assertIn("SCENE_MAP_NO_IMAGES", codes)
        self.assertIn("SCENE_MAP_UNCOVERED", codes)

    def test_v1_planner_ignores_the_manifest(self):
        self.write_standard_project()
        manifest = mf.VisualManifest(video=mf.VisualManifestVideo(title="x", fps=30.0))
        options = ProjectOptions()
        options.planner = "v1"
        inventory = project_loader.load(self.project.path, options)
        inventory.manifest = manifest
        result = edit_planner.plan(inventory, 20.0, options)
        self.assertTrue(result.success)
        self.assertIn("MANIFEST_IGNORED", [i.code for i in result.issues])


# ====================================================================== ProjectLoader
class ProjectLoaderTests(unittest.TestCase):
    def setUp(self):
        self.project = TempProject()
        self.addCleanup(self.project.cleanup)

    def load(self, options=None):
        return project_loader.load(self.project.path, options)

    def test_loads_complete_project(self):
        p = self.project
        p.write_image("S01_01.png", 2304, 1296)
        p.write_image("S01_02_ZI.png", 2304, 1296)
        p.write_image("S02_01.png", 2304, 1296)
        p.write_srt()
        p.write_audio()
        inv = self.load()
        self.assertFalse(m.has_errors(inv.issues))
        self.assertIsNotNone(inv.audio_file_path)
        self.assertIsNotNone(inv.srt_file_path)
        self.assertEqual(3, len(inv.all_images))
        self.assertEqual(2, len(inv.scene_groups))
        self.assertEqual("S01", inv.scene_groups[0].scene_id)
        self.assertEqual(2, len(inv.scene_groups[0].images))
        self.assertEqual(4, len(inv.subtitles))
        self.assertIsNone(inv.scene_map_windows)

    def test_warns_about_unrecognized_files(self):
        p = self.project
        p.write_image("S01_01.png", 2304, 1296)
        p.write_image("notes.png", 100, 100)
        p.write_srt()
        p.write_audio()
        inv = self.load()
        self.assertTrue([i for i in inv.issues if i.code == "IMAGE_UNRECOGNIZED" and "S{scene}_{index}" in i.message])

    def test_caps_unrecognized_warnings_with_summary(self):
        for i in range(1, 8):
            self.project.write_image(f"junk{i}.png", 100, 100)
        self.project.write_srt()
        self.project.write_audio()
        inv = self.load()
        self.assertEqual(5, len([i for i in inv.issues if i.code == "IMAGE_UNRECOGNIZED"]))
        self.assertTrue([i for i in inv.issues if i.code == "IMAGE_UNRECOGNIZED_MORE" and "2 more" in i.message])

    def test_explains_extension_mismatch_when_no_allowed_extension(self):
        for n in ("S01_01.jpg", "S01_02.jpg"):
            with open(os.path.join(self.project.path, "images", n), "wb") as f:
                f.write(bytes([0xFF, 0xD8, 0xFF, 0xC0, 0, 0x0B, 8, 0x05, 0x10, 0x09, 0x00, 1, 1, 0, 1, 1, 0]))
        self.project.write_srt()
        self.project.write_audio()
        inv = self.load()
        self.assertTrue([i for i in inv.issues
                         if i.code == "IMAGES_EXTENSION" and ".jpg" in i.message and ".png" in i.message])
        self.assertTrue([i for i in inv.issues if i.code == "IMAGES_NONE" and i.severity == m.ERROR])

    def test_errors_when_images_folder_is_empty(self):
        self.project.write_srt()
        self.project.write_audio()
        inv = self.load()
        self.assertTrue([i for i in inv.issues if i.code == "IMAGES_EMPTY" and i.severity == m.ERROR])

    def test_errors_when_images_folder_is_missing(self):
        os.rmdir(os.path.join(self.project.path, "images"))
        self.project.write_srt()
        self.project.write_audio()
        self.assertIn("IMAGES_MISSING", [i.code for i in self.load().issues])

    def test_loads_new_naming_format_with_type_codes(self):
        p = self.project
        p.write_image("S01_01_SCN_ST.png", 2304, 1296)
        p.write_image("S01_02_CU_ZI.png", 2304, 1296)
        p.write_image("S01_03_PROC.png", 2304, 1296)
        p.write_srt()
        p.write_audio()
        inv = self.load()
        self.assertFalse(m.has_errors(inv.issues))
        imgs = inv.all_images
        self.assertEqual(3, len(imgs))
        self.assertEqual("scene", imgs[0].name.type)
        self.assertEqual("close_up", imgs[1].name.type)
        self.assertEqual(m.ZOOM_IN, imgs[1].name.code)
        self.assertEqual("process", imgs[2].name.type)
        self.assertIsNone(imgs[2].name.code)

    def test_finds_audio_and_srt_with_any_file_name(self):
        p = self.project
        p.write_image("S01_01.png", 2304, 1296)
        p.write_file("voice_take3.mp3", "fake audio")
        p.write_file("final_subs.srt", "1\n00:00:00,000 --> 00:00:02,000\nHello.\n")
        inv = self.load()
        self.assertFalse(m.has_errors(inv.issues))
        self.assertTrue(inv.audio_file_path.endswith("voice_take3.mp3"))
        self.assertTrue(inv.srt_file_path.endswith("final_subs.srt"))
        codes = [i.code for i in inv.issues]
        self.assertNotIn("AUDIO_AMBIGUOUS", codes)
        self.assertNotIn("SRT_AMBIGUOUS", codes)

    def test_info_when_multiple_audio_files_exist(self):
        p = self.project
        p.write_image("S01_01.png", 2304, 1296)
        p.write_srt()
        p.write_file("audio/track_b.mp3", "fake audio")
        p.write_file("audio/track_a.mp3", "fake audio")
        inv = self.load()
        self.assertTrue([i for i in inv.issues if i.code == "AUDIO_AMBIGUOUS" and "track_a.mp3" in i.message])
        self.assertTrue(inv.audio_file_path.endswith("track_a.mp3"))

    def test_info_when_multiple_srt_files_exist(self):
        p = self.project
        p.write_image("S01_01.png", 2304, 1296)
        p.write_audio()
        p.write_srt()
        p.write_file("alt.srt", "1\n00:00:00,000 --> 00:00:01,000\nAlt.\n")
        inv = self.load()
        self.assertTrue([i for i in inv.issues if i.code == "SRT_AMBIGUOUS" and "alt.srt" in i.message])
        self.assertEqual(1, len(inv.subtitles))

    def test_warns_about_unknown_motion_code(self):
        p = self.project
        p.write_image("S01_01_XX.png", 2304, 1296)
        p.write_srt()
        p.write_audio()
        inv = self.load()
        self.assertIn("IMAGE_UNKNOWN_CODE", [i.code for i in inv.issues])
        self.assertEqual(1, len(inv.all_images))
        self.assertTrue(inv.all_images[0].name.has_unknown_suffix)
        self.assertIsNone(inv.all_images[0].name.code)

    def test_errors_when_audio_missing(self):
        self.project.write_image("S01_01.png", 2304, 1296)
        self.project.write_srt()
        inv = self.load()
        self.assertTrue([i for i in inv.issues if i.code == "AUDIO_MISSING" and i.severity == m.ERROR])
        self.assertIsNone(inv.audio_file_path)

    def test_errors_when_srt_unparsable(self):
        self.project.write_image("S01_01.png", 2304, 1296)
        self.project.write_audio()
        self.project.write_file("narration.srt", "1\nbroken\n")
        inv = self.load()
        self.assertTrue([i for i in inv.issues if i.code == "SRT_UNPARSABLE" and i.severity == m.ERROR])

    def test_errors_when_srt_missing(self):
        self.project.write_image("S01_01.png", 2304, 1296)
        self.project.write_audio()
        self.assertTrue([i for i in self.load().issues if i.code == "SRT_MISSING" and i.severity == m.ERROR])

    def test_warns_about_duplicate_stems(self):
        options = ProjectOptions()
        options.naming.image_extensions = [".png", ".jpg"]
        p = self.project
        p.write_image("S01_01.png", 2304, 1296)
        with open(os.path.join(p.path, "images", "S01_01.jpg"), "wb") as f:
            f.write(png_header(2304, 1296))
        p.write_srt()
        p.write_audio()
        inv = self.load(options)
        self.assertIn("IMAGE_DUPLICATE_STEM", [i.code for i in inv.issues])
        self.assertEqual(1, len(inv.scene_groups[0].images))

    def test_loads_scene_map_when_present(self):
        p = self.project
        p.write_image("S01_01.png", 2304, 1296)
        p.write_image("S02_01.png", 2304, 1296)
        p.write_srt()
        p.write_audio()
        p.write_file("scenes.json", """
            [
              { "scene": "S01", "start": 0.0, "end": 10.0 },
              { "scene": "S02", "start": 10.0, "end": 20.0 }
            ]""")
        inv = self.load()
        self.assertIsNotNone(inv.scene_map_windows)
        self.assertEqual(2, len(inv.scene_map_windows))

    def test_warns_about_risky_png_format(self):
        p = self.project
        path = os.path.join(p.path, "images", "S01_01.png")
        with open(path, "wb") as f:
            f.write(png_header(2304, 1296, bit_depth=16, color_type=3) + b"\0" * 8)
        p.write_srt()
        p.write_audio()
        issue = next(i for i in self.load().issues if i.code == "PNG_FORMAT_RISKY")
        self.assertIn('"S01_01.png" is a 16-bit palette PNG', issue.message)

    def test_overrides_exclude_and_order(self):
        p = self.project
        p.write_image("S01_01.png", 2304, 1296)
        p.write_image("S01_02.png", 2304, 1296)
        p.write_image("S01_03.png", 2304, 1296)
        p.write_srt()
        p.write_audio()
        p.write_file("overrides.json", json.dumps({"schema_version": 1, "clips": [
            {"file": "images/S01_01.png", "exclude": True},
            {"file": "images/S01_03.png", "order": 1}]}))
        inv = self.load()
        self.assertIn("IMAGE_EXCLUDED", [i.code for i in inv.issues])
        self.assertEqual(["S01_03.png", "S01_02.png"],
                         [os.path.basename(i.file_path) for i in inv.all_images])

    def test_broken_overrides_are_reported(self):
        self.project.write_image("S01_01.png", 2304, 1296)
        self.project.write_file("overrides.json", '{ "schema_version": 7 }')
        inv = self.load()
        issue = next(i for i in inv.issues if i.code == "OVERRIDES_INVALID")
        self.assertEqual(m.ERROR, issue.severity)
        self.assertIn("Unsupported overrides schema_version 7", issue.message)

    def test_manifest_images_are_grouped_by_asset_scene(self):
        p = self.project
        p.write_image("S01_B01_01.png", 1920, 1080)
        p.write_image("S02_B01_01.png", 1920, 1080)
        p.write_srt()
        p.write_audio()
        p.write_file("visual_manifest.json", json.dumps({
            "schema_version": "1.0", "video": {},
            "assets": [{"id": "A1", "file": "S01_B01_01.png", "scene_id": "S01"},
                       {"id": "A2", "file": "S02_B01_01.png", "scene_id": "S02"}],
            "timeline": []}))
        inv = self.load()
        self.assertIsNotNone(inv.manifest)
        self.assertEqual(["S01", "S02"], [g.scene_id for g in inv.scene_groups])
        self.assertEqual(1, len(inv.scene_groups[1].images))

    def test_invalid_manifest_is_reported(self):
        self.project.write_file("visual_manifest.json", '{ "schema_version": "9.9" }')
        issue = next(i for i in self.load().issues if i.code == "MANIFEST_INVALID")
        self.assertIn("Unsupported visual_manifest schema_version", issue.message)

    def test_v1_planner_ignores_the_shotlist(self):
        self.project.write_file("shotlist.json", '{ "shots": [] }')
        options = ProjectOptions()
        options.planner = "v1"
        self.assertIn("SHOTLIST_IGNORED", [i.code for i in self.load(options).issues])

    def test_broken_shotlist_is_reported(self):
        self.project.write_file("shotlist.json", "not json")
        issue = next(i for i in self.load().issues if i.code == "SHOTLIST_INVALID")
        self.assertTrue(issue.message.startswith("shotlist.json could not be loaded: "))


# ====================================================================== ManifestPlanner
def _vm(**kw):
    return mf.VisualManifestVideo(**kw)


def sample_manifest(shot4_end_ms=13480):
    return mf.VisualManifest(
        schema_version="1.0",
        video=_vm(id="interval-walking", title="Interval Walking", duration_ms=530000, fps=30),
        assets=[
            mf.VisualAsset("IMG001", "S01_B01_01.png", "S01", ["B01"], "closeup",
                           ["ZI", "ZO", "STATIC"], False, None),
            mf.VisualAsset("IMG002", "S01_B02_01.png", "S01", ["B02"], "lifestyle",
                           ["ZI", "ZO", "PL", "PR", "STATIC"], False, None),
            mf.VisualAsset("IMG003", "S01_B03_01_INFO.png", "S01", ["B03", "B04"], "comparison",
                           ["STATIC", "ZI"], True,
                           [mf.FocalRegion("left", 0.0, 0.0, 0.5, 1.0),
                            mf.FocalRegion("right", 0.5, 0.0, 0.5, 1.0)])],
        timeline=[
            mf.VisualShot("SH001", 0, 3240, "S01", "B01", [1, 2],
                          "Most people think walking is simply about getting more steps.",
                          "hook", "opening_visual", "IMG001", mf.VisualFraming("close", None),
                          mf.VisualMotion("ZI", 1.0, 1.1, None), mf.VisualTransition("CUT", 0)),
            mf.VisualShot("SH002", 3240, 6810, "S01", "B02", [2, 3],
                          "But there may be another part of walking that matters.",
                          "new_point", "new_claim", "IMG002", mf.VisualFraming("wide", None),
                          mf.VisualMotion("PR", None, None, None), mf.VisualTransition("CUT", 0)),
            mf.VisualShot("SH003", 6810, 10150, "S01", "B03", [4],
                          "Researchers compared easier walking with periods of faster walking.",
                          "comparison", "new_comparison", "IMG003", mf.VisualFraming("crop", "left"),
                          mf.VisualMotion("STATIC", None, None, None), mf.VisualTransition("CUT", 0)),
            mf.VisualShot("SH004", 10150, shot4_end_ms, "S01", "B04", [4, 5],
                          "The faster intervals created a very different pattern of effort.",
                          "detail", "same_topic_new_focus", "IMG003", mf.VisualFraming("crop", "right"),
                          mf.VisualMotion("ZI", 1.0, 1.08, None), mf.VisualTransition("CROSSFADE", 180))])


def images16():
    return [_image(os.path.join("images", f), 1920, 1080, stem=f, index=0)
            for f in ("S01_B01_01.png", "S01_B02_01.png", "S01_B03_01_INFO.png")]


def mp_options():
    o = ProjectOptions()
    o.motion.motion_duration_ms = 0
    return o


def mplan(manifest=None, audio_frames=405, overrides=None, shot4_end_ms=13480, images=None, options=None):
    result = manifest_planner.plan(manifest or sample_manifest(shot4_end_ms), images or images16(),
                                   options or mp_options(), audio_frames,
                                   overrides or ov.ProjectOverrides())
    return result


def mplan_ok(**kw):
    r = mplan(**kw)
    assert r.timeline is not None
    return r.timeline, r.coverage, r.issues


def with_shot(manifest, index, **changes):
    shots = list(manifest.timeline)
    shots[index] = dataclasses.replace(shots[index], **changes)
    return dataclasses.replace(manifest, timeline=shots)


class ManifestPlannerTests(unittest.TestCase):
    def test_plans_shots_into_one_scene_with_manifest_durations(self):
        tl, cov, issues = mplan_ok()
        self.assertEqual(1, len(tl.scenes))
        clips = tl.scenes[0].clips
        self.assertEqual([97, 107, 100, 101], [c.duration_frames for c in clips])
        self.assertEqual(405, sum(c.duration_frames for c in clips))
        self.assertTrue(all(c.scene_id == "S01" for c in clips))
        self.assertIsNotNone(clips[0].shot_id)
        self.assertFalse([i for i in issues if i.severity == m.ERROR])
        self.assertEqual((3, 3), (cov.unique_assets_used, cov.asset_count))
        self.assertEqual("Interval Walking", tl.project_name)

    def test_maps_scales_and_framing_to_viewports(self):
        clips = mplan_ok()[0].scenes[0].clips
        sh1 = clips[0]
        self.assertAlmostEqual(1080 * 0.70, sh1.start_viewport.height, 1)
        self.assertLess(sh1.end_viewport.height, sh1.start_viewport.height)
        sh3 = clips[2]
        self.assertEqual(m.STATIC, sh3.motion)
        self.assertAlmostEqual(960, sh3.start_viewport.width, 1)
        self.assertAlmostEqual(960 / (16.0 / 9.0), sh3.start_viewport.height, 1)

    def test_scene_editor_overrides_win_over_shotlist_and_settings(self):
        o = ov.ProjectOverrides()
        o.clips.append(ov.ClipOverride(file="images/S01_B01_01.png", shot="SH001", motion=m.PAN_RIGHT,
                                       easing=m.EASE_IN))
        o.cuts.append(ov.CutOverride(os.path.join("images", "S01_B03_01_INFO.png"), "fade_black"))
        tl, _, issues = mplan_ok(overrides=o)
        clips = tl.scenes[0].clips
        # the PR image has no horizontal overscan: the engine's push-in
        # fallback is surfaced honestly (relabeled + VIEWPORT warning)
        # instead of rendering a push-in under a pan label
        self.assertEqual(m.ZOOM_IN, clips[0].motion)
        self.assertEqual(m.AUTO_SELECTED, clips[0].motion_source)
        self.assertTrue([i for i in issues if i.code == "VIEWPORT"
                         and i.severity == m.WARNING and "SH001" in i.message])
        self.assertEqual(m.EASE_IN, clips[0].easing)
        self.assertEqual("fade_black", clips[3].transition.kind)

    def test_zoom_without_explicit_scales_uses_motion_settings(self):
        manifest = with_shot(sample_manifest(), 0, motion=mf.VisualMotion("ZI", None, None, None))
        sh1 = mplan_ok(manifest=manifest)[0].scenes[0].clips[0]
        self.assertAlmostEqual(1080 * 0.70, sh1.start_viewport.height, 1)
        self.assertAlmostEqual(1080 * 0.70 / 1.06, sh1.end_viewport.height, 1)

    def test_slide_shot_transitions(self):
        for name, kind in (("SLIDE_LEFT", "slide_left"), ("SLIDE_RIGHT", "slide_right"),
                           ("SLIDE_UP", "slide_up"), ("SLIDE_DOWN", "slide_down")):
            manifest = with_shot(sample_manifest(), 2, transition_out=mf.VisualTransition(name, 180))
            clips = mplan_ok(manifest=manifest)[0].scenes[0].clips
            self.assertEqual(kind, clips[3].transition.kind)

    def test_applies_transitions_from_previous_shot(self):
        manifest = with_shot(sample_manifest(), 2, transition_out=mf.VisualTransition("CROSSFADE", 180))
        clips = mplan_ok(manifest=manifest)[0].scenes[0].clips
        self.assertIsNone(clips[0].transition)
        self.assertEqual("crossfade", clips[1].transition.kind)
        self.assertEqual(15, clips[1].transition.duration_frames)
        self.assertEqual("crossfade", clips[2].transition.kind)
        self.assertEqual("crossfade", clips[3].transition.kind)
        self.assertEqual(5, clips[3].transition.duration_frames)

    def test_dip_transitions_and_scene_boundary_kind(self):
        manifest = sample_manifest()
        shots = list(manifest.timeline)
        shots[0] = dataclasses.replace(shots[0], transition_out=mf.VisualTransition("DIP", 300))
        shots[1] = dataclasses.replace(shots[1], transition_out=mf.VisualTransition("DIP_WHITE", 300))
        clips = mplan_ok(manifest=dataclasses.replace(manifest, timeline=shots))[0].scenes
        flat = [c for s in clips for c in s.clips]
        self.assertEqual(("fade_black", 9), (flat[1].transition.kind, flat[1].transition.duration_frames))
        self.assertEqual("fade_white", flat[2].transition.kind)
        # SH003 says CUT without an explicit transition: the settings fallback (within-scene kind)
        self.assertEqual("crossfade", flat[3].transition.kind)

    def test_settings_fallback_uses_scene_boundary_kind(self):
        manifest = sample_manifest()
        shots = list(manifest.timeline)
        shots[1] = dataclasses.replace(shots[1], scene_id="S02", transition_out=None)
        shots[0] = dataclasses.replace(shots[0], transition_out=None)
        flat = [c for s in mplan_ok(manifest=dataclasses.replace(manifest, timeline=shots))[0].scenes
                for c in s.clips]
        self.assertEqual("fade_black", flat[1].transition.kind)

    def test_pans_delegate_to_motion_engine(self):
        imgs = [_image(os.path.join("images", "S01_B01_01.png"), 1920, 1080, stem="S01_B01_01"),
                _image(os.path.join("images", "S01_B02_01.png"), 2880, 1296, stem="S01_B02_01",
                       index=2, code=m.PAN_RIGHT),
                _image(os.path.join("images", "S01_B03_01_INFO.png"), 1920, 1080, stem="S01_B03_01_INFO")]
        result = mplan(images=imgs)
        sh2 = result.timeline.scenes[0].clips[1]
        self.assertEqual((m.PAN_RIGHT, m.EXPLICIT_CODE), (sh2.motion, sh2.motion_source))
        self.assertGreater(sh2.end_viewport.x, sh2.start_viewport.x)
        # explicit PR = full-band sweep (576 px here), shaped by easing
        self.assertAlmostEqual(576.0, sh2.end_viewport.x)
        self.assertEqual([], [i for i in result.issues if i.code == "VIEWPORT"])

    def test_missing_shotlist_motion_falls_back_to_the_filename_code(self):
        manifest = with_shot(sample_manifest(), 1, motion=None)
        imgs = [_image(os.path.join("images", "S01_B01_01.png"), 1920, 1080, stem="S01_B01_01"),
                _image(os.path.join("images", "S01_B02_01.png"), 2880, 1296, stem="S01_B02_01",
                       index=2, code=m.PAN_RIGHT),
                _image(os.path.join("images", "S01_B03_01_INFO.png"), 1920, 1080, stem="S01_B03_01_INFO")]
        result = mplan(manifest=manifest, images=imgs)
        clip = result.timeline.scenes[0].clips[1]
        self.assertEqual((m.PAN_RIGHT, m.EXPLICIT_CODE), (clip.motion, clip.motion_source))
        self.assertTrue([i for i in result.issues if i.code == "MOTION_FROM_FILENAME"
                         and i.severity == m.INFO and "SH002" in i.message])

    def test_missing_motion_everywhere_defaults_to_static(self):
        tl, _, issues = mplan_ok(manifest=with_shot(sample_manifest(), 1, motion=None))
        clip = tl.scenes[0].clips[1]
        self.assertEqual((m.STATIC, m.AUTO_SELECTED), (clip.motion, clip.motion_source))
        self.assertNotIn("MOTION_FROM_FILENAME", [i.code for i in issues])

    def test_missing_asset_produces_generation_hint_and_time_is_absorbed(self):
        imgs = [i for i in images16() if not i.file_path.endswith("S01_B02_01.png")]
        result = mplan(images=imgs)
        self.assertIsNotNone(result.timeline)
        self.assertEqual(1, len(result.coverage.missing_assets))
        hint = result.coverage.missing_assets[0]
        self.assertIn("S01_B02_01.png", hint)
        self.assertIn("generate it", hint)
        self.assertIn("But there may be another part of walking", hint)
        self.assertTrue([i for i in result.issues if i.severity == m.WARNING])
        clips = result.timeline.scenes[0].clips
        self.assertEqual(3, len(clips))
        self.assertEqual(405, sum(c.duration_frames for c in clips))
        self.assertEqual([204, 100], [clips[0].duration_frames, clips[1].duration_frames])

    def test_unsafe_motion_warns(self):
        manifest = with_shot(sample_manifest(), 1, motion=mf.VisualMotion("PV", None, None, None))
        _, _, issues = mplan_ok(manifest=manifest)
        self.assertIn("MANIFEST_UNSAFE_MOTION", [i.code for i in issues])

    def test_motion_duration_cap_is_set_from_options(self):
        o = mp_options()
        o.motion.motion_duration_ms = 2000
        clips = mplan(options=o).timeline.scenes[0].clips
        self.assertEqual(60, clips[0].motion_duration_frames)
        self.assertIsNone(clips[2].motion_duration_frames)

    def test_per_shot_duration_overrides_apply_and_bad_sums_are_ignored(self):
        o = ov.ProjectOverrides()
        o.clips.append(ov.ClipOverride(shot="SH003", file="images/S01_B03_01_INFO.png", duration_frames=110))
        o.clips.append(ov.ClipOverride(shot="SH004", file="images/S01_B03_01_INFO.png", duration_frames=91))
        tl, _, issues = mplan_ok(overrides=o)
        self.assertEqual(110, tl.scenes[0].clips[2].duration_frames)
        self.assertNotIn("MANIFEST_OVERRIDE_IGNORED", [i.code for i in issues])

        broken = ov.ProjectOverrides()
        broken.clips.append(ov.ClipOverride(shot="SH003", file="images/S01_B03_01_INFO.png", duration_frames=200))
        tl2, _, issues2 = mplan_ok(overrides=broken)
        self.assertIn("MANIFEST_OVERRIDE_IGNORED", [i.code for i in issues2])
        self.assertEqual(100, tl2.scenes[0].clips[2].duration_frames)

    def test_excluded_shot_is_dropped_and_time_absorbed(self):
        o = ov.ProjectOverrides()
        o.clips.append(ov.ClipOverride(shot="SH002", file="images/S01_B02_01.png", exclude=True))
        tl, _, issues = mplan_ok(overrides=o)
        self.assertIn("SHOT_EXCLUDED", [i.code for i in issues])
        clips = tl.scenes[0].clips
        self.assertEqual(3, len(clips))
        self.assertEqual(405, sum(c.duration_frames for c in clips))
        self.assertEqual(204, clips[0].duration_frames)

    def test_holds_over_the_threshold_warn_for_review(self):
        _, _, issues = mplan_ok(audio_frames=1500)
        warnings = [i for i in issues if i.code == "SHOT_HOLD_LONG"]
        self.assertEqual(1, len(warnings))
        self.assertEqual(m.WARNING, warnings[0].severity)
        self.assertIn("SH004", warnings[0].message)
        self.assertIn("split it in shotlist.json", warnings[0].message)
        self.assertIn("holds 39.9s", warnings[0].message)

    def test_holds_under_the_threshold_stay_silent(self):
        self.assertNotIn("SHOT_HOLD_LONG", [i.code for i in mplan_ok()[2]])

    def test_zero_threshold_disables_the_hold_warning(self):
        o = mp_options()
        o.timing.warn_hold_seconds = 0
        self.assertNotIn("SHOT_HOLD_LONG", [i.code for i in mplan(audio_frames=1500, options=o).issues])

    def test_timeline_tail_is_pinned_to_audio_duration(self):
        tl, _, issues = mplan_ok(shot4_end_ms=14500)
        self.assertIn("MANIFEST_TIMING_FIXED", [i.code for i in issues])
        self.assertEqual(405, sum(c.duration_frames for c in tl.scenes[0].clips))
        self.assertEqual(101, tl.scenes[0].clips[3].duration_frames)

    def test_fps_mismatch_is_an_error(self):
        manifest = dataclasses.replace(sample_manifest(), video=_vm(title="x", fps=24))
        result = mplan(manifest=manifest)
        self.assertIsNone(result.timeline)
        self.assertEqual("Manifest fps 24 does not match the project output fps 30.", result.issues[0].message)

    def test_shot_timing_message_format(self):
        _, _, issues = mplan_ok()
        first = next(i for i in issues if i.code == "SHOT_TIMING")
        self.assertEqual('Shot "SH001" (S01) · cues 1-2 · 0:00.000 → 0:03.233 (3.2 s) · '
                         '"Most people think walking is simply about getting more steps."', first.message)

    def test_unused_assets_are_reported(self):
        manifest = sample_manifest()
        manifest = dataclasses.replace(manifest, assets=manifest.assets + [
            mf.VisualAsset("IMG004", "S01_B09_01.png", "S01")])
        _, cov, issues = mplan_ok(manifest=manifest)
        self.assertEqual(["IMG004 (S01_B09_01.png)"], cov.unused_assets)
        self.assertIn("MANIFEST_ASSET_UNUSED", [i.code for i in issues])

    def test_coverage_figures(self):
        _, cov, _ = mplan_ok()
        self.assertAlmostEqual(13.5, cov.duration_seconds)
        self.assertEqual(4, cov.shot_count)
        self.assertAlmostEqual(13.5 / 2, cov.average_seconds_per_source_change)  # 2 source changes
        self.assertAlmostEqual((100 + 101) / 30.0, cov.longest_same_source_seconds)
        self.assertAlmostEqual(107 / 30.0, cov.longest_shot_seconds)

    # ---- gap handling -------------------------------------------------
    @staticmethod
    def gap_manifest(shots):
        return mf.VisualManifest(
            "1.0", _vm(id="gap-test", title="Gap Test", duration_ms=60000, fps=30),
            [mf.VisualAsset("IMG001", "S01_B01_01.png", "S01", ["B01"], "closeup", ["STATIC"], False, None),
             mf.VisualAsset("IMG002", "S01_B02_01.png", "S01", ["B02"], "lifestyle", ["STATIC"], False, None)],
            [mf.VisualShot(f"SH{i + 1:03d}", s[0], s[1], "S01", f"B{i + 1:02d}", [i + 1],
                           f"Narration {i + 1}.", "new_point", "new_claim",
                           "IMG001" if i == 0 else "IMG002", mf.VisualFraming("wide", None),
                           mf.VisualMotion("STATIC", None, None, None), mf.VisualTransition("CUT", 0))
             for i, s in enumerate(shots)])

    def test_narration_pause_between_shots_is_held_by_the_previous_shot(self):
        tl, _, issues = mplan_ok(manifest=self.gap_manifest([[0, 3000], [3600, 6000]]), audio_frames=180)
        clips = tl.scenes[0].clips
        self.assertEqual([(0, 108), (108, 72)], [(c.start_frame, c.duration_frames) for c in clips])
        self.assertTrue([i for i in issues if i.code == "MANIFEST_TIMING_FIXED" and i.severity == m.INFO
                         and "Narration pause of 0.6s" in i.message])

    def test_lead_in_silence_extends_the_first_shot(self):
        tl, _, issues = mplan_ok(manifest=self.gap_manifest([[600, 3000], [3000, 6000]]), audio_frames=180)
        clips = tl.scenes[0].clips
        self.assertEqual([(0, 90), (90, 90)], [(c.start_frame, c.duration_frames) for c in clips])
        self.assertTrue([i for i in issues if i.code == "MANIFEST_TIMING_FIXED"
                         and "extended to cover from 0" in i.message])

    def test_overlapping_shot_windows_are_clamped(self):
        tl, _, issues = mplan_ok(manifest=self.gap_manifest([[0, 3000], [2700, 6000]]), audio_frames=180)
        clips = tl.scenes[0].clips
        self.assertEqual([(0, 90), (90, 90)], [(c.start_frame, c.duration_frames) for c in clips])
        self.assertTrue([i for i in issues if i.code == "MANIFEST_TIMING_FIXED" and i.severity == m.WARNING
                         and "Overlap before shot" in i.message and "(+9 frames)" in i.message])

    # ---- reveal + sfx (RevealTests.cs ideas) ---------------------------
    def reveal_manifest(self, **changes):
        manifest = sample_manifest()
        return with_shot(manifest, 2, motion=mf.VisualMotion("STATIC", None, None, None), **changes)

    def test_reveal_shot_expands_into_steps_with_crossfades(self):
        # SH003 spans frames 204..304; cues at 6.81s, 8.0s, 9.0s
        manifest = self.reveal_manifest(reveal_at_ms=[6810, 8000, 9000])
        tl, _, issues = mplan_ok(manifest=manifest)
        clips = tl.scenes[0].clips
        steps = [c for c in clips if c.reveal is not None]
        self.assertEqual(3, len(steps))
        self.assertEqual(["SH003", "SH003.2", "SH003.3"], [c.shot_id for c in steps])
        self.assertEqual([204, 240, 270], [c.start_frame for c in steps])
        self.assertEqual([36, 30, 34], [c.duration_frames for c in steps])
        self.assertEqual([1, 2, 3], [c.reveal.visible for c in steps])
        self.assertTrue(all(c.reveal.base_shot_id == "SH003" and c.reveal.count == 3 for c in steps))
        self.assertTrue(steps[2].file_path.endswith("S01_B03_01_INFO.png"))
        self.assertTrue(steps[0].file_path.endswith(os.path.join("out", "reveal", "S01_B03_01_INFO_reveal1of3.png")))
        self.assertEqual(8, steps[1].transition.duration_frames)  # round(0.25 s * 30 fps) = 8 (banker's)
        self.assertEqual(405, sum(c.duration_frames for c in clips))

    def test_reveal_needs_static_motion(self):
        manifest = with_shot(sample_manifest(), 3, reveal_at_ms=[10150, 11500])
        tl, _, issues = mplan_ok(manifest=manifest)
        self.assertIn("REVEAL_NEEDS_STATIC", [i.code for i in issues])
        self.assertTrue(all(c.reveal is None for c in tl.scenes[0].clips))

    def test_grid_reveal_with_wrong_count_falls_back_to_row(self):
        manifest = self.reveal_manifest(reveal_at_ms=[6810, 8000, 9000], reveal_layout="grid")
        tl, _, issues = mplan_ok(manifest=manifest)
        self.assertIn("REVEAL_GRID_NEEDS_FOUR", [i.code for i in issues])
        self.assertTrue(all(c.reveal.layout == "row" for c in tl.scenes[0].clips if c.reveal))

    def test_reveal_step_too_close_is_skipped(self):
        manifest = self.reveal_manifest(reveal_at_ms=[6810, 6900, 9000])
        tl, _, issues = mplan_ok(manifest=manifest)
        self.assertIn("REVEAL_STEP_SKIPPED", [i.code for i in issues])
        steps = [c for c in tl.scenes[0].clips if c.reveal]
        self.assertEqual([1, 3], [c.reveal.visible for c in steps])

    def test_sfx_plays_at_the_start_of_a_normal_shot_and_per_reveal_step(self):
        manifest = with_shot(sample_manifest(), 0, sfx=["whoosh"])
        manifest = with_shot(manifest, 2, motion=mf.VisualMotion("STATIC", None, None, None),
                             reveal_at_ms=[6810, 8000, 9000], sfx=["pop", "ding"])
        tl, _, _ = mplan_ok(manifest=manifest)
        self.assertEqual([("whoosh", 0), ("pop", 204), ("ding", 240), ("ding", 270)],
                         [(s.name, s.start_frame) for s in tl.sounds])

    def test_reveal_stack_uses_separate_images(self):
        manifest = sample_manifest()
        manifest = dataclasses.replace(manifest, assets=manifest.assets + [
            mf.VisualAsset("IMG004", "S01_B05_01.png", "S01")])
        manifest = with_shot(manifest, 2, motion=mf.VisualMotion("STATIC", None, None, None),
                             reveal_at_ms=[6810, 8000], reveal_assets=["IMG003", "IMG004"])
        imgs = images16() + [_image(os.path.join("images", "S01_B05_01.png"), 1920, 1080, index=0)]
        result = mplan(manifest=manifest, images=imgs)
        steps = [c for c in result.timeline.scenes[0].clips if c.reveal]
        self.assertEqual(2, len(steps))
        self.assertTrue(all(c.reveal.is_stack and len(c.reveal.sources) == 2 for c in steps))
        self.assertIn("_stack", os.path.basename(steps[0].file_path))
        self.assertIsNone(steps[0].reveal.source_path)


# ====================================================================== BuildReport
def sample_timeline():
    return m.Timeline(
        project_name="Video_003", fps=30,
        audio=m.AudioTrack("audio\\narration.mp3", 315),
        scenes=[
            m.Scene("S01", 0, 150, [m.VideoClip(
                file_path="images\\S01_01.png", scene_id="S01", start_frame=0, duration_frames=150,
                motion=m.ZOOM_IN, motion_source=m.AUTO_SELECTED, easing=m.EASE_IN_OUT,
                image_type="scene", transition=m.TransitionIn("crossfade", 15))]),
            m.Scene("S02", 150, 315, [m.VideoClip(
                file_path="images\\S02_01.png", scene_id="S02", start_frame=150, duration_frames=165,
                motion=m.PAN_RIGHT, motion_source=m.EXPLICIT_CODE, easing=m.LINEAR)])])


def create_report():
    return build_report.create("Video_003", sample_timeline(), ProjectOptions(), [
        m.ValidationIssue(m.WARNING, "TIMING_HOLD", "Scene S02 holds long."),
        m.ValidationIssue(m.INFO, "MOTION_EXPLICIT", "S02_01.png uses explicit motion PR.")])


class BuildReportTests(unittest.TestCase):
    def test_to_json_uses_snake_case_and_contains_plan_decisions(self):
        text = build_report.to_json(create_report())
        for needle in ('"schema_version": 1', '"project": "Video_003"', '"start_frame": 0',
                       '"duration_frames": 150', '"motion": "ZI"', '"motion_source": "ExplicitCode"',
                       '"transition_in": "Crossfade"', '"scene_boundary_kind"', '"TIMING_HOLD"',
                       '"audio_file": "audio\\\\narration.mp3"'):
            self.assertIn(needle, text)

    def test_to_json_is_deterministic(self):
        self.assertEqual(build_report.to_json(create_report()), build_report.to_json(create_report()))

    def test_create_sums_clip_coverage_into_total_frames(self):
        r = create_report()
        self.assertEqual(315, r.timeline["total_frames"])
        self.assertEqual(2, len(r.timeline["scenes"]))
        self.assertEqual(2, r.inventory["clip_count"])
        self.assertEqual(2, len(r.issues))

    def test_settings_use_display_names(self):
        s = create_report().settings
        self.assertEqual("Ease in out", s["easing"])
        self.assertEqual({"enabled": True, "kind": "Crossfade", "scene_boundary_kind": "Dip to black",
                          "alignment": "Late", "duration_frames": 15}, s["transitions"])

    def test_clip_without_type_or_transition_omits_the_keys(self):
        clip = create_report().timeline["scenes"][1]["clips"][0]
        self.assertNotIn("image_type", clip)
        self.assertNotIn("transition_in", clip)
        first = create_report().timeline["scenes"][0]["clips"][0]
        self.assertEqual("Scene", first["image_type"])

    def test_save_writes_the_file(self):
        project = TempProject()
        self.addCleanup(project.cleanup)
        path = os.path.join(project.path, "build-report.json")
        build_report.save(create_report(), path)
        with open(path, encoding="utf-8") as f:
            self.assertIn('"schema_version": 1', f.read())


if __name__ == "__main__":
    unittest.main()
