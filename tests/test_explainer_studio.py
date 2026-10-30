"""The explainer in the Studio flow: stages skipped, merge renders the video."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from whisperradar import autorun
from whisperradar.video.explainer import render


SRT = """1
00:00:00,000 --> 00:00:03,000
Hello there, this is the first idea.

2
00:00:03,000 --> 00:00:06,000
And here is the second idea.
"""
PLAN = {"theme": "chalkboard", "scenes": [
    {"id": "a", "start": "hello there", "layout": "row", "title": "Ideas",
     "items": [{"label": "First", "icon": "box", "at": "this is the first"},
               {"label": "Second", "icon": "zap", "at": "and here is the second"}]}]}


class ExplainerStudioTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        Path(self.d, "subtitles.srt").write_text(SRT, encoding="utf-8")

    def test_is_explainer_by_plan_or_doc(self):
        self.assertFalse(autorun.is_explainer(self.d))
        Path(self.d, "plan.json").write_text("{}", encoding="utf-8")
        self.assertTrue(autorun.is_explainer(self.d))

    def test_needs_build_and_build(self):
        self.assertFalse(render.needs_build(self.d))
        Path(self.d, "plan.json").write_text(json.dumps(PLAN), encoding="utf-8")
        self.assertTrue(render.needs_build(self.d))
        n = render.build_from_plan(self.d, log=lambda m: None)
        self.assertEqual(n, 1)
        self.assertTrue(os.path.isfile(os.path.join(self.d, "explainer.json")))
        self.assertFalse(render.needs_build(self.d))

    def test_bad_plan_is_a_readable_error(self):
        bad = json.loads(json.dumps(PLAN))
        bad["scenes"][0]["start"] = "words that are never said"
        Path(self.d, "plan.json").write_text(json.dumps(bad), encoding="utf-8")
        from whisperradar.video.explainer.validate import ExplainerError
        with self.assertRaises(ExplainerError) as cm:
            render.build_from_plan(self.d, log=lambda m: None)
        self.assertIn("not found in the subtitles", cm.exception.errors[0])

    def test_render_video_runs_scenes_and_mux(self):
        Path(self.d, "plan.json").write_text(json.dumps(PLAN), encoding="utf-8")
        calls = []
        with mock.patch.object(render.prep, "render_scene_clip",
                               side_effect=lambda doc, cs, ff, f, fps, **k: self._clip(cs)), \
             mock.patch.object(render, "_run", side_effect=lambda a, w: (calls.append(w), Path(a[-1]).write_bytes(b"x"))), \
             mock.patch("whisperradar.video.tool_locator.resolve", return_value="ffmpeg"), \
             mock.patch("whisperradar.video.ffprobe.ffprobe_duration_seconds", return_value=6.0):
            audio = os.path.join(self.d, "a.mp3")
            Path(audio).write_bytes(b"x")
            out = render.render_video(self.d, audio=audio, final=True, log=lambda m: None)
        self.assertTrue(out.endswith(os.path.join("out", "final", "final.mp4")))
        self.assertEqual(calls, ["joining the scenes", "adding the narration"])

    def _clip(self, cs):
        p = os.path.join(self.d, f"{cs.spec.id}.mp4")
        Path(p).write_bytes(b"x")
        return p



class ExplainerPageTests(unittest.TestCase):
    """The Studio page for an explainer production."""

    def setUp(self):
        import sys
        from whisperradar import db
        from whisperradar.config import load_config
        from whisperradar.webapp import create_app
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root))
        self.cfg = load_config(root / "config.yaml")
        tmp = Path(tempfile.mkdtemp())
        self.cfg.db_path = tmp / "wr.db"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        pdir = tmp / "p"
        self.pid = db.create_production(conn, "Vram", "general", None, str(pdir))
        conn.close()
        app = create_app(self.cfg)
        app.testing = True
        self.client = app.test_client()
        from whisperradar import studio
        self.pdir = studio.prod_dir(self.cfg, self.pid)
        self.pdir.mkdir(parents=True, exist_ok=True)

    def test_merge_stage_offers_explainer_render(self):
        (self.pdir / "plan.json").write_text("{}", encoding="utf-8")
        html = self.client.get(f"/studio/{self.pid}?stage=merge").get_data(as_text=True)
        self.assertIn("Render explainer video", html)
        self.assertNotIn("Build preview + export to", html)

    def test_stop_button_shows_only_while_an_explainer_job_runs(self):
        from whisperradar import webapp
        self.assertNotIn("Stop scene plan", self.client.get(f"/studio/{self.pid}?stage=shots").get_data(as_text=True))
        r = self.client.post(f"/studio/{self.pid}/explainer/stop")
        self.assertIn("No explainer job", __import__("urllib.parse").parse.unquote(r.headers["Location"]))

    def test_stop_button_appears_and_cancels_a_running_scene_plan(self):
        import threading
        import urllib.parse
        from whisperradar import autorun
        (self.pdir / "subtitles.srt").write_text(SRT, encoding="utf-8")
        (self.pdir / "plan.json").write_text("{}", encoding="utf-8")        # makes it an explainer production
        started, release, seen = threading.Event(), threading.Event(), {}

        def fake(cfg, pid, stage, opts):
            seen["cancel"] = opts["cancel"]
            started.set()
            release.wait(10)
            return {}
        with mock.patch.object(autorun, "run_stage_and_advance", side_effect=fake), \
                mock.patch.object(autorun, "raise_result", lambda r: None):
            self.client.post(f"/studio/{self.pid}/scenes/generate", data={"transport": "webchat"})
            self.assertTrue(started.wait(5))
            html = self.client.get(f"/studio/{self.pid}?stage=shots").get_data(as_text=True)
            self.assertIn("Stop scene plan", html)
            self.assertFalse(seen["cancel"]())
            r = self.client.post(f"/studio/{self.pid}/explainer/stop")
            self.assertIn("Stopping the scene plan", urllib.parse.unquote(r.headers["Location"]))
            self.assertTrue(seen["cancel"]())
            release.set()

    def test_redo_scene_plan_takes_the_llm_chosen_on_the_page(self):
        from whisperradar import autorun
        (self.pdir / "subtitles.srt").write_text(SRT, encoding="utf-8")
        (self.pdir / "plan.json").write_text("{}", encoding="utf-8")
        html = self.client.get(f"/studio/{self.pid}?stage=shots").get_data(as_text=True)
        self.assertIn('name="writer"', html)
        self.assertIn('name="judge"', html)
        seen = {}

        def fake(cfg, pid, stage, opts):
            seen.update(opts)
            return {}
        with mock.patch.object(autorun, "run_stage_and_advance", side_effect=fake), \
                mock.patch.object(autorun, "raise_result", lambda r: None):
            self.client.post(f"/studio/{self.pid}/scenes/generate",
                             data={"transport": "webchat", "writer": "zai", "judge": "deepseek_judge"})
            for _ in range(50):
                if seen:
                    break
                __import__("time").sleep(0.1)
        self.assertEqual((seen.get("writer"), seen.get("judge")), ("zai", "deepseek_judge"))

    def test_an_unknown_chat_site_is_refused(self):
        (self.pdir / "subtitles.srt").write_text(SRT, encoding="utf-8")
        r = self.client.post(f"/studio/{self.pid}/scenes/generate", data={"transport": "webchat", "writer": "nope"})
        self.assertIn("Unknown chat site", __import__("urllib.parse").parse.unquote(r.headers["Location"]))

    def test_normal_production_unchanged(self):
        html = self.client.get(f"/studio/{self.pid}?stage=merge").get_data(as_text=True)
        self.assertNotIn("Render explainer video", html)
        self.assertIn("Build preview + export to", html)

    def test_image_stage_says_not_needed(self):
        (self.pdir / "plan.json").write_text("{}", encoding="utf-8")
        html = self.client.get(f"/studio/{self.pid}?stage=images").get_data(as_text=True)
        self.assertIn("no shot list, references, images or motion clips", html)

    def test_render_needs_audio_and_subtitles_not_images(self):
        (self.pdir / "plan.json").write_text("{}", encoding="utf-8")
        r = self.client.post(f"/studio/{self.pid}/video/render")
        self.assertIn("Need audio and subtitles first", __import__("urllib.parse").parse.unquote(r.headers["Location"]))

    def test_merge_accepts_the_jobs_callable_cancel(self):
        """The Studio job passes cancel as a callable; the renderer needs .is_set()."""
        (self.pdir / "plan.json").write_text("{}", encoding="utf-8")
        (self.pdir / "subtitles.srt").write_text(SRT, encoding="utf-8")
        (self.pdir / "audio.mp3").write_bytes(b"x")
        seen = {}

        def fake(folder, audio="", final=True, log=None, cancel=None):
            seen["cancelled"] = cancel.is_set()
            out = os.path.join(folder, "out", "final", "final.mp4")
            os.makedirs(os.path.dirname(out), exist_ok=True)
            Path(out).write_bytes(b"x")
            return out
        with mock.patch.object(render, "render_video", side_effect=fake):
            autorun._run_merge(self.cfg, self.pid, mode="cli", log=lambda m: None,
                               cancel=lambda: False)
        self.assertIs(seen["cancelled"], False)

    def test_shots_stage_is_the_scene_plan(self):
        (self.pdir / "explainer.json").write_text("{}", encoding="utf-8")
        html = self.client.get(f"/studio/{self.pid}?stage=shots").get_data(as_text=True)
        self.assertIn("Write scene plan (web chat)", html)
        self.assertNotIn("/shotlist/generate", html)
        self.assertEqual(autorun.stage_action(self.cfg, self.pid, "shots")["action"], "skip")
        (self.pdir / "explainer.json").unlink()

    def test_channel_visual_mode_makes_it_an_explainer(self):
        from whisperradar import db
        from whisperradar.video import settings_spec as ss
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        try:
            glob = ss.load_global(conn)
            glob["visual_mode"] = "motion_graphics"
            ss.save_global(conn, glob)
        finally:
            conn.close()
        self.assertTrue(autorun.explainer_production(self.cfg, self.pid))
        self.assertEqual(autorun.stage_action(self.cfg, self.pid, "shots")["action"], "run")
        self.assertEqual(autorun.stage_action(self.cfg, self.pid, "images")["action"], "skip")

    def test_autorun_plan_skips_image_stages(self):
        (self.pdir / "plan.json").write_text("{}", encoding="utf-8")
        for st in autorun.EXPLAINER_SKIPPED:
            self.assertEqual(autorun.stage_action(self.cfg, self.pid, st)["action"], "skip")
        self.assertEqual(autorun.stage_action(self.cfg, self.pid, "merge")["action"], "run")
        self.assertEqual(autorun.run_stage(self.cfg, self.pid, "images"), "ok")


if __name__ == "__main__":
    unittest.main()
