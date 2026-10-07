"""Flow native + local Real-ESRGAN: "download masters first, upscale when the
downloads finish", for BOTH engines.

Selecting Render resolution = Flow native reveals an upscale level (off / 1k /
2k / 4k). Both engines then download at Flow's native size and ONE local
Real-ESRGAN pass (FlowBatch's upscaler - never the Renderly backend) upscales
the stills afterwards.

Covers the engine-independent runner (counting, idempotent skips, tier-off /
empty no-ops, the Lanczos-fallback warning, the actionable error when
FlowBatch is not configured), the level resolution (channel over global, only
for Flow native), the images-stage hook that drives both engines with
upscaling OFF and then runs the pass once, gallery recovery, and the manual
IMAGES-stage action. Everything is stubbed - no Flow, no browser, no paid API.

Run: python -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import autorun, db, services, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402

SHOTLIST = {
    "shots": [{"asset": "S01_01.png", "cues": "1-1"}],
    "images": [{"file": "S01_01.png", "prompt": "a red fox"}],
    "refs": {},
}


class LocalUpscaleRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.pdir = Path(self.tmp.name) / "prod"
        (self.pdir / "images").mkdir(parents=True)
        (self.pdir / "images" / "S01_01.png").write_bytes(b"\x89PNG" + b"x" * 40)
        self.repo = Path(self.tmp.name) / "flowbatch"
        self.repo.mkdir()

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def test_flowbatch_in_place_pass_counts_the_log_lines(self):
        def fake_stream(cmd, repo, log, cancel, on_line=None):
            lines = [
                "20:00:00 INFO  a.png: skipped (already 2560x1440 (tier 2k wants 2560x1440))",
                "20:00:05 OK    b.png (in place) 2560x1440 via Lanczos CPU (5.0s)",
            ]
            for line in lines:
                if on_line:
                    on_line(line)
            return 0, lines

        with mock.patch.object(studio, "flowbatch_ready", side_effect=lambda c: True), \
                mock.patch.object(studio, "flowbatch_dir", side_effect=lambda c: self.repo), \
                mock.patch.object(studio, "_flowbatch_stream", fake_stream):
            result = studio.upscale_images_locally(self.cfg, self.pdir, tier=2)

        self.assertEqual(result["engine"], "flowbatch")
        self.assertEqual(result["tier"], "2k")
        self.assertEqual(result["upscaled"], 1)
        self.assertEqual(result["skipped"], 1)
        # one file exists but two log lines were counted: failed never goes
        # negative (it is derived from the file count)
        self.assertEqual(result["failed"], 0)

    def test_tier_off_and_an_empty_folder_never_touch_the_upscaler(self):
        with mock.patch.object(studio, "flowbatch_ready",
                               side_effect=lambda c: True) as ready:
            off = studio.upscale_images_locally(self.cfg, self.pdir, tier=0)
        self.assertEqual(off, {"engine": "off", "tier": "off", "total": 0,
                               "upscaled": 0, "skipped": 0, "failed": 0})
        ready.assert_not_called()

        empty = Path(self.tmp.name) / "empty"
        (empty / "images").mkdir(parents=True)
        with mock.patch.object(studio, "flowbatch_ready",
                               side_effect=lambda c: True) as ready2:
            result = studio.upscale_images_locally(self.cfg, empty, tier=2)
        self.assertEqual(result["total"], 0)
        ready2.assert_not_called()

    def test_no_local_upscaler_is_an_actionable_error(self):
        with mock.patch.object(studio, "flowbatch_ready", side_effect=lambda c: False):
            with self.assertRaises(RuntimeError) as ctx:
                studio.upscale_images_locally(self.cfg, self.pdir, tier=2)
        self.assertIn("No local upscaler available", str(ctx.exception))
        self.assertIn("flowbatch_repo", str(ctx.exception))

    def test_the_renderly_backend_upscaler_is_gone(self):
        for name in ("_renderly_backend_dir", "_renderly_python",
                     "_RENDERLY_UPSCALE_PY"):
            self.assertFalse(hasattr(studio, name), name)

    def test_tier_names_and_numbers_both_work(self):
        seen = []

        def fake_stream(cmd, repo, log, cancel, on_line=None):
            seen.append(cmd[cmd.index("--tier") + 1])
            return 0, []

        with mock.patch.object(studio, "flowbatch_ready", side_effect=lambda c: True), \
                mock.patch.object(studio, "flowbatch_dir", side_effect=lambda c: self.repo), \
                mock.patch.object(studio, "_flowbatch_stream", fake_stream):
            studio.upscale_images_locally(self.cfg, self.pdir, tier="4k")
            studio.upscale_images_locally(self.cfg, self.pdir, tier=2)
            off = studio.upscale_images_locally(self.cfg, self.pdir, tier="bogus")
        self.assertEqual(seen, ["4k", "2k"])
        self.assertEqual(off["tier"], "off")

    def test_a_lanczos_fallback_is_reported_not_hidden(self):
        logged = []

        def fake_stream(cmd, repo, log, cancel, on_line=None):
            line = "20:00:05 OK    b.png (in place) 2560x1440 via Lanczos CPU (5.0s)"
            on_line(line)
            return 0, [line]

        with mock.patch.object(studio, "flowbatch_ready", side_effect=lambda c: True), \
                mock.patch.object(studio, "flowbatch_dir", side_effect=lambda c: self.repo), \
                mock.patch.object(studio, "_flowbatch_stream", fake_stream):
            result = studio.upscale_images_locally(
                self.cfg, self.pdir, tier="2k", log=logged.append)
        self.assertEqual(result["lanczos"], 1)
        self.assertTrue(any("Lanczos fallback" in m for m in logged))


class FlowNativeLevelTests(unittest.TestCase):
    """The level only exists for Flow native, channel over global."""

    def test_only_flow_native_has_a_post_download_pass(self):
        for res in ("1080p", "2k", "4k", "", None):
            eff = {"render_resolution": res, "flow_native_upscale": "2k",
                   "upscale": 2}
            self.assertEqual(autorun.post_download_tier(eff), "off", res)
        eff = {"render_resolution": "flow-native", "flow_native_upscale": "2k",
               "upscale": 2}
        self.assertEqual(autorun.post_download_tier(eff), "2k")

    def test_flow_native_downloads_native_whatever_the_tier_says(self):
        eff = {"render_resolution": "flow-native", "flow_native_upscale": "4k",
               "upscale": 3}
        self.assertEqual(autorun._upscale_for(eff), 0)

    def test_an_unknown_or_empty_level_means_off(self):
        for lvl in ("off", "", None, "8k", "2"):
            eff = {"render_resolution": "flow-native",
                   "flow_native_upscale": lvl}
            self.assertEqual(autorun.post_download_tier(eff), "off", lvl)

    def test_the_manual_button_uses_the_native_level_else_the_tier(self):
        native = {"render_resolution": "flow-native",
                  "flow_native_upscale": "1k", "upscale": 4}
        self.assertEqual(autorun.manual_upscale_tier(native), "1k")
        plain = {"render_resolution": "2k", "flow_native_upscale": "1k",
                 "upscale": 4}
        self.assertEqual(autorun.manual_upscale_tier(plain), "4k")
        self.assertEqual(autorun.manual_upscale_tier(
            {"render_resolution": "2k", "upscale": 0}), "off")


class ImagesUpscaleHookTests(unittest.TestCase):
    """_run_images must download masters and upscale ONCE at the end when the
    resolution is Flow native with a level, and keep today's inline upscaling
    for every other resolution."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        chan = db.create_own_channel(conn, "Ch")
        self.pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, self.pid, own_channel_id=chan)
        db.set_setting(conn, "default_engine", "flowbatch")
        db.set_setting(conn, "default_upscale", "2")
        conn.commit()
        conn.close()
        pdir = studio.prod_dir(self.cfg, self.pid)
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "shotlist.json").write_text(json.dumps(SHOTLIST),
                                            encoding="utf-8")
        (pdir / "images").mkdir(exist_ok=True)

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def _set(self, key, value):
        conn = db.connect(self.cfg.db_path)
        db.set_setting(conn, key, value)
        conn.commit()
        conn.close()

    def _native(self, level="2k"):
        """Render resolution = Flow native, with the global upscale level."""
        self._set("render_resolution", "flow-native")
        self._set("flow_native_upscale", level)

    def _image_steps(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        try:
            return [s for s in db.step_history(conn, self.pid)
                    if s["stage"] == "images"]
        finally:
            conn.close()

    def test_flow_native_downloads_masters_then_upscales_once(self):
        self._native("2k")
        seen = {}

        def fake_generate(cfg, pdir, pid, upscale=None, log=None, cancel=None,
                          project_url=None):
            seen["upscale"] = upscale
            return 1

        with mock.patch.object(studio, "run_imagegen_flowbatch", fake_generate), \
                mock.patch.object(studio, "upscale_images_locally",
                                  side_effect=lambda *a, **k: {
                                      "engine": "flowbatch", "tier": "2k",
                                      "total": 1, "upscaled": 1,
                                      "skipped": 0, "failed": 0}) as up, \
                mock.patch.object(services.MANAGER, "ensure"), \
                mock.patch.object(services.MANAGER, "release"):
            autorun._run_images(self.cfg, self.pid)

        self.assertEqual(seen["upscale"], 0, "the engine must deliver masters")
        up.assert_called_once()
        self.assertEqual(up.call_args.kwargs["tier"], "2k")
        detail = self._image_steps()[0]["detail"]
        self.assertIn("local upscale to 2k", detail)
        self.assertIn("FlowBatch", detail)

    def test_other_resolutions_keep_inline_upscaling(self):
        self._set("render_resolution", "2k")
        self._set("flow_native_upscale", "4k")  # ignored: not Flow native
        seen = {}

        def fake_generate(cfg, pdir, pid, upscale=None, log=None, cancel=None,
                          project_url=None):
            seen["upscale"] = upscale
            return 1

        with mock.patch.object(studio, "run_imagegen_flowbatch", fake_generate), \
                mock.patch.object(studio, "upscale_images_locally") as up, \
                mock.patch.object(services.MANAGER, "ensure"), \
                mock.patch.object(services.MANAGER, "release"):
            autorun._run_images(self.cfg, self.pid)

        # Unchanged pass-through: the caller's value (None here) reaches the
        # engine, which then falls back to its configured tier - i.e. inline
        # upscaling exactly as before.
        self.assertIsNone(seen["upscale"])
        up.assert_not_called()
        self.assertNotIn("local upscale", self._image_steps()[0]["detail"])

    def test_flow_native_covers_the_flow_mode_too(self):
        """The flow-mode (FlowBatch) path must send an explicit 0: omitting
        --upscale would let the engine fall back to its own tier and upscale
        during the download, which is exactly what this mode avoids."""
        self._native("2k")
        conn = db.connect(self.cfg.db_path)
        db.set_setting(conn, "default_engine", "renderly")
        db.update_production(conn, self.pid, render_mode="flow")
        conn.commit()
        conn.close()
        seen = {}

        def fake_flow(cfg, pdir, pid, upscale=None, log=None, cancel=None,
                      project_url=None):
            seen["upscale"] = upscale
            return 1

        with mock.patch.object(studio, "run_imagegen_flowbatch",
                               side_effect=fake_flow), \
                mock.patch.object(studio, "upscale_images_locally",
                                  side_effect=lambda *a, **k: {
                                      "engine": "flowbatch", "tier": "2k",
                                      "total": 1, "upscaled": 1,
                                      "skipped": 0, "failed": 0}) as up, \
                mock.patch.object(services.MANAGER, "ensure"), \
                mock.patch.object(services.MANAGER, "release"):
            autorun._run_images(self.cfg, self.pid)

        self.assertEqual(seen["upscale"], 0)
        up.assert_called_once()

    def test_recover_follows_the_same_rule(self):
        """Gallery recovery downloads masters too, so it runs the same single
        local pass at the end instead of upscaling each adopted file."""
        self._native("2k")
        seen = {}

        def fake_recover(cfg, pdir, pid, upscale=None, log=None, cancel=None,
                         project_url=None):
            seen["upscale"] = upscale
            return {"recovered": ["S01_01.png"], "still_missing": []}

        with mock.patch.object(studio, "run_flowbatch_recover",
                               side_effect=fake_recover), \
                mock.patch.object(studio, "upscale_images_locally",
                                  side_effect=lambda *a, **k: {
                                      "engine": "flowbatch", "tier": "2k",
                                      "total": 1, "upscaled": 1,
                                      "skipped": 0, "failed": 0}) as up, \
                mock.patch.object(services.MANAGER, "ensure"), \
                mock.patch.object(services.MANAGER, "release"):
            autorun.recover_images(self.cfg, self.pid)

        self.assertEqual(seen["upscale"], 0)
        up.assert_called_once()
        self.assertIn("local upscale to 2k", self._image_steps()[0]["detail"])

    def test_flow_native_with_the_level_off_downloads_native_and_never_upscales(self):
        self._native("off")
        seen = {}

        def fake_generate(cfg, pdir, pid, upscale=None, log=None, cancel=None,
                          project_url=None):
            seen["upscale"] = upscale
            return 1

        with mock.patch.object(studio, "run_imagegen_flowbatch", fake_generate), \
                mock.patch.object(studio, "upscale_images_locally") as up, \
                mock.patch.object(services.MANAGER, "ensure"), \
                mock.patch.object(services.MANAGER, "release"):
            autorun._run_images(self.cfg, self.pid)
        self.assertEqual(seen["upscale"], 0, "never fall back to the engine's tier")
        up.assert_not_called()

    def test_a_channel_level_overrides_the_global_one(self):
        self._native("4k")
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        chan = db.get_production(conn, self.pid)["own_channel_id"]
        db.update_own_channel(conn, chan, flow_native_upscale="1k")
        conn.close()
        with mock.patch.object(studio, "run_imagegen_flowbatch",
                               side_effect=lambda *a, **k: 1), \
                mock.patch.object(studio, "upscale_images_locally",
                                  side_effect=lambda *a, **k: {
                                      "engine": "flowbatch", "tier": "1k",
                                      "total": 1, "upscaled": 1,
                                      "skipped": 0, "failed": 0}) as up, \
                mock.patch.object(services.MANAGER, "ensure"), \
                mock.patch.object(services.MANAGER, "release"):
            autorun._run_images(self.cfg, self.pid)
        self.assertEqual(up.call_args.kwargs["tier"], "1k")

    def test_the_renderly_api_path_never_upscales_in_the_backend(self):
        self._native("2k")
        conn = db.connect(self.cfg.db_path)
        db.set_setting(conn, "default_engine", "renderly")
        db.update_production(conn, self.pid, render_mode="api")
        conn.commit()
        conn.close()
        seen = {}
        # a wide (PL) shot so the API pass is part of the run (Flow goes first
        # for the rest, the API last for PL/PR)
        pdir = studio.prod_dir(self.cfg, self.pid)
        (pdir / "shotlist.json").write_text(json.dumps({
            "images": [{"file": "S01_01_SCN_ZI.png", "prompt": "a"},
                       {"file": "S01_02_SCN_PL.png", "prompt": "b"}]}),
            encoding="utf-8")

        def fake_api(cfg, pdir, channel=None, upscale=None, motion_filter=None):
            seen["api_upscale"] = upscale
            (pdir / "images" / "S01_02_SCN_PL.png").write_bytes(b"x")
            return 1

        def fake_flow(cfg, pdir, pid, upscale=None, log=None, cancel=None,
                      project_url=None, skip_motion=None):
            seen["flow_upscale"] = upscale
            (pdir / "images" / "S01_01_SCN_ZI.png").write_bytes(b"x")
            return 1

        with mock.patch.object(studio, "run_imagegen", side_effect=fake_api), \
                mock.patch.object(studio, "run_imagegen_flowbatch",
                                  side_effect=fake_flow), \
                mock.patch.object(studio, "upscale_images_locally",
                                  side_effect=lambda *a, **k: {
                                      "engine": "flowbatch", "tier": "2k",
                                      "total": 1, "upscaled": 1,
                                      "skipped": 0, "failed": 0}) as up, \
                mock.patch.object(services.MANAGER, "ensure"), \
                mock.patch.object(services.MANAGER, "release"):
            autorun._run_images(self.cfg, self.pid, mode="api")
        self.assertEqual(seen["api_upscale"], 0)
        self.assertEqual(seen["flow_upscale"], 0)
        up.assert_called_once()

    def test_the_plan_detail_says_what_will_happen(self):
        self._native("2k")
        eff = autorun._effective(self.cfg, self.pid)
        self.assertEqual(autorun._upscale_text(eff),
                         "native size, then local Real-ESRGAN 2k")
        self._set("render_resolution", "2k")
        eff = autorun._effective(self.cfg, self.pid)
        self.assertEqual(autorun._upscale_text(eff), "upscale 2")

    def test_manual_action_records_a_done_step_when_nothing_is_missing(self):
        (studio.prod_dir(self.cfg, self.pid) / "images" / "S01_01.png") \
            .write_bytes(b"\x89PNG" + b"y" * 40)
        with mock.patch.object(studio, "upscale_images_locally",
                               side_effect=lambda *a, **k: {
                                   "engine": "flowbatch", "tier": "2k",
                                   "total": 3, "upscaled": 2,
                                   "skipped": 1, "failed": 0}):
            result = autorun.upscale_images(self.cfg, self.pid)
        self.assertEqual(result["upscaled"], 2)
        steps = self._image_steps()
        self.assertEqual(steps[0]["status"], "done")
        self.assertIn("local upscale to 2k", steps[0]["detail"])

    def test_manual_action_is_failed_when_images_are_still_missing(self):
        with mock.patch.object(studio, "upscale_images_locally",
                               side_effect=lambda *a, **k: {
                                   "engine": "flowbatch", "tier": "2k",
                                   "total": 1, "upscaled": 1,
                                   "skipped": 0, "failed": 0}):
            autorun.upscale_images(self.cfg, self.pid)
        steps = self._image_steps()
        self.assertEqual(steps[0]["status"], "failed")
        self.assertIn("still missing", steps[0]["detail"])


if __name__ == "__main__":
    unittest.main()
