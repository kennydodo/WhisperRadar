"""Stage 8 (merge/render) must not start while shotlist images are missing."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_external_prompts import Base  # noqa: E402
from whisperradar import autorun, studio  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class MergeImageCheckTests(Base):
    def setUp(self):
        super().setUp()
        plan = {"shots": [], "images": [
            {"file": "a_ST.png", "prompt": "one"},
            {"file": "b_ST.png", "prompt": "two"},
            {"file": "c_ST.png", "prompt": "three"}]}
        (self.pdir / "shotlist.json").write_text(json.dumps(plan), "utf-8")
        self.img = self.pdir / "images"
        self.img.mkdir(exist_ok=True)

    def test_missing_images_are_named_and_counted(self):
        (self.img / "a_ST.png").write_bytes(b"x")
        reason = autorun._merge_pause_reason(self.cfg, self.pid)
        self.assertIn("only 1 of 3", reason)
        self.assertIn("b_ST.png", reason)
        self.assertIn("c_ST.png", reason)

    def test_all_present_is_fine(self):
        for n in ("a_ST.png", "b_ST.png", "c_ST.png"):
            (self.img / n).write_bytes(b"x")
        self.assertIsNone(autorun._merge_pause_reason(self.cfg, self.pid))

    def _merge(self, mode, creates):
        """Run _run_merge with 1 of 3 images present; the (mocked) images
        stage creates `creates`. Returns (images_ran, render_ran, error)."""
        (self.img / "a_ST.png").write_bytes(b"x")
        ran = {"images": 0}

        def fake_images(cfg, pid, **kw):
            ran["images"] += 1
            for n in creates:
                (self.img / n).write_bytes(b"x")

        err = None
        with mock.patch.object(studio, "find_audio", return_value=Path("a.mp3")), \
             mock.patch.object(studio, "find_srt", return_value=Path("a.srt")), \
             mock.patch.object(autorun, "_stage_params", return_value={}), \
             mock.patch.object(autorun, "_run_images", fake_images), \
             mock.patch.object(autorun, "_effective",
                               return_value={"render_target": "premiere"}), \
             mock.patch.object(studio, "run_merge_render",
                               return_value={}) as render:
            try:
                autorun._run_merge(self.cfg, self.pid, mode)
            except (RuntimeError, autorun._Paused) as exc:
                err = exc
        return ran["images"], render.called, err

    def test_missing_images_are_rendered_first_then_the_merge_runs(self):
        for mode in ("cli", None):
            with self.subTest(mode=mode):
                for n in ("b_ST.png", "c_ST.png"):
                    (self.img / n).unlink(missing_ok=True)
                images, render, err = self._merge(mode, ["b_ST.png", "c_ST.png"])
                self.assertEqual((images, render, err), (1, True, None))

    def test_nothing_missing_means_no_extra_image_run(self):
        for n in ("b_ST.png", "c_ST.png"):
            (self.img / n).write_bytes(b"x")
        images, render, err = self._merge("cli", [])
        self.assertEqual((images, render, err), (0, True, None))

    def test_still_missing_after_the_image_run_nothing_is_merged(self):
        images, render, err = self._merge("cli", [])
        self.assertEqual((images, render), (1, False))
        self.assertIn("still missing", str(err))
        (self.img / "a_ST.png").unlink()
        images, render, err = self._merge(None, [])      # auto-run: pauses
        self.assertFalse(render)
        self.assertIsInstance(err, autorun._Paused)

    def test_the_render_button_says_images_are_rendered_first(self):
        (self.img / "a_ST.png").write_bytes(b"x")
        c = create_app(self.cfg).test_client()
        with mock.patch.object(studio, "find_audio", return_value=Path("a.mp3")), \
             mock.patch.object(studio, "find_srt", return_value=Path("a.srt")), \
             mock.patch.object(autorun, "run_stage_and_advance",
                               return_value="ok"):
            r = c.post(f"/studio/{self.pid}/video/render")
            import time
            time.sleep(0.3)
        self.assertEqual(r.status_code, 302)
        self.assertIn("rendering", r.headers["Location"].replace("+", " "))


if __name__ == "__main__":
    unittest.main()
