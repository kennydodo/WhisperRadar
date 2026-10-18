"""Filtergraph backend semantics: declared events drive text-only scenes."""
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

from whisperradar.video import parsing  # noqa: E402
from whisperradar.video.explainer import compose  # noqa: E402
from whisperradar.video.explainer import prepare as prep  # noqa: E402
from whisperradar.video.explainer import timing as tg  # noqa: E402
from whisperradar.video.explainer import validate as va  # noqa: E402
from whisperradar.video.explainer.layout import layout_scene  # noqa: E402
from whisperradar.video.explainer.plates import render_scene_plates  # noqa: E402

SRT = "1\n00:00:00,000 --> 00:00:02,000\none\n"

DOC = {
    "schema_version": 1,
    "theme": "finance_dark",
    "canvas": {"width": 320, "height": 180},
    "scenes": [{
        "id": "s1", "shot_id": "S01_01", "cue_range": [1, 1],
        "objects": [
            {"id": "lab", "type": "label", "text": "TYPE",
             "box": [0.1, 0.2, 0.6, 0.2]},
        ],
        "events": [
            {"target": "lab", "action": "type_on",
             "at": {"cue": 1, "edge": "start"}, "duration_ms": 900},
            {"target": "lab", "action": "fade_out",
             "at": {"cue": 1, "edge": "end"}, "offset_ms": -300,
             "duration_ms": 300},
        ],
    }],
}


def _run(ffmpeg, args):
    return subprocess.run(
        [ffmpeg, *args], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class FiltergraphSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="explainer_fg_")
        with open(os.path.join(cls.tmp, "subtitles.srt"), "w",
                  encoding="utf-8") as fh:
            fh.write(SRT)
        with open(os.path.join(cls.tmp, "explainer.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(DOC, fh)
        from whisperradar.video import tool_locator
        cls.ffmpeg = tool_locator.resolve("ffmpeg", "ffmpeg")
        cls.doc = va.load(os.path.join(cls.tmp, "explainer.json"),
                          project_folder=cls.tmp)
        cls.compiled = tg.compile_doc(cls.doc, parsing.parse_srt(SRT), 30.0,
                                      total_seconds=2.0)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_type_on_window_and_declared_fadeout(self):
        cs = self.compiled[0]
        laid = layout_scene(cs, self.doc.theme, self.doc.canvas)
        plates_dir = os.path.join(self.tmp, "plates")
        plate_map = render_scene_plates(laid, self.doc.canvas, plates_dir,
                                        self.doc.theme, self.tmp)
        inputs, graph = compose.build_filtergraph_scene(
            cs, laid, plate_map, self.doc.theme, self.doc.canvas, 30.0,
            self.doc.theme.background)
        self.assertIsNotNone(graph)
        # type_on at 0..0.9s with 60 fps: plate windows are successive slices
        self.assertIn("between(t,0,", graph)
        # declared fade_out starts at (2.0 - 0.3) = 1.7 s on the final plate
        self.assertIn("fade=t=out:st=1.7", graph)
        # no automatic scene-end fade: the only fade=t=out comes from the event
        self.assertEqual(graph.count("fade=t=out"), 1)

    def test_end_to_end_frame_semantics(self):
        """The final frame must be EMPTY (declared fade_out completed), and a
        mid-type-on frame must show a partial label. Output seeking (-ss after
        -i) preserves timestamps; input seeking would shift them and break the
        enable windows."""
        clip = prep.render_scene_clip(self.doc, self.compiled[0], self.ffmpeg,
                                      self.tmp, 30.0)
        last = os.path.join(self.tmp, "last.png")
        _run(self.ffmpeg, ["-i", clip, "-ss", "1.95", "-frames:v", "1", last])
        from PIL import Image
        img = Image.open(last).convert("L")
        # after the fade_out the label area is background (dark)
        box = img.crop((int(320 * 0.1), int(180 * 0.2),
                        int(320 * 0.7), int(180 * 0.4)))
        self.assertLess(max(box.getdata()), 60,
                        "label should be gone after the declared fade_out")
        mid = os.path.join(self.tmp, "mid.png")
        _run(self.ffmpeg, ["-i", clip, "-ss", "0.45", "-frames:v", "1", mid])
        img2 = Image.open(mid).convert("L")
        box2 = img2.crop((int(320 * 0.1), int(180 * 0.2),
                          int(320 * 0.7), int(180 * 0.4)))
        self.assertGreater(max(box2.getdata()), 140,
                           "label should be visible mid type-on")


if __name__ == "__main__":
    unittest.main()
