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


if __name__ == "__main__":
    unittest.main()
