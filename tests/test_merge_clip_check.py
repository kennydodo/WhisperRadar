"""The merge/render gate must also refuse to start while opening clips are
missing (the animated opening) - a render with clip gaps bakes the stills
into preview.mp4 and the NLE exports."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_merge_image_check import MergeImageCheckTests  # noqa: E402
from whisperradar import autorun, studio  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


def _animate_on():
    """_effective() with the animated opening switched on: two shots in the
    60s window, their images on disk, clips pointing into clips\\."""
    def eff(cfg, pid):
        return {"render_target": "premiere", "animate_enabled": True,
                "animate_seconds": 60, "animate_model": "Veo 3.1 Lite"}
    return eff


class MergeClipCheckTests(MergeImageCheckTests):
    def setUp(self):
        super().setUp()
        # the images gate must be satisfied first so the clip gate is the one
        # under test: put every shotlist image on disk
        for n in ("a_ST.png", "b_ST.png", "c_ST.png"):
            (self.img / n).write_bytes(b"x")
        # shotlist entries the window_clips pass will pick up: shot entries
        # with cues inside the first 60s and ZI/ST/ZO/PD codes
        plan = json.loads((self.pdir / "shotlist.json").read_text("utf-8"))
        plan["shots"] = [
            {"asset": "a_ST.png", "cues": "1-3", "ai_motion": "pan"},
            {"asset": "b_ST.png", "cues": "4-6", "ai_motion": "pan"},
            {"asset": "c_ST.png", "cues": "7-9", "ai_motion": "pan"}]
        (self.pdir / "shotlist.json").write_text(json.dumps(plan), "utf-8")
        clips = self.pdir / "clips"
        clips.mkdir(exist_ok=True)
        self.clips = clips

    def _clips(self, present):
        """_motion_clips patched to the window pass without touching the SRT."""
        def fake(cfg, pid, pdir, eff):
            return [{"asset": f"{n}_ST.png", "image": True,
                     "out": str(self.clips / f"{n}_ST.mp4"),
                     "prompt": "pan"}
                    for n in ("a", "b", "c")]
        return fake

    def test_missing_images_are_named_and_counted(self):
        for n in ("b_ST.png", "c_ST.png"):
            (self.img / n).unlink(missing_ok=True)
        with mock.patch.object(autorun, "_effective", _animate_on()), \
             mock.patch.object(autorun, "_motion_clips", self._clips(None)):
            reason = autorun._merge_pause_reason(self.cfg, self.pid)
        self.assertIn("only 1 of 3", reason)
        self.assertIn("b_ST.png", reason)
        self.assertIn("c_ST.png", reason)

    def test_nothing_missing_means_no_extra_image_run(self):
        for n in ("a", "b", "c"):
            (self.clips / f"{n}_ST.mp4").write_bytes(b"x" * 20_000)
        images, render, err = self._merge("cli", ["b_ST.png", "c_ST.png"])
        # the images stage renders the 2 missing stills, then the merge runs
        self.assertEqual((images, render, err), (1, True, None))

    def test_the_render_button_says_images_are_rendered_first(self):
        for n in ("a", "b", "c"):
            (self.clips / f"{n}_ST.mp4").write_bytes(b"x" * 20_000)
        for n in ("b_ST.png", "c_ST.png"):
            (self.img / n).unlink(missing_ok=True)
        c = create_app(self.cfg).test_client()
        with mock.patch.object(studio, "find_audio", return_value=Path("a.mp3")), \
             mock.patch.object(studio, "find_srt", return_value=Path("a.srt")), \
             mock.patch.object(studio, "find_images", return_value=[Path("x")]), \
             mock.patch.object(autorun, "_effective", _animate_on()), \
             mock.patch.object(autorun, "_motion_clips", self._clips(None)), \
             mock.patch.object(autorun, "run_stage_and_advance",
                               return_value="ok"):
            r = c.post(f"/studio/{self.pid}/video/render")
            import time
            time.sleep(0.3)
        self.assertEqual(r.status_code, 302)
        self.assertIn("rendering", r.headers["Location"].replace("+", " "))

    def test_missing_clips_block_the_merge(self):
        (self.clips / "a_ST.mp4").write_bytes(b"x" * 20_000)
        with mock.patch.object(autorun, "_effective", _animate_on()), \
             mock.patch.object(autorun, "_motion_clips", self._clips(None)):
            reason = autorun._merge_pause_reason(self.cfg, self.pid)
        self.assertIsNotNone(reason)
        self.assertIn("opening clips", reason)
        self.assertIn("b_ST", reason)
        self.assertIn("c_ST", reason)

    def test_all_clips_present_is_fine(self):
        for n in ("a", "b", "c"):
            (self.clips / f"{n}_ST.mp4").write_bytes(b"x" * 20_000)
        with mock.patch.object(autorun, "_effective", _animate_on()), \
             mock.patch.object(autorun, "_motion_clips", self._clips(None)):
            self.assertIsNone(autorun._merge_pause_reason(self.cfg, self.pid))

    def test_tiny_clip_files_count_as_missing(self):
        for n in ("a", "b", "c"):
            (self.clips / f"{n}_ST.mp4").write_bytes(b"x" * 500)  # < 10 KB
        with mock.patch.object(autorun, "_effective", _animate_on()), \
             mock.patch.object(autorun, "_motion_clips", self._clips(None)):
            reason = autorun._merge_pause_reason(self.cfg, self.pid)
        self.assertIsNotNone(reason)
        self.assertIn("opening clips", reason)

    def test_merge_pauses_for_clips_instead_of_running_images(self):
        """The images stage cannot fill clip gaps: with clips missing the
        merge must pause (auto-run), not burn an images pass."""
        ran = {"images": 0}

        def fake_images(cfg, pid, **kw):
            ran["images"] += 1

        with mock.patch.object(autorun, "_effective", _animate_on()), \
             mock.patch.object(autorun, "_motion_clips", self._clips(None)), \
             mock.patch.object(studio, "find_audio", return_value=Path("a.mp3")), \
             mock.patch.object(studio, "find_srt", return_value=Path("a.srt")), \
             mock.patch.object(studio, "find_images", return_value=[Path("x")]), \
             mock.patch.object(autorun, "_stage_params", return_value={}), \
             mock.patch.object(autorun, "_run_images", fake_images), \
             mock.patch.object(studio, "run_merge_render", return_value={}):
            with self.assertRaises(autorun._Paused) as ctx:
                autorun._run_merge(self.cfg, self.pid, None)
        self.assertIn("opening clips", str(ctx.exception))
        self.assertEqual(ran["images"], 0)

    def test_explicit_mode_raises_a_clear_error_for_clips(self):
        with mock.patch.object(autorun, "_effective", _animate_on()), \
             mock.patch.object(autorun, "_motion_clips", self._clips(None)), \
             mock.patch.object(studio, "find_audio", return_value=Path("a.mp3")), \
             mock.patch.object(studio, "find_srt", return_value=Path("a.srt")), \
             mock.patch.object(studio, "find_images", return_value=[Path("x")]), \
             mock.patch.object(autorun, "_stage_params", return_value={}):
            with self.assertRaises(RuntimeError) as ctx:
                autorun._run_merge(self.cfg, self.pid, "cli")
        self.assertIn("opening clips", str(ctx.exception))

    def test_with_stills_bypasses_the_clip_gate_but_not_images(self):
        """The user's explicit 'render with stills' choice: missing clips no
        longer pause the merge (missing images still do)."""
        (self.img / "a_ST.png").write_bytes(b"x")
        with mock.patch.object(autorun, "_effective", _animate_on()), \
             mock.patch.object(autorun, "_motion_clips", self._clips(None)), \
             mock.patch.object(studio, "find_audio", return_value=Path("a.mp3")), \
             mock.patch.object(studio, "find_srt", return_value=Path("a.srt")), \
             mock.patch.object(studio, "find_images", return_value=[Path("x")]), \
             mock.patch.object(autorun, "_stage_params", return_value={}), \
             mock.patch.object(studio, "run_merge_render", return_value={}) as render:
            # 1 of 3 images missing AND clips missing: the images pass runs
            # (the mock creates the rest), then the merge renders
            autorun._run_merge(self.cfg, self.pid, "cli", with_stills=True)
        self.assertTrue(render.called)

    def test_with_stills_renders_straight_when_images_are_complete(self):
        (self.img / "a_ST.png").write_bytes(b"x")
        (self.img / "b_ST.png").write_bytes(b"x")
        (self.img / "c_ST.png").write_bytes(b"x")
        with mock.patch.object(autorun, "_effective", _animate_on()), \
             mock.patch.object(autorun, "_motion_clips", self._clips(None)), \
             mock.patch.object(studio, "find_audio", return_value=Path("a.mp3")), \
             mock.patch.object(studio, "find_srt", return_value=Path("a.srt")), \
             mock.patch.object(studio, "find_images", return_value=[Path("x")]), \
             mock.patch.object(autorun, "_stage_params", return_value={}), \
             mock.patch.object(studio, "run_merge_render", return_value={}) as render:
            autorun._run_merge(self.cfg, self.pid, "cli", with_stills=True)
        self.assertTrue(render.called)

    def test_render_route_accepts_the_with_stills_choice(self):
        c = create_app(self.cfg).test_client()
        for n in ("a_ST.png", "b_ST.png", "c_ST.png"):
            (self.img / n).write_bytes(b"x")
        with mock.patch.object(autorun, "_effective", _animate_on()), \
             mock.patch.object(autorun, "_motion_clips", self._clips(None)), \
             mock.patch.object(studio, "find_audio", return_value=Path("a.mp3")), \
             mock.patch.object(studio, "find_srt", return_value=Path("a.srt")), \
             mock.patch.object(studio, "find_images", return_value=[Path("x")]), \
             mock.patch.object(autorun, "run_stage_and_advance",
                               return_value="ok") as run:
            r = c.post(f"/studio/{self.pid}/video/render",
                       data={"render_with_stills": "1"})
            import time
            time.sleep(0.3)
        self.assertEqual(r.status_code, 302)
        self.assertIn("stills", r.headers["Location"].replace("+", " "))
        self.assertTrue(run.called)
        params = run.call_args[0][3] if len(run.call_args[0]) > 3 \
            else run.call_args.kwargs.get("params") or {}
        self.assertTrue(params.get("with_stills"))

    def test_with_all_clips_the_inherited_image_path_is_unchanged(self):
        """Clips complete + images missing: the images stage fills the gap and
        the merge runs, exactly like the images-only gate before."""
        for n in ("a", "b", "c"):
            (self.clips / f"{n}_ST.mp4").write_bytes(b"x" * 20_000)
        images, render, err = self._merge("cli", ["b_ST.png", "c_ST.png"])
        self.assertEqual((images, render, err), (1, True, None))

    def _merge(self, mode, creates):
        """Run _run_merge with the images gate failing (1 of 3 images on
        disk) and all clips present; the (mocked) images stage creates
        `creates`. Returns (images_ran, render_ran, error)."""
        for n in ("a", "b", "c"):
            (self.clips / f"{n}_ST.mp4").write_bytes(b"x" * 20_000)
        for n in ("a_ST.png", "b_ST.png", "c_ST.png"):
            (self.img / n).unlink(missing_ok=True)
        (self.img / "a_ST.png").write_bytes(b"x")
        ran = {"images": 0}

        def fake_images(cfg, pid, **kw):
            ran["images"] += 1
            for n in creates:
                (self.img / n).write_bytes(b"x")

        err = None
        with mock.patch.object(autorun, "_effective", _animate_on()), \
             mock.patch.object(autorun, "_motion_clips", self._clips(None)), \
             mock.patch.object(studio, "find_audio", return_value=Path("a.mp3")), \
             mock.patch.object(studio, "find_srt", return_value=Path("a.srt")), \
             mock.patch.object(studio, "find_images", return_value=[Path("x")]), \
             mock.patch.object(autorun, "_stage_params", return_value={}), \
             mock.patch.object(autorun, "_run_images", fake_images), \
             mock.patch.object(studio, "run_merge_render",
                               return_value={}) as render:
            try:
                autorun._run_merge(self.cfg, self.pid, mode)
            except (RuntimeError, autorun._Paused) as exc:
                err = exc
        return ran["images"], render.called, err


if __name__ == "__main__":
    unittest.main()
