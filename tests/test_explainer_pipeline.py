"""Phase 3 integration: explainer.json -> plan_project -> render_final.

Runs the REAL engine pipeline on a synthetic explainer production (no images,
no AI clips, no paid APIs). Legacy fixtures are untouched.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(REPO, "docs", "explainer", "demo", "silent-timing-demo")

SRT = """1
00:00:00,000 --> 00:00:02,000
one

2
00:00:02,000 --> 00:00:04,000
two

3
00:00:04,000 --> 00:00:06,000
three
"""

DOC = {
    "schema_version": 1,
    "theme": "finance_dark",
    "canvas": {"width": 1280, "height": 720},
    "scenes": [
        {"id": "s1", "shot_id": "S01_01", "cue_range": [1, 2],
         "objects": [
             {"id": "t1", "type": "text", "text": "Hello explainer",
              "box": [0.1, 0.1, 0.8, 0.2], "style": "heading"},
             {"id": "p1", "type": "path", "points": [[0.2, 0.5], [0.6, 0.5]],
              "arrow": True, "box": [0, 0, 1, 1], "style": "accent"},
         ],
         "events": [
             {"target": "t1", "action": "fade_in",
              "at": {"cue": 1, "edge": "start"}, "duration_ms": 300},
             {"target": "p1", "action": "draw",
              "at": {"cue": 2, "edge": "start"}, "duration_ms": 400},
         ]},
        {"id": "s2", "shot_id": "S02_01", "cue_range": [3, 3],
         "objects": [
             {"id": "c1", "type": "counter", "from": 0, "to": 120,
              "format": "currency_usd", "box": [0.3, 0.4, 0.3, 0.15],
              "style": "stat"},
         ],
         "events": [
             {"target": "c1", "action": "count",
              "at": {"cue": 3, "edge": "start"}, "duration_ms": 800},
         ]},
    ],
}


def _ffmpeg_audio(path, seconds=6.0):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={seconds}",
         "-c:a", "libmp3lame", "-b:a", "64k", path],
        check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _probe(path, entries="stream=codec_type,width,height,nb_frames"):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entries,
         "-of", "json", path],
        capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return json.loads(out.stdout or "{}")


class ExplainerPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="explainer_pipe_")
        with open(os.path.join(cls.tmp, "subtitles.srt"), "w",
                  encoding="utf-8") as fh:
            fh.write(SRT)
        with open(os.path.join(cls.tmp, "explainer.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(DOC, fh)
        _ffmpeg_audio(os.path.join(cls.tmp, "audio.mp3"))
        # a stray image must be ignored by explainer mode
        shutil.copyfile(os.path.join(DEMO, "explainer.json"),
                        os.path.join(cls.tmp, "explainer.json.copy"))
        from whisperradar.video import plan as plan_mod, render_project
        result = plan_mod.plan_project(cls.tmp)
        cls.plan_result = result
        assert result.success, [i.message for i in result.issues
                                if i.severity == "Error"]
        cls.timeline = result.timeline
        ok, out, errors = render_project.render_final(cls.tmp, preview=True)
        cls.render_ok, cls.render_out, cls.render_errors = ok, out, errors

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_plan_builds_explainer_timeline(self):
        self.assertTrue(self.plan_result.success)
        self.assertEqual(len(self.timeline.scenes), 2)
        clip = self.timeline.scenes[0].clips[0]
        self.assertTrue(clip.explainer_clip)
        self.assertTrue(os.path.isfile(clip.file_path))
        self.assertIn("out", os.path.normpath(clip.file_path))
        self.assertEqual(clip.motion, "static")

    def test_timeline_starts_at_zero_and_covers_audio(self):
        first = min(s.start_frame for s in self.timeline.scenes)
        last = max(s.end_frame for s in self.timeline.scenes)
        self.assertEqual(first, 0)
        self.assertEqual(last, 180)   # 6.0 s audio at 30 fps

    def test_render_produces_preview(self):
        self.assertTrue(self.render_ok, self.render_errors)
        self.assertTrue(os.path.isfile(self.render_out))
        data = _probe(self.render_out)
        streams = data.get("streams", [])
        video = next(s for s in streams if s.get("codec_type") == "video")
        from whisperradar.video import options as options_mod
        opts = options_mod.load_or_default(os.path.join(self.tmp, "imgtovideo.json"))
        self.assertEqual(int(video["width"]), int(opts.render.preview_width))
        self.assertGreater(int(video.get("nb_frames", 0)), 150)

    def test_prepared_clip_fingerprint_stable_across_replan(self):
        from whisperradar.video import plan as plan_mod
        again = plan_mod.plan_project(self.tmp)
        self.assertTrue(again.success)
        clip2 = again.timeline.scenes[0].clips[0]
        clip1 = self.timeline.scenes[0].clips[0]
        self.assertEqual(os.path.basename(clip2.file_path),
                         os.path.basename(clip1.file_path))


class LegacySafetyTests(unittest.TestCase):
    def test_video_clip_defaults_backward_compatible(self):
        from whisperradar.video import models as m
        clip = m.VideoClip(file_path="x.png", scene_id="s1")
        d = clip.to_dict()
        self.assertNotIn("explainer_clip", d)
        back = m.VideoClip.from_dict(d)
        self.assertFalse(back.explainer_clip)

    def test_video_clip_roundtrip_with_marker(self):
        from whisperradar.video import models as m
        clip = m.VideoClip(file_path="x.mp4", scene_id="s1", explainer_clip=True)
        back = m.VideoClip.from_dict(clip.to_dict())
        self.assertTrue(back.explainer_clip)

    def test_explainer_absent_gives_planner_the_floor(self):
        # no explainer.json in a fixture folder -> plan_project must NOT take
        # the explainer branch (verified via the branch condition directly)
        from whisperradar.video import explainer_planner
        self.assertFalse(explainer_planner.has_explainer(
            os.path.join(REPO, "tests", "fixtures", "video", "fees")))


class WhiteboardPipelineTests(unittest.TestCase):
    """visual_mode=whiteboard end to end: stroke scenes through plan_project
    + render_final (16.9 step 5: modes are distinct, timing shared)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="explainer_wb_")
        with open(os.path.join(cls.tmp, "subtitles.srt"), "w",
                  encoding="utf-8") as fh:
            fh.write(SRT)
        doc = json.loads(json.dumps(DOC))
        doc["schema_version"] = 2
        doc["style"] = "whiteboard"
        doc["scenes"] = [{
            "id": "wb", "shot_id": "S01_01", "cue_range": [1, 3],
            "objects": [
                {"id": "house", "type": "stroke_asset",
                 "asset": "builtin:house", "box": [0.1, 0.2, 0.3, 0.5]},
                {"id": "hw", "type": "handwriting", "text": "Paid 150",
                 "box": [0.5, 0.3, 0.4, 0.2]},
            ],
            "events": [
                {"target": "house", "action": "draw_strokes",
                 "at": {"cue": 1, "edge": "start"}, "duration_ms": 1500},
                {"target": "hw", "action": "draw_strokes",
                 "at": {"cue": 2, "edge": "start"}, "duration_ms": 1400},
                {"target": "house", "action": "erase",
                 "at": {"cue": 3, "edge": "start"}, "duration_ms": 500},
            ]}]
        with open(os.path.join(cls.tmp, "explainer.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(doc, fh)
        _ffmpeg_audio(os.path.join(cls.tmp, "audio.mp3"))
        from whisperradar.video import plan as plan_mod, render_project
        with open(os.path.join(cls.tmp, "imgtovideo.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"visual_mode": "whiteboard"}, fh)
        result = plan_mod.plan_project(cls.tmp)
        cls.plan_result = result
        assert result.success, [i.message for i in result.issues
                                if i.severity == "Error"]
        ok, out, errors = render_project.render_final(cls.tmp, preview=True)
        cls.render_ok, cls.render_out, cls.render_errors = ok, out, errors

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_plan_uses_whiteboard(self):
        self.assertTrue(self.plan_result.success)
        note = [i for i in self.plan_result.issues
                if i.code == "EXPLAINER_MODE"]
        self.assertTrue(note and "whiteboard" in note[0].message)

    def test_whiteboard_renders(self):
        self.assertTrue(self.render_ok, self.render_errors)
        data = _probe(self.render_out)
        video = next(s for s in data.get("streams", [])
                     if s.get("codec_type") == "video")
        self.assertGreater(int(video.get("nb_frames", 0)), 150)

    def test_mode_without_sidecar_fails_clearly(self):
        from whisperradar.video import plan as plan_mod, options as opt
        other = tempfile.mkdtemp(prefix="explainer_wb_nosidecar_")
        try:
            with open(os.path.join(other, "subtitles.srt"), "w",
                      encoding="utf-8") as fh:
                fh.write(SRT)
            _ffmpeg_audio(os.path.join(other, "audio.mp3"))
            opts_path = os.path.join(other, "imgtovideo.json")
            o = opt.load_or_default(opts_path)
            o.visual_mode = "whiteboard"
            opt.save(o, opts_path)
            result = plan_mod.plan_project(other)
            self.assertFalse(result.success)
            codes = [i.code for i in result.issues]
            self.assertIn("EXPLAINER_MODE_WITHOUT_SIDECAR", codes)
        finally:
            shutil.rmtree(other, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
