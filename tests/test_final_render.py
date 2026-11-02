"""Full-size final render after review (normal channels) and the explainer's output size."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.video.explainer import remotion_backend as rb, render as xr  # noqa: E402
from whisperradar.video.explainer.validate import ExplainerError  # noqa: E402


class ExplainerSizeTests(unittest.TestCase):
    def test_scene_sizes(self):
        self.assertEqual(rb.scale_for(1920, 1080), 1.0)
        self.assertEqual(rb.scale_for(960, 540), 0.5)
        self.assertEqual(rb.scale_for(3840, 2160), 2.0)
        self.assertEqual(round(1920 * rb.scale_for(2560, 1440)), 2560)
        self.assertEqual(round(1080 * rb.scale_for(2560, 1440)), 1440)
        with self.assertRaises(ExplainerError):
            rb.scale_for(1376, 768)

    def test_output_canvas_follows_the_render_resolution(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(xr.output_canvas(d, (1920, 1080)), (1920, 1080))     # no file
            for size, want in (((1920, 1080), (1920, 1080)), ((2560, 1440), (2560, 1440)),
                               ((3840, 2160), (3840, 2160)),
                               ((1376, 768), (1920, 1080)),                       # Flow native: 1080p
                               ((1080, 1920), (1920, 1080))):                     # portrait: base
                (Path(d) / "imgtovideo.json").write_text(
                    json.dumps({"output": {"width": size[0], "height": size[1]}}), encoding="utf-8")
                self.assertEqual(xr.output_canvas(d, (1920, 1080)), want, size)
            (Path(d) / "imgtovideo.json").write_text("{broken", encoding="utf-8")
            self.assertEqual(xr.output_canvas(d, (1920, 1080)), (1920, 1080))


class FinalRenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.cfg.studio_video_engine = "python"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.pid = db.create_production(conn, "Normal", "general", None, None)
        conn.commit()
        conn.close()
        self.pdir = studio.prod_dir(self.cfg, self.pid)
        self.pdir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def _project(self, preview=True):
        (self.pdir / "audio.mp3").write_bytes(b"\x00" * 8)
        (self.pdir / "subtitles.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")
        (self.pdir / "images").mkdir(exist_ok=True)
        (self.pdir / "images" / "S01_01.png").write_bytes(b"\x89PNG")
        if preview:
            (self.pdir / "out").mkdir(exist_ok=True)
            (self.pdir / "out" / "preview.mp4").write_bytes(b"\x00" * 16)

    def test_final_render_runs_the_full_size_engine_pass(self):
        calls = {}

        def fake_render_final(folder, preview, **kw):
            calls["preview"] = preview
            out = Path(folder) / "out" / "final"
            out.mkdir(parents=True, exist_ok=True)
            (out / "final.mp4").write_bytes(b"x")
            kw["progress"](0.5)
            return True, str(out / "final.mp4"), []

        planned = mock.Mock(success=True, issues=[], config_errors=[])
        logs = []
        with mock.patch("whisperradar.video.plan.plan_project", return_value=planned), \
                mock.patch("whisperradar.video.render_project.render_final", side_effect=fake_render_final):
            final = studio.run_final_render(self.cfg, self.pdir, log=logs.append)
        self.assertFalse(calls["preview"])
        self.assertEqual(final, self.pdir / "out" / "final" / "final.mp4")
        self.assertTrue(any("%" in m for m in logs))

    def test_a_failed_render_raises(self):
        planned = mock.Mock(success=True, issues=[], config_errors=[])
        with mock.patch("whisperradar.video.plan.plan_project", return_value=planned), \
                mock.patch("whisperradar.video.render_project.render_final",
                           return_value=(False, "", ["ffmpeg died"])):
            with self.assertRaises(RuntimeError):
                studio.run_final_render(self.cfg, self.pdir)

    def _client(self):
        from whisperradar.webapp import create_app
        app = create_app(self.cfg)
        app.testing = True
        return app.test_client()

    def test_button_needs_a_preview_and_names_the_size(self):
        client = self._client()
        self._project(preview=False)
        html = client.get(f"/studio/{self.pid}?stage=merge").get_data(as_text=True)
        self.assertNotIn("/video/render_final", html)
        (self.pdir / "out").mkdir(exist_ok=True)
        (self.pdir / "out" / "preview.mp4").write_bytes(b"\x00" * 16)
        html = client.get(f"/studio/{self.pid}?stage=merge").get_data(as_text=True)
        self.assertIn(f"/studio/{self.pid}/video/render_final", html)
        self.assertIn("Render final video", html)
        import re
        self.assertRegex(html, r"Render final video \(\d+x\d+")

    def test_route_refuses_without_a_preview_and_runs_with_one(self):
        client = self._client()
        self._project(preview=False)
        r = client.post(f"/studio/{self.pid}/video/render_final", follow_redirects=True)
        self.assertIn("preview first", r.get_data(as_text=True))
        self._project(preview=True)
        called = []

        def fake(cfg, pdir, **kw):
            called.append(kw)
            (Path(pdir) / "out" / "final").mkdir(parents=True, exist_ok=True)
            (Path(pdir) / "out" / "final" / "final.mp4").write_bytes(b"x")
            return Path(pdir) / "out" / "final" / "final.mp4"

        with mock.patch.object(studio, "run_final_render", side_effect=fake):
            r = client.post(f"/studio/{self.pid}/video/render_final", follow_redirects=True)
            for _ in range(50):
                if called:
                    break
                time.sleep(0.1)
        self.assertIn("Final render started", r.get_data(as_text=True))
        self.assertTrue(called)

    def test_explainer_production_is_refused(self):
        client = self._client()
        self._project()
        (self.pdir / "plan.json").write_text("{}", encoding="utf-8")
        r = client.post(f"/studio/{self.pid}/video/render_final", follow_redirects=True)
        self.assertIn("already rendered full size", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
