"""Simple mode (Settings): only HIDES things - the script page keeps the web
chat, the external-LLM prompts, target words and additional direction; the
beats plan is hidden (the judge shotlist stays); Auto Run's merge renders the
final video instead of exporting a Premiere project."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import autorun, db, settings, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

HIDDEN_ON_SCRIPT = ("Use an API LLM for the script", "GPT-Claude Script Review",
                    "Notes prompt", "Save as research notes", "Save notes")
KEPT_ON_SCRIPT = ("Run script in web chat", "Use an external LLM (ChatGPT / Claude) for the script",
                  "Target words", "Writer prompt", "Judge prompt",
                  "Additional direction for the LLM")


class SimpleModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        self.pid = db.create_production(self.conn, "Simple One", "general", None, None)
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def set_mode(self, on):
        settings.save(self.conn, {"simple_mode": "1" if on else ""})

    def page(self, stage):
        return self.client.get(f"/studio/{self.pid}?stage={stage}").get_data(as_text=True)

    def test_off_by_default_and_everything_shows(self):
        self.assertFalse(settings.load(self.conn)["simple_mode"])
        text = self.page("script")
        for s in HIDDEN_ON_SCRIPT + KEPT_ON_SCRIPT:
            self.assertIn(s, text, s)
        shots = self.page("shots")
        self.assertIn("Plan from the script (beats)", shots)
        self.assertIn("Or the old way", shots)
        self.assertIn("Use an API LLM for the shotlist", shots)
        for there in ("Visual style (how the images look)", "Character / reference bible", "Image prompts"):
            self.assertIn(there, shots, there)

    def test_script_page_in_simple_mode(self):
        self.set_mode(True)
        self.assertTrue(settings.load(self.conn)["simple_mode"])
        text = self.page("script")
        for s in HIDDEN_ON_SCRIPT:
            self.assertNotIn(s, text, s)
        for s in KEPT_ON_SCRIPT:
            self.assertIn(s, text, s)
        # the field the prompt buttons read stays in the page (hidden), so
        # they keep working
        self.assertIn('data-f="notes"', text)

    def test_shots_page_keeps_only_the_judge_way(self):
        self.set_mode(True)
        text = self.page("shots")
        self.assertNotIn("Plan from the script (beats)", text)
        self.assertNotIn("Or the old way", text)
        self.assertNotIn("Use an API LLM for the shotlist", text)
        self.assertNotIn("Save notes", text)
        for gone in ("Visual style (how the images look)", "Character / reference bible",
                     "Image prompts", "Upload bible file", "Extract prompts from shotlist"):
            self.assertNotIn(gone, text, gone)
        self.assertIn("Shotlist with a judge", text)
        self.assertIn("/webchat/shots", text)

    def test_settings_page_has_the_switch(self):
        text = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("simple_mode", text)

    def _merge(self, on, mode=None):
        self.set_mode(on)
        pdir = Path(self.tmp.name) / "pd"
        pdir.mkdir(exist_ok=True)
        with mock.patch.object(studio, "prepare_project_folder", return_value=pdir), \
             mock.patch.object(studio, "find_audio", return_value=Path("a.mp3")), \
             mock.patch.object(studio, "find_srt", return_value=Path("a.srt")), \
             mock.patch.object(studio, "find_images", return_value=[Path("x")]), \
             mock.patch.object(autorun, "explainer_production", return_value=False), \
             mock.patch.object(autorun, "_merge_pause_reason", return_value=None), \
             mock.patch.object(autorun, "_effective",
                               return_value={"render_target": "premiere", "animate_enabled": False,
                                             "animate_seconds": None}), \
             mock.patch.object(self.cfg, "studio_merge_command", "", create=True), \
             mock.patch.object(studio, "run_final_render", return_value=Path("f.mp4")) as final, \
             mock.patch.object(studio, "run_merge_render",
                               return_value={"project": "p"}) as nle:
            autorun._run_merge(self.cfg, self.pid, mode)
        return final, nle

    def test_auto_run_merge_renders_the_final_video_in_simple_mode(self):
        final, nle = self._merge(True)
        self.assertTrue(final.called)
        self.assertFalse(nle.called)

    def test_auto_run_merge_is_unchanged_when_simple_mode_is_off(self):
        final, nle = self._merge(False)
        self.assertFalse(final.called)
        self.assertTrue(nle.called)

    def test_the_premiere_export_button_path_still_exports(self):
        final, nle = self._merge(True, mode="cli")
        self.assertTrue(nle.called)
        self.assertFalse(final.called)


class SimpleModeUpscaleTests(unittest.TestCase):
    """Simple mode downloads the stills at Flow's size and upscales them ONCE,
    just before the final render. Normal mode keeps its own behaviour."""

    def eff(self, simple, resolution="2k", upscale=2, native_level="off"):
        return {"simple_mode": simple, "render_resolution": resolution, "upscale": upscale,
                "flow_native_upscale": native_level, "animate_enabled": False,
                "animate_seconds": None, "render_target": "premiere"}

    def test_download_is_native_in_simple_mode_only(self):
        self.assertEqual(autorun._upscale_for(self.eff(True)), 0)
        self.assertEqual(autorun._upscale_for(self.eff(False)), 2)
        self.assertEqual(autorun._upscale_for(self.eff(False, "4k", 4)), 4)

    def test_no_pass_after_the_downloads_in_simple_mode(self):
        flow_native = self.eff(True, "flow-native", 0, "2k")
        self.assertEqual(autorun._download_upscale_tier(flow_native), "off")
        normal = self.eff(False, "flow-native", 0, "2k")
        self.assertEqual(autorun._download_upscale_tier(normal), "2k")
        self.assertIn("upscaled just before", autorun._upscale_text(self.eff(True)))

    def test_pass_runs_before_render_to_the_selected_resolution(self):
        for resolution, upscale, tier in (("1080p", 1, "1k"), ("2k", 2, "2k"), ("4k", 4, "4k")):
            with mock.patch.object(autorun, "_effective",
                                   return_value=self.eff(True, resolution, upscale)), \
                 mock.patch.object(studio, "upscale_images_locally",
                                   return_value={"tier": tier}) as up:
                got = autorun.upscale_before_render(None, 1, Path("p"))
            self.assertEqual(up.call_args.kwargs["tier"], tier)
            self.assertEqual(got, {"tier": tier})

    def test_flow_native_uses_its_own_level_and_no_level_means_no_pass(self):
        with mock.patch.object(autorun, "_effective",
                               return_value=self.eff(True, "flow-native", 0, "2k")), \
             mock.patch.object(studio, "upscale_images_locally", return_value={}) as up:
            autorun.upscale_before_render(None, 1, Path("p"))
        self.assertEqual(up.call_args.kwargs["tier"], "2k")
        with mock.patch.object(autorun, "_effective",
                               return_value=self.eff(True, "flow-native", 0, "off")), \
             mock.patch.object(studio, "upscale_images_locally") as up:
            self.assertIsNone(autorun.upscale_before_render(None, 1, Path("p")))
        self.assertFalse(up.called)

    def test_normal_mode_never_upscales_before_render(self):
        with mock.patch.object(autorun, "_effective", return_value=self.eff(False)), \
             mock.patch.object(studio, "upscale_images_locally") as up:
            self.assertIsNone(autorun.upscale_before_render(None, 1, Path("p")))
        self.assertFalse(up.called)

    def test_auto_run_merge_upscales_then_renders(self):
        calls = []
        pdir = Path(tempfile.mkdtemp())
        with mock.patch.object(studio, "prepare_project_folder", return_value=pdir), \
             mock.patch.object(studio, "find_audio", return_value=Path("a.mp3")), \
             mock.patch.object(studio, "find_srt", return_value=Path("a.srt")), \
             mock.patch.object(studio, "find_images", return_value=[Path("x")]), \
             mock.patch.object(autorun, "explainer_production", return_value=False), \
             mock.patch.object(autorun, "_merge_pause_reason", return_value=None), \
             mock.patch.object(autorun, "_simple_mode", return_value=True), \
             mock.patch.object(autorun, "_effective", return_value=self.eff(True)), \
             mock.patch.object(autorun, "_connect"), \
             mock.patch.object(autorun.db, "add_step"), \
             mock.patch.object(autorun, "upscale_before_render",
                               side_effect=lambda *a, **k: calls.append("upscale")), \
             mock.patch.object(studio, "run_final_render",
                               side_effect=lambda *a, **k: calls.append("render") or Path("f")):
            cfg = mock.Mock(studio_merge_command="")
            autorun._run_merge(cfg, 1, None)
        self.assertEqual(calls, ["upscale", "render"])


class SimpleModeEffTests(unittest.TestCase):
    def test_for_production_exposes_the_setting(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            conn = db.connect(Path(tmp.name) / "w.db")
            db.init_db(conn)
            pid = db.create_production(conn, "T", "general", None, None)
            prod = db.get_production(conn, pid)
            self.assertFalse(settings.for_production(conn, prod)["simple_mode"])
            settings.save(conn, {"simple_mode": "1"})
            self.assertTrue(settings.for_production(conn, prod)["simple_mode"])
            conn.close()
        finally:
            wr_tmp.cleanup(tmp)


if __name__ == "__main__":
    unittest.main()
