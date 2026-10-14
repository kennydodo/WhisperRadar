"""The NLE exports must time video-backed shots like the render: an 8s clip
covers up to 10s of shot at 0.8x, and longer shots freeze the clip's last
frame - never black past the end of the media (S03_01, production 46)."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_merge_clip_check import _animate_on  # noqa: E402
from whisperradar import studio  # noqa: E402
from whisperradar.video import nle_export  # noqa: E402


def _make_clip_folder(pdir, shots):
    """clips/<stem>.mp4 for every animated shot; returns the freeze-free base."""
    clips = pdir / "clips"
    clips.mkdir(exist_ok=True)
    for stem, seconds in shots:
        # a valid tiny mp4 is not needed for export math; >10KB placeholder
        (clips / f"{stem}.mp4").write_bytes(b"x" * 20_000)
    return clips


class NleVideoTimingTests(unittest.TestCase):
    def setUp(self):
        from tests.test_external_prompts import Base
        self._base = Base("setUp")
        self._base.setUp()
        self.cfg, self.pid, self.pdir = self._base.cfg, self._base.pid, self._base.pdir
        # two shots in the 60s window: one 9.17s (speed stretch), one 12s (hold)
        plan = {"shots": [
            {"asset": "a_ST.png", "cues": "1-3", "ai_motion": "pan"},
            {"asset": "b_ST.png", "cues": "4-6", "ai_motion": "pan"},
            {"asset": "c_ST.png", "cues": "7-9", "ai_motion": "pan"}],
            "images": [
                {"file": "a_ST.png", "prompt": "one"},
                {"file": "b_ST.png", "prompt": "two"},
                {"file": "c_ST.png", "prompt": "three"}]}
        (self.pdir / "shotlist.json").write_text(json.dumps(plan), "utf-8")
        img = self.pdir / "images"
        img.mkdir(exist_ok=True)
        for n in ("a_ST.png", "b_ST.png", "c_ST.png"):
            (img / n).write_bytes(b"x" * 20_000)
        for n in ("a", "b", "c"):
            (self.pdir / "clips").mkdir(exist_ok=True)
            (self.pdir / "clips" / f"{n}_ST.mp4").write_bytes(b"x" * 20_000)
        (self.pdir / "subtitles.srt").write_text(
            "1\n00:00:00,000 --> 00:00:09,000\none\n\n"
            "2\n00:00:09,000 --> 00:00:18,000\ntwo\n\n"
            "3\n00:00:18,000 --> 00:00:27,000\nthree\n", "utf-8")
        (self.pdir / "audio.mp3").write_bytes(b"x")

    def tearDown(self):
        self._base.tearDown()

    def _export(self):
        freezes = {}
        (self.pdir / "out" / "render").mkdir(parents=True, exist_ok=True)
        for stem in ("a_ST", "b_ST", "c_ST"):
            freeze = self.pdir / "out" / "render" / f"freeze_{stem}.png"
            freeze.write_bytes(b"x")
            freezes[str(self.pdir / "clips" / f"{stem}.mp4")] = str(freeze)
        with mock.patch.object(nle_export, "_prepare",
                               return_value=(str(self.pdir), str(self.pdir / "out"),
                                             self._timeline(), [], None, freezes)):
            path = nle_export.export_premiere(str(self.pdir))
        return ElementTree.parse(path).getroot()

    def _timeline(self):
        from whisperradar.video import models as m
        fps = 30
        res = m.Resolution(1376, 768)
        clips = []
        for i, (stem, frames) in enumerate([("a_ST", 275), ("b_ST", 130), ("c_ST", 360)]):
            sv = m.Rect(0, 0, 1376, 768)
            clips.append(m.VideoClip(
                file_path=str(self.pdir / "images" / f"{stem}.png"),
                scene_id="sc1", shot_id=f"shot-{i + 1:03d}",
                start_frame=sum(c.duration_frames for c in clips),
                duration_frames=frames, motion=m.ZOOM_IN,
                start_viewport=sv, end_viewport=sv, easing="linear",
                video_path=str(self.pdir / "clips" / f"{stem}.mp4")))
        scene = m.Scene(id="sc1", clips=clips)
        return m.Timeline(project_name="t", resolution=res, fps=fps,
                          audio=m.AudioTrack(file_path=str(self.pdir / "audio.mp3"),
                                             duration_frames=275 + 130 + 360),
                          scenes=[scene])

    def test_stretch_shot_plays_slower_not_black(self):
        """a_ST: 275-frame (9.17s) shot, no transitions in this synthetic
        timeline -> one 275-frame item at speed 87 (240 source frames of
        media slowed to cover the whole span) - no black, no freeze."""
        root = self._export()
        items = {i.findtext("name"): i for i in root.iter("clipitem")}
        item = items["a_ST.mp4"]
        speed = next(p for p in item.iter("parameter")
                     if p.findtext("parameterid") == "speed")
        self.assertEqual(speed.findtext("value"), "87")
        self.assertEqual(int(item.findtext("in")), 0)
        self.assertEqual(int(item.findtext("out")), 240)
        self.assertEqual(int(item.findtext("end")) - int(item.findtext("start")), 275)
        # no freeze item for this one
        self.assertNotIn("freeze_a_ST.png", items)

    def test_hold_shot_freezes_the_last_frame(self):
        """c_ST: 360-frame (12s) item span, 8s media -> 300 timeline frames at
        80% (10s of playback) + a freeze item covering the 60-frame hold."""
        root = self._export()
        items = {i.findtext("name"): i for i in root.iter("clipitem")}
        video = items["c_ST.mp4"]
        self.assertEqual(int(video.findtext("end")) - int(video.findtext("start")), 300)
        self.assertEqual(int(video.findtext("out")), 240)
        freeze = items["freeze_c_ST.png"]
        f_start = int(freeze.findtext("start"))
        f_end = int(freeze.findtext("end"))
        self.assertEqual(f_end - f_start, 60)
        self.assertEqual(f_start, int(video.findtext("end")))


if __name__ == "__main__":
    unittest.main()
