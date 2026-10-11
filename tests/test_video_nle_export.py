"""Premiere (FCP7 XML) and CapCut draft exporters of the Python video engine."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402,F401

from whisperradar.video import capcut_export, premiere_export, timeline_json  # noqa: E402
from whisperradar.video import models as m  # noqa: E402
from whisperradar.video import options as options_mod  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "video" / "premiere_more_to_live"


def _lf(text):
    return text.replace("\r\n", "\n")


class PremiereGolden(unittest.TestCase):
    """The XML ImgToVideo's C# exporter wrote for a real production, rebuilt from its
    timeline.json: the two must match exactly."""

    def test_matches_the_csharp_export(self):
        tl = timeline_json.load(str(FIX / "timeline.json"))
        dims = json.loads((FIX / "dims.json").read_text())
        images = [m.ImageInfo(p, m.ParsedImageName("x", 0, 0), w, h) for p, (w, h) in dims.items()]
        opts = options_mod.load_or_default(str(FIX / "imgtovideo.json"))
        got = premiere_export.export(tl, images, True, opts)
        want = (FIX / "premiere.xml").read_bytes().decode("utf-8")
        self.assertEqual(_lf(got), _lf(want))
        self.assertIn("\r\n", got)          # CRLF like the C# writer

    def test_path_url(self):
        self.assertEqual(
            premiere_export.to_path_url(r"E:\YOUTUBE\My Project (2)\out\sfx\pop.wav"),
            "file://localhost/E:/YOUTUBE/My%20Project%20%282%29/out/sfx/pop.wav")


def _sample():
    p1, p2 = r"D:\Repos\My Project\images\S01_01_SCN_ZI.png", r"D:\Repos\My Project\images\S01_02_SCN_ST.png"
    images = [m.ImageInfo(p1, m.ParsedImageName("S01_01_SCN_ZI", 1, 1), 2304, 1296),
              m.ImageInfo(p2, m.ParsedImageName("S01_02_SCN_ST", 1, 2), 2304, 1296)]
    tl = m.Timeline(
        project_name="Spike Check", resolution=m.Resolution(1920, 1080), fps=30,
        audio=m.AudioTrack(r"D:\Repos\My Project\audio\narration.mp3", 180),
        scenes=[m.Scene("S01", 0, 180, [
            m.VideoClip(file_path=p1, scene_id="S01", start_frame=0, duration_frames=90,
                        motion=m.ZOOM_IN, start_viewport=m.Rect(0, 0, 2304, 1296),
                        end_viewport=m.Rect(96, 54, 2112, 1188)),
            m.VideoClip(file_path=p2, scene_id="S01", start_frame=90, duration_frames=90,
                        motion=m.STATIC, start_viewport=m.Rect(0, 0, 2304, 1296),
                        end_viewport=m.Rect(0, 0, 2304, 1296))])])
    return tl, images


class PremiereSounds(unittest.TestCase):
    def test_overlapping_sounds_get_their_own_tracks(self):
        tl, images = _sample()
        tl.sounds = [m.SoundEffect("pop", r"D:\x\pop.wav", 10, 20),
                     m.SoundEffect("ding", r"D:\x\ding.wav", 15, 20),
                     m.SoundEffect("tick", r"D:\x\tick.wav", 40, 5)]
        xml = premiere_export.export(tl, images)
        self.assertEqual(xml.count('<clipitem id="clipitem-sfx-'), 3)
        self.assertIn('id="clipitem-sfx-1"', xml)
        # 1 narration track + 2 sound tracks (pop/tick share, ding overlaps pop)
        audio = xml.split("</video>\r\n      <audio>")[-1]
        self.assertEqual(audio.count("<track>"), 3)


class CapCut(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tl, images = _sample()
        folder = os.path.join(self.tmp.name, "draft one")
        capcut_export.export(tl, images, folder)
        self.folder = folder
        self.content = json.loads(Path(folder, "draft_content.json").read_text(encoding="utf-8"))
        self.meta = json.loads(Path(folder, "draft_meta_info.json").read_text(encoding="utf-8"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_bomless_files(self):
        data = Path(self.folder, "draft_content.json").read_bytes()
        self.assertNotEqual(data[0], 0xEF)

    def test_durations_in_microseconds(self):
        c = self.content
        self.assertEqual(c["duration"], 6000000)
        self.assertEqual(c["fps"], 30)
        seg = c["tracks"][0]["segments"]
        self.assertEqual([s["target_timerange"] for s in seg],
                         [{"start": 0, "duration": 3000000}, {"start": 3000000, "duration": 3000000}])

    def test_zoom_gets_scale_keyframes_static_does_not(self):
        seg = self.content["tracks"][0]["segments"]
        kfs = seg[0]["common_keyframes"]
        self.assertEqual([k["property_type"] for k in kfs], ["KFTypeScaleX", "KFTypeScaleY"])
        vals = [k["values"][0] for k in kfs[0]["keyframe_list"]]
        self.assertAlmostEqual(vals[0], 1.0, 6)
        self.assertAlmostEqual(vals[1], 2304 / 2112, 6)
        self.assertEqual(kfs[0]["keyframe_list"][1]["time_offset"], 3000000)
        self.assertEqual(seg[1]["common_keyframes"], [])

    def test_six_companions_and_materials(self):
        mats = self.content["materials"]
        for kind in ("canvases", "speeds", "sound_channel_mappings", "material_colors",
                     "placeholder_infos", "vocal_separations"):
            self.assertGreaterEqual(len(mats[kind]), 2, kind)
        for s in self.content["tracks"][0]["segments"]:
            self.assertEqual(len(s["extra_material_refs"]), 6)
        self.assertEqual(len(mats["videos"]), 2)
        self.assertTrue(all(v["duration"] == 10800000000 for v in mats["videos"]))
        self.assertIn("narration.mp3", mats["audios"][0]["path"])
        self.assertEqual(self.content["tracks"][1]["segments"][0]["target_timerange"]["duration"], 6000000)

    def test_meta(self):
        self.assertEqual(self.meta["tm_duration"], 6000000)
        self.assertEqual(self.meta["draft_new_version"], "185.0.0")
        self.assertEqual(len(self.meta["draft_materials"][0]["value"]), 2)
        self.assertEqual(self.meta["draft_name"], "draft one")

    def test_json_style_matches_system_text_json(self):
        self.assertEqual(capcut_export.dumps({"a": 1.0, "b": 0.5, "c": "é<'"}),
                         '{"a":1,"b":0.5,"c":"\\u00E9\\u003C\\u0027"}')

    def test_sounds_go_to_extra_tracks(self):
        tl, images = _sample()
        tl.sounds = [m.SoundEffect("pop", r"D:\x\pop.wav", 10, 20),
                     m.SoundEffect("ding", r"D:\x\ding.wav", 15, 20)]
        text = capcut_export.build_draft_content(tl, images, "d")
        d = json.loads(text)
        self.assertEqual([t["type"] for t in d["tracks"]], ["video", "audio", "audio", "audio"])
        self.assertEqual(len(d["materials"]["audios"]), 3)


if __name__ == "__main__":
    unittest.main()
