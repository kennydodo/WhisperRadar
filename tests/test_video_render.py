"""Port of the ffmpeg-side C# tests: PreviewRenderPlanFactoryTests, EncoderArgsTests,
PreviewRenderServiceTests, ToolLocatorTests, and the ffmpeg parts of RevealTests /
SoundAndSettingsOptionsTests, plus a real end-to-end render smoke test."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from whisperradar.video import models as m
from whisperradar.video import (encoder_probe, ffprobe, render_plan, render_project, reveal_images,
                                reveal_paths, sound_effects, timeline_json, tool_locator)
from whisperradar.video.ffmpeg_runner import FfmpegRunner
from whisperradar.video.options import ProjectOptions, RenderOptions
from whisperradar.video.render_service import PreviewRenderService

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
FIXTURES = Path(__file__).parent / "fixtures" / "video"


def opts():
    o = ProjectOptions()
    o.render.preview_width = 960
    o.render.preview_height = 540
    o.render.preview_preset = "veryfast"
    o.render.preview_crf = 28
    o.render.encoder = "libx264"
    return o


def arg_after(args, flag):
    i = args.index(flag)
    assert i >= 0 and i + 1 < len(args), flag
    return args[i + 1]


def img(path, w, h):
    return m.ImageInfo(path, m.ParsedImageName(Path(path).stem, 1, 1), w, h)


def sample_timeline():
    p1 = os.path.join("images", "S01_01.png")
    p2 = os.path.join("images", "S01_02_PR.png")
    images = [img(p1, 2304, 1296), img(p2, 2880, 1296)]
    tl = m.Timeline(project_name="test", fps=30, audio=m.AudioTrack(os.path.join("audio", "narration.mp3")))
    tl.scenes = [m.Scene("S01", 0, 180, [
        m.VideoClip(file_path=p1, scene_id="S01", start_frame=0, duration_frames=90, motion=m.ZOOM_IN,
                    start_viewport=m.Rect(192, 108, 1920, 1080),
                    end_viewport=m.Rect(252, 141.75, 1800, 1012.5)),
        m.VideoClip(file_path=p2, scene_id="S01", start_frame=90, duration_frames=90, motion=m.PAN_RIGHT,
                    motion_source=m.EXPLICIT_CODE,
                    start_viewport=m.Rect(0, 108, 1920, 1080), end_viewport=m.Rect(960, 108, 1920, 1080)),
    ])]
    return tl, images


def single_clip_timeline(path, frames, viewport=None):
    start = viewport or m.Rect(192, 108, 1920, 1080)
    end = viewport or m.Rect(252, 141.75, 1800, 1012.5)
    tl = m.Timeline(project_name="test", fps=30, audio=m.AudioTrack("audio/narration.mp3"))
    tl.scenes = [m.Scene("S01", 0, frames, [m.VideoClip(
        file_path=path, scene_id="S01", duration_frames=frames, motion=m.ZOOM_IN,
        start_viewport=start, end_viewport=end)])]
    return tl


class PlanFactoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.out = os.path.join(self.root, "out", "render")
        self.preview = os.path.join(self.root, "out", "preview.mp4")

    def build(self, tl, images, options=None):
        return render_plan.build(tl, images, options or opts(), self.out, self.preview)

    def test_one_segment_per_clip_with_encoder_settings(self):
        tl, images = sample_timeline()
        plan = self.build(tl, images)
        self.assertEqual(2, len(plan.segments))
        self.assertEqual(180, plan.total_frames)
        s = plan.segments[0]
        self.assertEqual(1, s.index)
        self.assertEqual(90, s.frame_count)
        self.assertTrue(s.output_path.endswith("seg_0001.mp4"))
        for tok in ("-frames:v", "90", "-preset", "veryfast", "-crf", "28", "-an"):
            self.assertIn(tok, s.arguments)
        self.assertGreater(s.arguments.index(s.output_path), 0)

    def test_zoom_in_filter_is_exact(self):
        tl, images = sample_timeline()
        vf = arg_after(self.build(tl, images).segments[0].arguments, "-vf")
        self.assertEqual(
            "scale=13824:7776:flags=lanczos,"
            "zoompan=z='min(1.28,max(1.2,13824/(11520+(-720)*((on+0)/89))))'"
            ":x='min(max(0,(6912+(0)*((on+0)/89))-iw/zoom/2),iw-iw/zoom)'"
            ":y='min(max(0,(3888+(0)*((on+0)/89))-ih/zoom/2),ih-ih/zoom)'"
            ":d=90:s=960x540:fps=30,format=yuv420p", vf)

    def test_pan_right_is_fixed_size_crop_on_supersampled_still(self):
        tl, images = sample_timeline()
        vf = arg_after(self.build(tl, images).segments[1].arguments, "-vf")
        self.assertEqual(
            "scale=14400:6480:flags=lanczos,"
            "loop=loop=89:size=1:start=0,"
            "crop=w='9600':h='5400'"
            ":x='min(iw-ow,max(0,0+(4800)*((n+0)/89)))'"
            ":y='min(ih-oh,max(0,540+(0)*((n+0)/89)))',"
            "scale=960:540,format=yuv420p", vf)

    def test_ease_in_shapes_progress(self):
        tl, images = sample_timeline()
        tl.scenes[0].clips[0].easing = m.EASE_IN
        vf = arg_after(self.build(tl, images).segments[0].arguments, "-vf")
        self.assertIn("pow(min(1,max(0,((on+0)/89))),2)", vf)
        self.assertIn("13824/(11520+(-720)*pow(min(1,max(0,((on+0)/89))),2))", vf)

    def _with_crossfade(self):
        tl, images = sample_timeline()
        tl.scenes[0].clips[1].transition = m.TransitionIn("crossfade", 15)
        return tl, images, self.build(tl, images)

    def test_transitions_produce_trimmed_pieces_and_joins(self):
        tl, images, plan = self._with_crossfade()
        self.assertEqual(3, len(plan.segments))
        self.assertEqual(180, plan.total_frames)
        self.assertTrue(plan.segments[0].output_path.endswith("seg_0001.mp4"))
        self.assertEqual(90, plan.segments[0].frame_count)
        self.assertTrue(plan.segments[1].output_path.endswith("join_0001.mp4"))
        self.assertEqual(15, plan.segments[1].frame_count)
        self.assertTrue(plan.segments[2].output_path.endswith("seg_0002.mp4"))
        self.assertEqual(75, plan.segments[2].frame_count)
        idx = [plan.concat_list_content.index(s.output_path) for s in plan.segments]
        self.assertTrue(idx[0] < idx[1] < idx[2])

    def test_join_arguments_blend_with_xfade(self):
        tl, images, plan = self._with_crossfade()
        join = plan.segments[1]
        self.assertEqual(2, join.arguments.count("-i"))
        self.assertIn(tl.scenes[0].clips[0].file_path, join.arguments)
        self.assertIn(tl.scenes[0].clips[1].file_path, join.arguments)
        fc = arg_after(join.arguments, "-filter_complex")
        for s in ("xfade=transition=fade:duration=0.5:offset=0", "[va]", "[vb]", "((on+90)/89)",
                  "((n+0)/89)", "loop=loop=14:size=1:start=0", "format=yuv420p"):
            self.assertIn(s, fc)
        self.assertIn("15", join.arguments)

    def test_join_incoming_pan_continues_without_jump(self):
        tl, images, plan = self._with_crossfade()
        self.assertIn("((n+0)/89)", arg_after(plan.segments[1].arguments, "-filter_complex"))
        self.assertIn("((n+15)/89)", arg_after(plan.segments[2].arguments, "-vf"))

    def test_old_preview_threshold_images_are_supersampled(self):
        p = os.path.join(self.root, "S01_01.png")
        tl = single_clip_timeline(p, 60)
        plan = self.build(tl, [img(p, 3840, 1296)])
        vf = arg_after(plan.segments[0].arguments, "-vf")
        self.assertIn("scale=15360:5184:flags=lanczos", vf)
        self.assertTrue(vf.startswith("scale="))

    def test_single_frame_clip_uses_constant_expressions(self):
        p = os.path.join(self.root, "S01_01.png")
        plan = self.build(single_clip_timeline(p, 1), [img(p, 2304, 1296)])
        vf = arg_after(plan.segments[0].arguments, "-vf")
        for s in ("z='1.2'", "x='1152'", "y='648'"):
            self.assertIn(s, vf)

    def test_concat_list_and_arguments(self):
        tl, images = sample_timeline()
        plan = self.build(tl, images)
        self.assertEqual("ffconcat version 1.0", plan.concat_list_content.split("\n")[0])
        for s in plan.segments:
            self.assertIn(f"file '{s.output_path}'", plan.concat_list_content)
        for tok in ("-f", "concat", "-safe", "0", "-c", "copy", plan.rough_path):
            self.assertIn(tok, plan.concat_arguments)

    def test_mux_arguments_map_narration_and_use_shortest(self):
        tl, images = sample_timeline()
        plan = self.build(tl, images)
        for tok in (self.preview, tl.audio.file_path, "-shortest", "aac"):
            self.assertIn(tok, plan.mux_arguments)
        self.assertTrue(plan.mux_arguments.index("1:a:0") > plan.mux_arguments.index("0:v:0") >= 0)

    def test_missing_dimensions_throw(self):
        p = os.path.join(self.root, "S01_01.png")
        with self.assertRaises(RuntimeError):
            self.build(single_clip_timeline(p, 60), [])

    def test_decimal_formatting_is_invariant_and_trimmed(self):
        p = os.path.join(self.root, "S01_01.png")
        tl = single_clip_timeline(p, 1, m.Rect(0, 0, 1920.25, 1080.1875))
        vf = arg_after(self.build(tl, [img(p, 2304, 1296)]).segments[0].arguments, "-vf")
        self.assertIn("crop=w='11521.5'", vf)
        self.assertIn("h='6481.125'", vf)
        self.assertNotIn("11521,5", vf)
        self.assertEqual("0.5", render_plan.F(0.5))
        self.assertEqual("30", render_plan.F(30.0))
        self.assertEqual("0.333333", render_plan.F(1 / 3))
        self.assertEqual("-720", render_plan.F(-720))

    def test_clip_preview_mux_arguments(self):
        a = render_plan.build_clip_preview_mux_arguments("v.mp4", "a.wav", 1.5, 2.0, "o.mp4")
        self.assertEqual(["-hide_banner", "-loglevel", "error", "-y", "-i", "v.mp4", "-ss", "1.5",
                          "-i", "a.wav", "-t", "2", "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                          "-c:a", "aac", "-b:a", "192k", "-shortest", "o.mp4"], a)

    def test_every_preset_maps_to_accepted_hardware_values(self):
        for preset in ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow",
                       "slower", "veryslow", "nonsense"):
            qsv = arg_after(render_plan.video_encoder_args("h264_qsv", preset, 28, 0), "-preset")
            self.assertIn(qsv, ["veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"])
            nv = arg_after(render_plan.video_encoder_args("h264_nvenc", preset, 28, 0), "-preset")
            self.assertRegex(nv, r"^p[1-7]$")
            amf = arg_after(render_plan.video_encoder_args("h264_amf", preset, 28, 0), "-quality")
            self.assertIn(amf, ["speed", "balanced", "quality"])

    def test_libx264_keeps_preset_and_crf(self):
        a = render_plan.video_encoder_args("libx264", "ultrafast", 23, 0)
        self.assertEqual(("ultrafast", "23", "0"),
                         (arg_after(a, "-preset"), arg_after(a, "-crf"), arg_after(a, "-bf")))

    def test_explicit_cpu_resolves_without_probe(self):
        r = RenderOptions(encoder="cpu")
        self.assertEqual("libx264", render_plan.resolve_encoder(r))

    def test_golden_timelines_build_plans(self):
        for case in ("fees", "animals", "more_to_live"):
            with self.subTest(case=case):
                d = FIXTURES / case
                tl = timeline_json.load(d / "timeline.json")
                sizes = json.loads((d / "images.json").read_text(encoding="utf-8"))
                options = ProjectOptions()
                options.render.encoder = "libx264"
                images = []
                for sc in tl.scenes:
                    for c in sc.clips:
                        base = c.file_path.replace("\\", "/").split("/")[-1]
                        w, h = sizes.get(base, (2560, 1440))
                        c.file_path = os.path.join(self.root, base)
                        images.append(img(c.file_path, w, h))
                plan = render_plan.build(tl, images, options, self.out, self.preview)
                clips = [c for sc in tl.scenes for c in sc.clips]
                self.assertEqual(sum(c.duration_frames for c in clips), plan.total_frames)
                self.assertEqual(plan.total_frames, sum(s.frame_count for s in plan.segments))
                for s in plan.segments:
                    self.assertTrue(all(isinstance(a, str) for a in s.arguments))
                    self.assertNotIn("None", " ".join(s.arguments))


class EncoderArgsTests(unittest.TestCase):
    @staticmethod
    def args(render):
        clip = m.VideoClip(file_path="images/S01_01.png", scene_id="S01", duration_frames=60,
                           motion=m.ZOOM_IN, start_viewport=m.Rect(192, 108, 1920, 1080),
                           end_viewport=m.Rect(246, 138, 1811, 1019))
        o = ProjectOptions()
        o.render = render
        return render_plan.build_clip_preview_arguments(clip, 2304, 1296, o, "out.mp4")

    def test_libx264_default(self):
        a = self.args(RenderOptions(encoder="libx264", preview_preset="veryfast", preview_crf=28))
        self.assertEqual("libx264", arg_after(a, "-c:v"))
        self.assertIn("veryfast", a)
        self.assertIn("28", a)
        self.assertNotIn("h264_nvenc", a)

    def test_nvenc_cq(self):
        a = self.args(RenderOptions(encoder="h264_nvenc", preview_preset="veryfast", preview_crf=28))
        self.assertEqual("h264_nvenc", arg_after(a, "-c:v"))
        self.assertEqual("vbr", arg_after(a, "-rc"))
        self.assertEqual("28", arg_after(a, "-cq"))
        self.assertEqual("p3", arg_after(a, "-preset"))

    def test_qsv_and_amf(self):
        q = self.args(RenderOptions(encoder="h264_qsv", preview_preset="slow", preview_crf=20))
        self.assertIn("h264_qsv", q)
        self.assertEqual("slow", arg_after(q, "-preset"))
        self.assertEqual("20", arg_after(q, "-global_quality"))
        a = self.args(RenderOptions(encoder="h264_amf", preview_preset="slow", preview_crf=20))
        self.assertEqual("quality", arg_after(a, "-quality"))
        self.assertEqual("20", arg_after(a, "-qp_i"))

    def test_bframes(self):
        self.assertEqual("0", arg_after(self.args(RenderOptions(encoder="libx264")), "-bf"))
        self.assertEqual("3", arg_after(self.args(RenderOptions(encoder="libx264", preview_bframes=3)), "-bf"))
        for enc in ("h264_nvenc", "h264_qsv", "h264_amf"):
            self.assertEqual("0", arg_after(self.args(RenderOptions(encoder=enc, preview_bframes=0)), "-bf"))

    def test_supersample_reaches_target_grid(self):
        for w, exp in ((1376, 4), (1920, 3), (2304, 2), (2880, 2), (3840, 2), (5504, 1)):
            self.assertEqual(exp, render_plan.supersample_for(w, 4608), w)
        for w, exp in ((2560, 5), (3840, 4), (1280, 8), (640, 8), (17000, 1), (13000, 1)):
            self.assertEqual(exp, render_plan.supersample_for(w, 12288), w)
        self.assertEqual(1, render_plan.supersample_for(1376, 0))

    def test_supersample_target_never_below_render_ratio(self):
        o = ProjectOptions()
        o.render.supersample_target_width = 4608
        o.render.preview_width = 960
        self.assertEqual(4608, render_plan.supersample_target_for(o))
        o.render.preview_width = 2560
        self.assertEqual(12288, render_plan.supersample_target_for(o))
        o.render.supersample_target_width = 0
        self.assertEqual(0, render_plan.supersample_target_for(o))

    def _args_for(self, w, h):
        clip = m.VideoClip(file_path="images/x.png", scene_id="S01", duration_frames=60, motion=m.ZOOM_IN,
                           start_viewport=m.Rect(0, 0, w, h),
                           end_viewport=m.Rect(w * 0.02, h * 0.02, w * 0.96, h * 0.96))
        return render_plan.build_clip_preview_arguments(clip, w, h, ProjectOptions(), "out.mp4")

    def test_low_res_sources_supersampled_to_grid(self):
        for w, h, exp in ((1376, 768, "scale=11008:6144"), (1920, 1080, "scale=13440:7560"),
                          (2304, 1296, "scale=13824:7776")):
            self.assertIn(exp, arg_after(self._args_for(w, h), "-vf"))

    def test_large_sources_not_supersampled(self):
        self.assertNotIn("scale=", arg_after(self._args_for(13000, 7313), "-vf"))

    def test_resolve_encoder_falls_back_to_cpu_when_ffmpeg_missing(self):
        r = RenderOptions(encoder="auto", ffmpeg_path="ffmpeg-definitely-missing-xyz")
        self.assertEqual("libx264", render_plan.resolve_encoder(r))
        self.assertEqual("libx264", render_plan.resolve_encoder(RenderOptions(encoder="cpu")))

    def test_final_render_options_substitution(self):
        o = ProjectOptions()
        o.render.encoder = "h264_nvenc"
        o.render.final_preset = "slow"
        o.render.final_crf = 18
        f = render_project.final_render_options(o)
        self.assertEqual((2560, 1440), (f.render.preview_width, f.render.preview_height))
        self.assertEqual(3, f.render.preview_bframes)
        self.assertEqual(960, o.render.preview_width)  # original untouched
        a = self.args(f.render)
        self.assertEqual("p6", arg_after(a, "-preset"))
        self.assertEqual("18", arg_after(a, "-cq"))


class ToolLocatorTests(unittest.TestCase):
    def test_rooted_existing_path_returned_as_is(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(d, tool_locator.resolve("ffmpeg", d))

    def test_rooted_missing_path_returned_as_is(self):
        self.assertEqual("Z:\\nowhere\\ffmpeg.exe", tool_locator.resolve("ffmpeg", "Z:\\nowhere\\ffmpeg.exe"))
        self.assertEqual("/nowhere/ffmpeg", tool_locator.resolve("ffmpeg", "/nowhere/ffmpeg"))

    def test_resolves_tool_from_path(self):
        sh = tool_locator.resolve("sh", "sh") if os.name != "nt" else tool_locator.resolve("cmd", "cmd")
        self.assertTrue(os.path.isabs(sh))

    def test_unknown_tool_returns_name(self):
        self.assertEqual("nope-xyz", tool_locator.resolve("ffmpeg", "nope-xyz"))
        self.assertEqual([], tool_locator.resolve_all_candidates("ffmpeg", "nope-xyz"))


class MuxAndSoundTests(unittest.TestCase):
    sounds = [m.SoundEffect("pop", "p.wav", 30, 30), m.SoundEffect("pop", "p.wav", 45, 30),
              m.SoundEffect("ding", "d.wav", 120, 30)]

    def test_preview_mix_delays_each_sound(self):
        args = render_plan.build_mux_arguments("rough.mp4", "n.wav", "out.mp4", self.sounds, 30)
        g = arg_after(args, "-filter_complex")
        for s in ("adelay=1000:all=1", "adelay=1500:all=1", "adelay=4000:all=1",
                  "amix=inputs=4:duration=first"):
            self.assertIn(s, g)
        self.assertIn("volume=0.8[s0]", g)
        self.assertEqual("[aout]", args[len(args) - 1 - args[::-1].index("-map") + 1])

    def test_mix_without_sounds_is_plain_mux(self):
        args = render_plan.build_mux_arguments("rough.mp4", "n.wav", "out.mp4")
        self.assertNotIn("-filter_complex", args)
        self.assertEqual("1:a:0", args[len(args) - 1 - args[::-1].index("-map") + 1])

    def test_built_in_sounds_and_case_insensitivity(self):
        self.assertTrue(sound_effects.is_built_in("Pop"))
        self.assertTrue(sound_effects.is_built_in("whoosh"))
        self.assertFalse(sound_effects.is_built_in("nonsense"))
        self.assertIsNotNone(sound_effects.build_arguments("ding", "o.wav"))

    def test_every_catalog_pop_variant_has_recipe(self):
        from whisperradar.video.options import POP_VARIANTS
        self.assertGreaterEqual(len(POP_VARIANTS), 10)
        for pid, _ in POP_VARIANTS:
            self.assertTrue(sound_effects.is_built_in(pid), pid)
            self.assertIsNotNone(sound_effects.build_arguments(pid, "o.wav"))

    def test_pop_alias_follows_default_pop(self):
        r = sound_effects.resolve_alias
        self.assertEqual("pop_marimba", r("Pop", "pop_marimba"))
        self.assertEqual("pop_bubble", r("pop", None))
        self.assertEqual("pop_bubble", r("pop", "nonsense"))
        self.assertEqual("ding", r("ding", "pop_marimba"))
        self.assertIn("aevalsrc", " ".join(sound_effects.build_arguments("pop", "o.wav", "pop_blip")))

    def test_default_pop_option_validates(self):
        o = ProjectOptions()
        self.assertFalse([e for e in o.validate() if "pop" in e.lower()])
        o.sound.default_pop = "pop_ping"
        self.assertFalse([e for e in o.validate() if "pop" in e.lower()])
        o.sound.default_pop = "nope"
        self.assertTrue([e for e in o.validate() if "pop" in e.lower()])

    def test_resolver_with_fake_runner_and_probe(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "sfx"))
            Path(d, "sfx", "mine.wav").write_bytes(b"x")
            tl = m.Timeline(fps=30)
            tl.sounds = [m.SoundEffect("mine", "", 10), m.SoundEffect("Mine", "", 40),
                         m.SoundEffect("nonsense_zzz", "", 50)]
            issues = sound_effects.resolve(tl, d, FfmpegRunner("ffmpeg-nope"), probe=lambda p: 4.0)
            self.assertEqual(2, len(tl.sounds))
            self.assertEqual(120, tl.sounds[0].duration_frames)
            self.assertTrue(tl.sounds[0].file_path.endswith("mine.wav"))
            self.assertEqual(["SFX_LONG", "SFX_UNKNOWN"], sorted(i.code for i in issues))


class RevealTests(unittest.TestCase):
    def test_step_files_named_next_to_images_folder(self):
        src = os.path.join("proj", "images", "S02_03_CMP_ST.png")
        p = reveal_paths.step_path(src, 2, 3)
        self.assertTrue(p.endswith(os.path.join("proj", "out", "reveal", "S02_03_CMP_ST_reveal2of3.png")))
        self.assertEqual(src, reveal_paths.step_path(src, 3, 3))
        self.assertTrue(reveal_paths.step_path(src, 1, 4, "grid").endswith("S02_03_CMP_ST_grid1of4.png"))

    def test_stack_path_hash(self):
        import hashlib
        p = reveal_paths.stack_path(["/a/images/X.png", "/a/images/Y.png"], 1, "row")
        h = hashlib.sha1(b"x.png|y.png|row").hexdigest()[:6]
        self.assertTrue(p.endswith(f"X_stack{h}_1of2.png"))

    def test_row_arguments(self):
        a = reveal_images.build_arguments("in.png", 2880, 1080, 2, 3, "out.png")
        self.assertEqual("crop=1920:1080:0:0,pad=2880:1080:0:0:color=black", arg_after(a, "-vf"))

    def test_grid_arguments(self):
        one = reveal_images.build_grid_arguments("in.png", 1920, 1080, 1, "o.png")
        self.assertEqual("drawbox=x=960:y=0:w=960:h=540:color=black:t=fill,"
                         "drawbox=x=0:y=540:w=960:h=540:color=black:t=fill,"
                         "drawbox=x=960:y=540:w=960:h=540:color=black:t=fill", arg_after(one, "-vf"))
        three = reveal_images.build_grid_arguments("in.png", 1920, 1080, 3, "o.png")
        self.assertEqual("drawbox=x=960:y=540:w=960:h=540:color=black:t=fill", arg_after(three, "-vf"))
        self.assertEqual("null", arg_after(reveal_images.build_grid_arguments("i", 8, 8, 4, "o"), "-vf"))

    def test_slot_rects(self):
        self.assertEqual([(0, 0, 640, 1080), (640, 0, 640, 1080), (1280, 0, 640, 1080)],
                         reveal_images.slot_rects(1920, 1080, 3, "row"))
        self.assertEqual([(0, 0, 960, 540), (960, 0, 960, 540), (0, 540, 960, 540), (960, 540, 960, 540)],
                         reveal_images.slot_rects(1920, 1080, 4, "grid"))

    def test_stack_arguments(self):
        a = reveal_images.build_stack_arguments(["a.png", "b.png"], 1920, 1080, 3, "row", "o.png")
        self.assertEqual(
            "[1:v]scale=640:1080:force_original_aspect_ratio=increase,crop=640:1080,setsar=1[s0];"
            "[0:v][s0]overlay=0:0[o0];"
            "[2:v]scale=640:1080:force_original_aspect_ratio=increase,crop=640:1080,setsar=1[s1];"
            "[o0][s1]overlay=640:0[o1]", arg_after(a, "-filter_complex"))
        self.assertEqual("[o1]", arg_after(a, "-map"))


class RunnerTests(unittest.TestCase):
    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg missing")
    def test_runs_ffmpeg_version(self):
        r = FfmpegRunner().run(["-version"])
        self.assertTrue(r.success)
        self.assertIn("ffmpeg", r.standard_output)

    def test_missing_ffmpeg_raises_actionable_error(self):
        with self.assertRaises(RuntimeError) as cm:
            FfmpegRunner("ffmpeg-definitely-not-on-path-xyz").run(["-version"])
        self.assertIn("Install ffmpeg", str(cm.exception))

    def test_cancel_kills_process(self):
        import threading
        from whisperradar.video.ffmpeg_runner import RenderCancelled
        ev = threading.Event()
        ev.set()
        runner = FfmpegRunner("sleep")
        runner.ffmpeg_path = shutil.which("sleep") or "sleep"
        if not shutil.which("sleep"):
            self.skipTest("no sleep")
        with self.assertRaises(RenderCancelled):
            runner.run(["5"], cancel_event=ev)

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg missing")
    def test_encoder_probe_libx264(self):
        self.assertIn("libx264", encoder_probe.get_encoders("ffmpeg"))
        self.assertTrue(encoder_probe.supports("ffmpeg", "libx264"))
        self.assertFalse(encoder_probe.supports("ffmpeg", "no_such_encoder"))


def ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def probe_stream(path, entries, stream="v:0"):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries",
                          f"stream={entries}", "-of", "json", path], check=True,
                         capture_output=True, text=True).stdout
    return json.loads(out)["streams"][0]


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe missing")
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        os.makedirs(os.path.join(self.root, "images"))
        os.makedirs(os.path.join(self.root, "audio"))
        self.imgs = []
        for name, w, h, color in (("S01_01.png", 640, 360, "red"), ("S01_02_PL.png", 800, 360, "green"),
                                  ("S02_01.png", 640, 360, "blue")):
            p = os.path.join(self.root, "images", name)
            ff("-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:d=1:r=1,drawbox=c={color}@0.5:t=fill",
               "-frames:v", "1", p)
            self.imgs.append(img(p, w, h))
        self.audio = os.path.join(self.root, "audio", "narration.wav")
        ff("-f", "lavfi", "-i", "sine=f=440:d=3", "-ar", "48000", self.audio)

    def make_timeline(self):
        a, b, c = (i.file_path for i in self.imgs)
        tl = m.Timeline(project_name="e2e", resolution=m.Resolution(320, 180), fps=30,
                        audio=m.AudioTrack(self.audio, 90))
        tl.scenes = [
            m.Scene("S01", 0, 60, [
                m.VideoClip(file_path=a, scene_id="S01", start_frame=0, duration_frames=30, motion=m.ZOOM_IN,
                            start_viewport=m.Rect(0, 0, 640, 360), end_viewport=m.Rect(32, 18, 576, 324)),
                m.VideoClip(file_path=b, scene_id="S01", start_frame=30, duration_frames=30, motion=m.PAN_LEFT,
                            start_viewport=m.Rect(160, 0, 640, 360), end_viewport=m.Rect(0, 0, 640, 360),
                            transition=m.TransitionIn("crossfade", 8))]),
            m.Scene("S02", 60, 90, [
                m.VideoClip(file_path=c, scene_id="S02", start_frame=60, duration_frames=30,
                            motion=m.STATIC, start_viewport=m.Rect(0, 0, 640, 360),
                            end_viewport=m.Rect(0, 0, 640, 360))])]
        tl.sounds = [m.SoundEffect("pop", "", 20, 0)]
        return tl

    def test_end_to_end_preview_render(self):
        tl = self.make_timeline()
        o = ProjectOptions()
        o.output.width, o.output.height = 320, 180
        o.render.preview_width, o.render.preview_height = 320, 180
        o.render.preview_preset, o.render.preview_crf = "ultrafast", 35
        o.render.encoder = "libx264"
        o.render.supersample_target_width = 0
        runner = FfmpegRunner(render_plan.resolve_effective_ffmpeg_path(o.render))
        issues = sound_effects.resolve(tl, self.root, runner, o.render.ffprobe_path, o.sound.default_pop)
        self.assertEqual([], [i for i in issues if i.severity != m.INFO])
        self.assertTrue(os.path.isfile(tl.sounds[0].file_path))
        out = os.path.join(self.root, "out", "preview.mp4")
        plan = render_plan.build(tl, self.imgs, o, os.path.join(self.root, "out", "render"), out)
        self.assertEqual(90, plan.total_frames)
        progress = []
        result = PreviewRenderService(runner).render(plan, 2, progress.append)
        self.assertTrue(result.success, "\n".join(result.errors))
        self.assertTrue(os.path.isfile(out))
        self.assertAlmostEqual(1.0, progress[-1])
        v = probe_stream(out, "width,height,r_frame_rate,nb_frames")
        self.assertEqual((320, 180), (v["width"], v["height"]))
        self.assertEqual("30/1", v["r_frame_rate"])
        dur = ffprobe.ffprobe_duration_seconds(out)
        self.assertLessEqual(abs(dur - 3.0), 1 / 30 + 0.05)  # narration is the shortest stream (3 s)
        a = probe_stream(out, "codec_name", "a:0")
        self.assertEqual("aac", a["codec_name"])

    def test_render_final_from_timeline_json_preview_and_final(self):
        tl = self.make_timeline()
        tl.sounds = []
        os.makedirs(os.path.join(self.root, "out"))
        timeline_json.save(tl, os.path.join(self.root, "out", "timeline.json"))
        Path(self.root, "narration.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
        o = ProjectOptions()
        o.output.width, o.output.height = 320, 180
        o.render.preview_width, o.render.preview_height = 160, 90
        o.render.preview_preset, o.render.preview_crf = "ultrafast", 40
        o.render.final_preset, o.render.final_crf, o.render.final_bframes = "ultrafast", 35, 0
        o.render.encoder = "libx264"
        o.render.supersample_target_width = 0
        from whisperradar.video import options as om
        om.save(o, os.path.join(self.root, "imgtovideo.json"))

        ok, path, errs = render_project.render_final(self.root, True)
        self.assertTrue(ok, errs)
        self.assertEqual(os.path.join(self.root, "out", "preview.mp4"), path)
        self.assertEqual(160, probe_stream(path, "width")["width"])

        ok, path, errs = render_project.render_final(self.root, False)
        self.assertTrue(ok, errs)
        self.assertEqual(os.path.join(self.root, "out", "final", "final.mp4"), path)
        self.assertEqual(320, probe_stream(path, "width")["width"])
        self.assertTrue(os.path.isfile(os.path.join(self.root, "out", "final", "captions.srt")))

    def test_render_final_without_timeline_reports_error(self):
        ok, path, errs = render_project.render_final(self.root, True)
        self.assertFalse(ok)
        self.assertTrue(errs)

    def test_segment_reuse(self):
        tl = self.make_timeline()
        tl.sounds = []
        o = ProjectOptions()
        o.output.width, o.output.height = 320, 180
        o.render.preview_width, o.render.preview_height = 160, 90
        o.render.preview_preset, o.render.preview_crf = "ultrafast", 40
        o.render.encoder = "libx264"
        o.render.supersample_target_width = 0
        runner = FfmpegRunner("ffmpeg")
        plan = render_plan.build(tl, self.imgs, o, os.path.join(self.root, "out", "render"),
                                 os.path.join(self.root, "out", "preview.mp4"))
        svc = PreviewRenderService(runner)
        self.assertTrue(svc.render(plan, 2, None, None, True).success)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "out", "render", "render-manifest.json")))
        seg = plan.segments[0].output_path
        t1 = os.stat(seg).st_mtime_ns
        self.assertTrue(svc.render(plan, 2, None, None, True).success)
        self.assertEqual(t1, os.stat(seg).st_mtime_ns)
        os.utime(self.imgs[0].file_path, (t1 / 1e9 + 100, t1 / 1e9 + 100))
        self.assertTrue(svc.render(plan, 2, None, None, True).success)
        self.assertGreater(os.stat(seg).st_mtime_ns, t1)

    def test_reveal_still_and_sfx_generation(self):
        src = self.imgs[0]
        tl = single_clip_timeline(os.path.join(self.root, "out", "reveal", "S01_01_reveal1of2.png"), 30)
        tl.resolution = m.Resolution(640, 360)
        tl.scenes[0].clips[0].reveal = m.RevealInfo("row", 2, 1, source_path=src.file_path)
        self.assertTrue(reveal_images.needs_images(tl, [src]))
        out = reveal_images.ensure(tl, [src], FfmpegRunner("ffmpeg"))
        self.assertEqual(2, len(out))
        self.assertTrue(os.path.isfile(out[1].file_path))
        # the partial still is padded back to the full source width
        self.assertEqual(640, probe_stream(out[1].file_path, "width")["width"])
        self.assertFalse(reveal_images.needs_images(tl, out))
        p = sound_effects.ensure_built_in(self.root, "pop", FfmpegRunner("ffmpeg"), "pop_blip")
        self.assertTrue(p.endswith(os.path.join("out", "sfx", "pop_blip.wav")))
        self.assertGreater(os.path.getsize(p), 100)


if __name__ == "__main__":
    unittest.main()
