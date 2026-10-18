"""Determinism and prepared-clip tests (real ffmpeg, no paid APIs)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from whisperradar.video import parsing  # noqa: E402
from whisperradar.video.explainer import prepare as prep  # noqa: E402
from whisperradar.video.explainer import validate as va  # noqa: E402
from whisperradar.video.explainer import timing as tg  # noqa: E402
from whisperradar.video.explainer import evaluator as ev  # noqa: E402
from whisperradar.video.explainer.layout import layout_scene  # noqa: E402

SRT = """1
00:00:00,000 --> 00:00:02,000
one

2
00:00:02,000 --> 00:00:04,000
two
"""

DOC = {
    "schema_version": 1,
    "theme": "finance_dark",
    "canvas": {"width": 320, "height": 180},
    "scenes": [{
        "id": "s1", "shot_id": "S01_01", "cue_range": [1, 2],
        "objects": [
            {"id": "c1", "type": "counter", "from": 0, "to": 150,
             "format": "currency_usd", "box": [0.1, 0.2, 0.4, 0.15],
             "style": "stat"},
            {"id": "p1", "type": "path",
             "points": [[0.1, 0.6], [0.5, 0.6], [0.5, 0.85]], "arrow": True,
             "box": [0, 0, 1, 1], "style": "accent"},
        ],
        "events": [
            {"target": "c1", "action": "count",
             "at": {"cue": 1, "edge": "start"}, "duration_ms": 600},
            {"target": "p1", "action": "draw",
             "at": {"cue": 2, "edge": "start"}, "duration_ms": 500},
        ],
    }],
}


def _make_project(tmp):
    with open(os.path.join(tmp, "subtitles.srt"), "w", encoding="utf-8") as fh:
        fh.write(SRT)
    with open(os.path.join(tmp, "explainer.json"), "w", encoding="utf-8") as fh:
        json.dump(DOC, fh)
    return tmp


def _probe_frames(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=nb_frames", "-of", "csv=p=0", path],
        capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return int(out.stdout.strip() or "0")


class PreparedClipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="explainer_test_")
        _make_project(cls.tmp)
        from whisperradar.video import tool_locator
        cls.ffmpeg = tool_locator.resolve("ffmpeg", "ffmpeg")
        doc = va.load(os.path.join(cls.tmp, "explainer.json"),
                      project_folder=cls.tmp)
        cues = parsing.parse_srt(SRT)
        cls.compiled = tg.compile_doc(doc, cues, 30.0, total_seconds=4.0)
        cls.doc = doc
        cls.clip = prep.render_scene_clip(doc, cls.compiled[0], cls.ffmpeg,
                                          cls.tmp, 30.0)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_clip_exists_and_frame_exact(self):
        self.assertTrue(os.path.isfile(self.clip))
        # scene spans cue 1 start (0.0s) to audio end (4.0s) = 120 frames
        self.assertEqual(_probe_frames(self.clip), 120)

    def test_clip_has_no_audio(self):
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", self.clip],
            capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(out.stdout.strip(), "")

    def test_cache_reuse_same_path(self):
        again = prep.render_scene_clip(self.doc, self.compiled[0], self.ffmpeg,
                                       self.tmp, 30.0)
        self.assertEqual(again, self.clip)

    def test_fingerprint_changes_when_srt_timing_changes(self):
        # same scene JSON, different compiled timing (SRT shifted) -> new key
        shifted = SRT.replace("00:00:00,000 --> 00:00:02,000",
                              "00:00:01,000 --> 00:00:02,000")
        compiled2 = tg.compile_doc(self.doc, parsing.parse_srt(shifted), 30.0,
                                   total_seconds=4.0)
        fp1 = prep.scene_fingerprint(self.compiled[0], self.doc, 30.0, 320, 180,
                                     self.tmp)
        fp2 = prep.scene_fingerprint(compiled2[0], self.doc, 30.0, 320, 180,
                                     self.tmp)
        self.assertNotEqual(fp1, fp2)

    def test_fingerprint_changes_when_theme_changes(self):
        import copy
        doc2 = copy.deepcopy(self.doc)
        doc2.theme.background = "#000000"
        fp1 = prep.scene_fingerprint(self.compiled[0], self.doc, 30.0, 320, 180,
                                     self.tmp)
        fp2 = prep.scene_fingerprint(self.compiled[0], doc2, 30.0, 320, 180,
                                     self.tmp)
        self.assertNotEqual(fp1, fp2)

    def test_verify_checks_frame_count(self):
        # the real clip passes verification
        prep._verify(self.clip, self.compiled[0], 30.0, 320, 180)
        # a compiled scene with a wrong expected length must fail
        from whisperradar.video.explainer.timing import CompiledScene
        wrong = CompiledScene(self.compiled[0].spec, 0, 60, 30.0,
                              list(self.compiled[0].events))
        with self.assertRaises(RuntimeError) as ctx:
            prep._verify(self.clip, wrong, 30.0, 320, 180)
        self.assertIn("wrong-length", str(ctx.exception))

    def test_cache_invalidates_on_content_change(self):
        data = json.loads(json.dumps(DOC))
        data["scenes"][0]["objects"][0]["to"] = 999
        path2 = os.path.join(self.tmp, "explainer2.json")
        with open(path2, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        doc2 = va.load(path2, project_folder=self.tmp)
        compiled2 = tg.compile_doc(doc2, parsing.parse_srt(SRT), 30.0,
                                   total_seconds=4.0)
        clip2 = prep.render_scene_clip(doc2, compiled2[0], self.ffmpeg,
                                       self.tmp, 30.0)
        self.assertNotEqual(os.path.basename(clip2),
                            os.path.basename(self.clip))

    def test_deterministic_frame_states(self):
        laid = layout_scene(self.compiled[0], self.doc.theme, self.doc.canvas)
        for frame in (0, 30, 60, 119):
            a = ev.evaluate(self.compiled[0], laid, frame)
            b = ev.evaluate(self.compiled[0], laid, frame)
            for oid in a:
                self.assertEqual(a[oid].value, b[oid].value)
                self.assertEqual(a[oid].progress, b[oid].progress)
                self.assertEqual(a[oid].alpha, b[oid].alpha)
                self.assertEqual(a[oid].visible, b[oid].visible)

    def test_arbitrary_frame_render_needs_no_replay(self):
        # evaluator is a pure function of frame: no earlier-frame state needed
        laid = layout_scene(self.compiled[0], self.doc.theme, self.doc.canvas)
        mid = ev.evaluate(self.compiled[0], laid, 90)
        counter = [o for o in self.compiled[0].spec.objects
                   if o.id == "c1"][0]
        st = mid["c1"]
        self.assertGreater(st.value, 0)
        self.assertLessEqual(st.value, counter.to_value)


class ImageObjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="explainer_img_")
        from PIL import Image as PILImage
        # a 64x48 red square asset
        img = PILImage.new("RGB", (64, 48), (220, 40, 40))
        img.save(os.path.join(cls.tmp, "cutout.png"))
        with open(os.path.join(cls.tmp, "subtitles.srt"), "w",
                  encoding="utf-8") as fh:
            fh.write(SRT)
        doc = json.loads(json.dumps(DOC))
        doc["scenes"][0]["objects"].append(
            {"id": "pic", "type": "image", "asset": "cutout.png",
             "box": [0.6, 0.5, 0.3, 0.4]})
        doc["scenes"][0]["events"].append(
            {"target": "pic", "action": "fade_in",
             "at": {"cue": 1, "edge": "start"}, "duration_ms": 300})
        with open(os.path.join(cls.tmp, "explainer.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(doc, fh)
        from whisperradar.video import tool_locator
        cls.ffmpeg = tool_locator.resolve("ffmpeg", "ffmpeg")
        cls.doc = va.load(os.path.join(cls.tmp, "explainer.json"),
                          project_folder=cls.tmp)
        cls.compiled = tg.compile_doc(cls.doc, parsing.parse_srt(SRT), 30.0,
                                      total_seconds=4.0)
        cls.clip = prep.render_scene_clip(cls.doc, cls.compiled[0], cls.ffmpeg,
                                          cls.tmp, 30.0)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_image_asset_renders_into_the_clip(self):
        self.assertTrue(os.path.isfile(self.clip))
        # read the final frame: the red cutout must be present
        frame_png = os.path.join(self.tmp, "frame.png")
        subprocess.run(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-i", self.clip, "-ss", "3.9", "-frames:v", "1", frame_png],
            check=True, capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        from PIL import Image as PILImage
        img = PILImage.open(frame_png).convert("RGB")
        w, h = img.size
        # probe inside the image box (0.6..0.9 x, 0.5..0.9 y)
        px = img.getpixel((int(w * 0.75), int(h * 0.7)))
        self.assertGreater(px[0], 150)   # red channel dominates
        self.assertLess(px[1], 90)


if __name__ == "__main__":
    unittest.main()
