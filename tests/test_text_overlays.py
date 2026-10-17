"""Text overlays: the shotlist names text + position, the renderer draws it in the
channel's font. Parsing, planning, PNG drawing and the ffmpeg overlay step."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from whisperradar.video import models as m, options as opt, shotlist, text_overlays  # noqa: E402
from whisperradar.video import render_plan  # noqa: E402


def _issues(shot):
    issues = []
    return shotlist._parse_overlays(shot, 1, issues), issues


class ParseTests(unittest.TestCase):
    def test_no_overlays(self):
        self.assertEqual(_issues({}), (None, []))

    def test_valid_item_with_defaults(self):
        items, issues = _issues({"overlays": [{"text": " $1,000 -> $1.8M ", "style": "Stat",
                                                "position": "top-center"}]})
        self.assertEqual(issues, [])
        self.assertEqual(items, [{"text": "$1,000 -> $1.8M", "style": "stat",
                                  "position": "top-center", "anim": "fade",
                                  "start": None, "end": None}])

    def test_single_object_is_a_list(self):
        items, _ = _issues({"overlays": {"text": "Hi"}})
        self.assertEqual(items[0]["position"], "bottom-center")

    def test_bad_items_are_skipped_with_a_warning(self):
        items, issues = _issues({"overlays": [
            {"text": ""}, "oops", {"text": "x" * 500},
            {"text": "ok", "position": "nowhere", "anim": "spin", "start": -1}]})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["position"], "bottom-center")
        self.assertEqual(items[0]["anim"], "fade")
        self.assertIsNone(items[0]["start"])
        self.assertTrue(all(i.code == "SHOTLIST_OVERLAY_INVALID" for i in issues))
        self.assertGreaterEqual(len(issues), 5)

    def test_end_before_start_uses_the_whole_shot(self):
        items, issues = _issues({"overlays": [{"text": "a", "start": 2, "end": 1}]})
        self.assertEqual((items[0]["start"], items[0]["end"]), (None, None))
        self.assertEqual(len(issues), 1)


class PlanTests(unittest.TestCase):
    def test_build_overlays_places_text_in_the_shot(self):
        from whisperradar.video import manifest_planner as mp, manifest as mf
        clip = m.VideoClip(file_path="a.png", shot_id="s1", start_frame=60, duration_frames=150)
        shot = mf.VisualShot(shot_id="s1", overlays=[
            {"text": "A", "style": "title", "position": "center", "anim": "fade",
             "start": 0.5, "end": 2.0},
            {"text": "B", "style": "caption", "position": "bottom-center", "anim": "none",
             "start": None, "end": None},
            {"text": "late", "start": 99, "end": 100}])
        out = mp._build_overlays([clip], [shot], 30.0)
        self.assertEqual([(o.text, o.start_frame, o.end_frame) for o in out],
                         [("B", 60, 210), ("A", 75, 120)])   # sorted by start; "late" is outside the shot

    def test_timeline_roundtrip(self):
        tl = m.Timeline(overlays=[m.TextOverlay("x", "stat", "center", 3, 30, "fade", "p.png")])
        back = m.Timeline.from_dict(json.loads(json.dumps(tl.to_dict())))
        self.assertEqual(back.overlays[0].text, "x")
        self.assertEqual((back.overlays[0].start_frame, back.overlays[0].end_frame), (3, 30))


class DrawTests(unittest.TestCase):
    def test_png_is_full_frame_and_transparent_outside_the_text(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "t.png")
            ov = m.TextOverlay("Hello world", "stat", "top-center")
            text_overlays.render_png(ov, 1280, 720, text_overlays.find_font(opt.ProjectOptions(), tmp),
                                     6.0, out)
            im = Image.open(out)
            self.assertEqual(im.size, (1280, 720))
            self.assertEqual(im.getpixel((5, 715))[3], 0)        # far corner is transparent
            top = im.crop((0, 0, 1280, 240)).getchannel("A").getbbox()
            self.assertIsNotNone(top)                              # text sits in the top band
            self.assertIsNone(im.crop((0, 400, 1280, 720)).getchannel("A").getbbox())

    def test_positions_move_the_text(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            font = text_overlays.find_font(opt.ProjectOptions(), tmp)
            boxes = {}
            for pos in ("top-left", "bottom-right"):
                out = os.path.join(tmp, pos + ".png")
                text_overlays.render_png(m.TextOverlay("Hi", "caption", pos), 1280, 720, font, 6.0, out)
                boxes[pos] = Image.open(out).getchannel("A").getbbox()
            self.assertLess(boxes["top-left"][0], 200)
            self.assertGreater(boxes["bottom-right"][2], 1000)
            self.assertLess(boxes["top-left"][1], 200)
            self.assertGreater(boxes["bottom-right"][3], 500)

    def test_resolve_drops_unknown_style_to_default_with_warning_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            tl = m.Timeline(resolution=m.Resolution(640, 360), overlays=[
                m.TextOverlay("a", "weird", "center", 0, 30)])
            issues = text_overlays.resolve(tl, tmp, opt.ProjectOptions())
            self.assertIn("TEXT_STYLE_UNKNOWN", [i.code for i in issues])
            self.assertTrue(os.path.isfile(tl.overlays[0].png_path))
            first = os.path.getmtime(tl.overlays[0].png_path)
            tl2 = m.Timeline(resolution=m.Resolution(640, 360), overlays=[
                m.TextOverlay("a", "weird", "center", 0, 30)])
            text_overlays.resolve(tl2, tmp, opt.ProjectOptions())
            self.assertEqual(os.path.getmtime(tl2.overlays[0].png_path), first)


class MuxTests(unittest.TestCase):
    def test_without_overlays_the_video_is_copied(self):
        args = render_plan.build_mux_arguments("r.mp4", "a.wav", "o.mp4")
        self.assertIn("copy", args)
        self.assertNotIn("-filter_complex", args)

    def test_overlays_reencode_with_overlay_filters(self):
        ov = m.TextOverlay("x", "stat", "center", 30, 120, "slide-up", "t.png")
        args = render_plan.build_mux_arguments(
            "r.mp4", "a.wav", "o.mp4", None, 30, [ov], opt.ProjectOptions().render,
            opt.ProjectOptions().text, (960, 540))
        graph = args[args.index("-filter_complex") + 1]
        self.assertIn("overlay=x=0", graph)
        self.assertIn("between(t,1,4)", graph)
        self.assertIn("fade=t=in", graph)
        self.assertIn("scale=960:540", graph)
        self.assertNotIn("copy", args[args.index("-map"):])
        self.assertIn("-itsoffset", args)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_real_ffmpeg_draws_the_text_only_in_its_window(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            rough = os.path.join(tmp, "rough.mp4")
            audio = os.path.join(tmp, "a.wav")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "color=c=blue:s=640x360:d=4:r=30", "-pix_fmt", "yuv420p", rough], check=True)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "anullsrc=r=48000:cl=stereo", "-t", "4", audio], check=True)
            tl = m.Timeline(resolution=m.Resolution(640, 360), overlays=[
                m.TextOverlay("HELLO", "title", "center", 30, 90, "fade")])
            options = opt.ProjectOptions()
            options.render.encoder = "libx264"
            text_overlays.resolve(tl, tmp, options)
            out = os.path.join(tmp, "out.mp4")
            args = render_plan.build_mux_arguments(rough, audio, out, None, 30, tl.overlays,
                                                   options.render, options.text, (640, 360))
            proc = subprocess.run(["ffmpeg"] + args, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
            def frame(t):
                png = os.path.join(tmp, f"f{t}.png")
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", out,
                                "-frames:v", "1", png], check=True)
                return Image.open(png).convert("RGB")
            blue = (0, 0, 255)
            def non_blue(im):
                px = im.load()
                return sum(1 for x in range(0, 640, 4) for y in range(0, 360, 4)
                           if max(abs(a - b) for a, b in zip(px[x, y], blue)) > 60)
            self.assertEqual(non_blue(frame(0.5)), 0)      # before the text
            self.assertGreater(non_blue(frame(2.0)), 20)   # while it is shown
            self.assertEqual(non_blue(frame(3.6)), 0)      # after it


class BriefAndFaultTests(unittest.TestCase):
    def test_brief_gets_the_overlay_section_only_for_channels_that_use_it(self):
        from whisperradar import briefs
        tpl = briefs.read_template(None)
        off = briefs.render_brief(tpl.strip(), briefs.resolve_profile(None))
        on = briefs.render_brief(tpl.strip(), briefs.resolve_profile(None, text_overlays=1))
        self.assertNotIn("TEXT DRAWN BY THE RENDERER", off)
        self.assertIn("TEXT DRAWN BY THE RENDERER", on)
        self.assertLess(on.index("SECTION 7C"), on.index("## SECTION 8 "))
        self.assertIn('"overlays"', on)

    def test_structural_faults_check_overlays(self):
        from whisperradar import studio
        shot = lambda ov: {"shots": [{"cues": "1-1", "asset": "S01_01_SCN_ZI.png",
                                     "scene": "S01", "motion": "ZI", "overlays": ov}],
                           "images": [{"file": "S01_01_SCN_ZI.png", "prompt": "p"}]}
        good = [{"text": "$1.8M", "style": "stat", "position": "top-center", "start": 0.2, "end": 2}]
        self.assertEqual([f for f in studio.shotlist_structural_faults(shot(good), 1)
                          if "overlay" in f], [])
        bad = [{"text": "", "position": "up"},
               {"text": "x", "style": "huge", "anim": "spin", "start": 3, "end": 1},
               {"text": "y"}]
        found = [f for f in studio.shotlist_structural_faults(shot(bad), 1) if "overlay" in f]
        self.assertGreaterEqual(len(found), 6)
        same = [{"text": "a", "position": "center"}, {"text": "b", "position": "center"}]
        self.assertTrue([f for f in studio.shotlist_structural_faults(shot(same), 1)
                         if "same time" in f])

    def test_pacing_flags_an_overlay_outside_its_shot(self):
        from whisperradar import studio
        cues = [{"index": 1, "start": "00:00:00,000", "end": "00:00:04,000", "text": "a"}]
        data = {"shots": [{"cues": "1-1", "asset": "a.png", "scene": "S01", "motion": "ZI",
                           "overlays": [{"text": "x", "start": 1, "end": 9}]}],
                "images": [{"file": "a.png", "prompt": "p"}]}
        faults, _ = studio.shotlist_pacing(data, cues, 12.0)
        self.assertTrue([f for f in faults if "overlays[1]" in f])

    def test_channel_setting_reaches_the_effective_settings(self):
        from whisperradar import db
        tmp = tempfile.TemporaryDirectory()
        try:
            conn = db.connect(Path(tmp.name) / "wr.db")
            db.init_db(conn)
            cid = db.create_own_channel(conn, "Ch", style="s", bible="b")
            db.update_own_channel(conn, cid, brief_text_overlays=1)
            row = conn.execute("select brief_text_overlays from own_channels where id=?", (cid,)).fetchone()
            self.assertEqual(row[0], 1)
            conn.close()
        finally:
            try:
                tmp.cleanup()
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
