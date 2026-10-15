"""Renderer: shots backed by an AI video clip (animated opening)."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar.video import models as m, options as o  # noqa: E402
from whisperradar.video import render_plan as rp, render_project  # noqa: E402

FULL = m.Rect(0, 0, 1920, 1080)


def _clip(frames, video=None, path="a.png"):
    return m.VideoClip(file_path=path, duration_frames=frames, video_path=video,
                       start_viewport=FULL, end_viewport=FULL)


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.opt = o.ProjectOptions()

    def test_video_clip_uses_plain_input_not_a_looped_still(self):
        args = rp._clip_input_arguments(_clip(150, "a.mp4"), self.opt)
        self.assertEqual(args, ["-i", "a.mp4"])
        still = rp._clip_input_arguments(_clip(150), self.opt)
        self.assertIn("-loop", still)

    def test_short_shot_plays_at_normal_speed(self):
        chain = rp._build_video_chain(_clip(150, "a.mp4"), 0, 150, self.opt)
        self.assertIn("trim=duration=5,setpts=(PTS-STARTPTS)/1,", chain)

    def test_nine_second_shot_is_slowed_not_held(self):
        chain = rp._build_video_chain(_clip(270, "a.mp4"), 0, 270, self.opt)
        self.assertIn("trim=duration=8,setpts=(PTS-STARTPTS)/0.8889,", chain)

    def test_long_shot_plays_at_min_speed_then_holds(self):
        chain = rp._build_video_chain(_clip(420, "a.mp4"), 0, 420, self.opt)
        self.assertIn("/0.8,", chain)
        self.assertIn("tpad=stop_mode=clone", chain)

    def test_piece_range_is_cut_like_a_still(self):
        chain = rp._build_video_chain(_clip(300, "a.mp4"), 10, 60, self.opt)
        self.assertIn("trim=start_frame=10:end_frame=70", chain)

    def test_side_chain_dispatches_to_video_only_with_a_clip(self):
        v = rp._build_side_chain(_clip(150, "a.mp4"), 1920, 1080, 0, 150, self.opt)
        s = rp._build_side_chain(_clip(150), 1920, 1080, 0, 150, self.opt)
        self.assertTrue(v.startswith("trim=duration"))
        self.assertFalse(s.startswith("trim=duration"))

    def test_clip_audio_is_dropped(self):
        c = _clip(150, "a.mp4")
        args = rp._wrap_segment_arguments(
            rp._clip_input_arguments(c, self.opt), None,
            rp._build_side_chain(c, 1920, 1080, 0, 150, self.opt), 150,
            self.opt.render, "out.mp4")
        self.assertIn("-an", args)


class TimelineTests(unittest.TestCase):
    def test_video_path_round_trips(self):
        c = _clip(90, "x.mp4")
        self.assertEqual(m.VideoClip.from_dict(c.to_dict()).video_path, "x.mp4")
        self.assertNotIn("video_path", _clip(90).to_dict())

    def test_attach_finds_clips_by_image_stem(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "clips").mkdir()
            (Path(d) / "clips" / "S01_SCN_ZI.mp4").write_bytes(b"x" * 20000)
            (Path(d) / "clips" / "S02_SCN_ZI.mp4").write_bytes(b"x")   # too small
            tl = m.Timeline(scenes=[m.Scene(id="1", clips=[
                _clip(90, path=str(Path(d) / "images" / "S01_SCN_ZI.png")),
                _clip(90, path=str(Path(d) / "images" / "S02_SCN_ZI.png")),
                _clip(90, path=str(Path(d) / "images" / "S03_SCN_ZI.png"))])])
            n = render_project.attach_motion_clips(tl, d)
            got = [c.video_path for c in tl.scenes[0].clips]
        self.assertEqual(n, 1)
        self.assertTrue(got[0].endswith("S01_SCN_ZI.mp4"))
        self.assertIsNone(got[1])
        self.assertIsNone(got[2])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg missing")
class FrameCountTests(unittest.TestCase):
    def test_every_shot_length_renders_exactly_its_frames(self):
        opt = o.ProjectOptions()
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "c.mp4")
            subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc=size=1280x720:rate=24:duration=8",
                            "-pix_fmt", "yuv420p", src], check=True)
            for secs in (5, 9, 14):
                frames = secs * 30
                c = _clip(frames, src)
                out = os.path.join(d, f"s{secs}.mp4")
                args = rp._wrap_segment_arguments(
                    rp._clip_input_arguments(c, opt), None,
                    rp._build_side_chain(c, 1920, 1080, 0, frames, opt),
                    frames, opt.render, out)
                subprocess.run(["ffmpeg"] + args, check=True,
                               capture_output=True)
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-count_frames", "-select_streams",
                     "v:0", "-show_entries", "stream=nb_read_frames", "-of",
                     "csv=p=0", out], capture_output=True, text=True)
                self.assertEqual(int(probe.stdout.strip()), frames, secs)


if __name__ == "__main__":
    unittest.main()


class AttachGateTests(unittest.TestCase):
    def _setup(self, d):
        (Path(d) / "clips").mkdir()
        (Path(d) / "images").mkdir()
        img = Path(d) / "images" / "S01_SCN_ZI.png"
        img.write_bytes(b"i")
        clip = Path(d) / "clips" / "S01_SCN_ZI.mp4"
        clip.write_bytes(b"x" * 20000)
        tl = m.Timeline(scenes=[m.Scene(id="1", clips=[_clip(90, path=str(img))])])
        return tl, img, clip

    def test_off_channel_ignores_leftover_clips(self):
        with tempfile.TemporaryDirectory() as d:
            tl, _img, _clip_p = self._setup(d)
            (Path(d) / "motion_gate.json").write_text('{"enabled": false}')
            self.assertEqual(render_project.attach_motion_clips(tl, d), 0)
            self.assertIsNone(tl.scenes[0].clips[0].video_path)

    def test_on_channel_uses_the_clip(self):
        with tempfile.TemporaryDirectory() as d:
            tl, _img, _clip_p = self._setup(d)
            (Path(d) / "motion_gate.json").write_text('{"enabled": true}')
            self.assertEqual(render_project.attach_motion_clips(tl, d), 1)

    def test_clip_older_than_its_regenerated_still_is_skipped(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            tl, img, clip = self._setup(d)
            old = os.path.getmtime(clip) - 600
            os.utime(clip, (old, old))      # clip made long before the still
            self.assertEqual(render_project.attach_motion_clips(tl, d), 0)

    def test_shots_past_the_current_window_keep_the_still(self):
        """animate_seconds was 60 when the clips were made, then the channel
        shrank the opening to 30s: the shot at 40s must not play its clip."""
        with tempfile.TemporaryDirectory() as d:
            tl, img, clip = self._setup(d)
            late = _clip(90, path=str(img))
            late.start_frame = 1200           # 40 s at 30 fps
            tl.scenes[0].clips.append(late)
            (Path(d) / "motion_gate.json").write_text(
                '{"enabled": true, "window_seconds": 30}')
            self.assertEqual(render_project.attach_motion_clips(tl, d), 1)
            self.assertIsNotNone(tl.scenes[0].clips[0].video_path)
            self.assertIsNone(tl.scenes[0].clips[1].video_path)

    def test_no_window_key_attaches_everywhere(self):
        with tempfile.TemporaryDirectory() as d:
            tl, img, clip = self._setup(d)
            late = _clip(90, path=str(img))
            late.start_frame = 3000
            tl.scenes[0].clips.append(late)
            (Path(d) / "motion_gate.json").write_text('{"enabled": true}')
            self.assertEqual(render_project.attach_motion_clips(tl, d), 2)


class HoldWarningTests(unittest.TestCase):
    def test_padding_counts_toward_the_hold_limit(self):
        from whisperradar import motion
        data = {"shots": [{"asset": "S01_SCN_ZI.png", "cues": "1", "motion": "ZI",
                           "ai_motion": "slow push"}]}
        cues = [{"index": 1, "start": 0, "end": 9.8}]
        _f, warns, _a = motion.window_check(data, cues, 60, lambda v: float(v))
        self.assertTrue(warns, "9.8s + 0.5s padding exceeds the 10s stretch")
        cues = [{"index": 1, "start": 0, "end": 9.0}]
        _f, warns, _a = motion.window_check(data, cues, 60, lambda v: float(v))
        self.assertFalse(warns)
