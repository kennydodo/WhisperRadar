"""A channel has ONE fixed writing style. It is required when the channel is
created, stage 1 loads it into every production (nothing is generated), and
saving a style in stage 1 writes it to the channel."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import autorun, db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class ChannelWritingStyleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def _prod(self, writing_style=None):
        chan = db.create_own_channel(self.conn, "Calm Habits",
                                     **({"writing_style": writing_style}
                                        if writing_style else {}))
        pid = db.create_production(self.conn, "Morning", "general", None, None)
        db.update_production(self.conn, pid, own_channel_id=chan)
        return chan, pid

    def _channel_style(self, chan):
        return db.get_own_channel(self.conn, chan)["writing_style"]

    def test_new_channel_requires_a_writing_style(self):
        with mock.patch.object(studio, "sync_renderly_channel",
                               return_value={"ok": False, "error": "x"}):
            resp = self.client.post("/my-channels/add", data={"name": "No Style"})
            self.assertIn("writing style", unquote(resp.headers["Location"]).lower())
            self.assertIsNone(db.get_own_channel(self.conn, "No Style"))
            self.client.post("/my-channels/add", data={
                "name": "Has Style", "writing_style": "Warm, slow, direct."})
        self.assertEqual(db.get_own_channel(self.conn, "Has Style")["writing_style"],
                         "Warm, slow, direct.")

    def test_stage_one_loads_the_channel_style_without_an_llm(self):
        _, pid = self._prod("## Voice\ncalm")
        self.assertEqual(autorun.stage_action(self.cfg, pid, "style")["action"],
                         "run")
        with mock.patch.object(studio, "llm_generate") as llm:
            autorun._run_style(self.cfg, pid)
        llm.assert_not_called()
        pdir = studio.prod_dir(self.cfg, pid)
        self.assertEqual(studio.find_writing_style(pdir).read_text("utf-8"),
                         "## Voice\ncalm\n")
        self.assertEqual(autorun.stage_action(self.cfg, pid, "style")["action"],
                         "skip")

    def test_channel_without_a_style_pauses_stage_one(self):
        _, pid = self._prod()
        entry = autorun.stage_action(self.cfg, pid, "style")
        self.assertEqual(entry["action"], "pause")
        with self.assertRaises(autorun._Paused):
            autorun._run_style(self.cfg, pid)

    def test_new_productions_are_seeded_from_the_channel_style(self):
        _, pid = self._prod("Seeded style")
        res = studio.seed_production(self.cfg, self.conn,
                                     db.get_production(self.conn, pid))
        self.assertTrue(res["writing_style"])
        self.assertEqual(studio.find_writing_style(
            studio.prod_dir(self.cfg, pid)).read_text("utf-8"), "Seeded style\n")

    def test_saving_in_stage_one_writes_the_channel_style(self):
        chan, pid = self._prod()
        self.client.post(f"/studio/{pid}/style/save", data={"style": "First style"})
        self.assertEqual(self._channel_style(chan), "First style")
        # the override replaces it for this production and the channel
        self.client.post(f"/studio/{pid}/style/save", data={"style": "Newer style"})
        self.assertEqual(self._channel_style(chan), "Newer style")
        self.assertIn("Newer style", studio.find_writing_style(
            studio.prod_dir(self.cfg, pid)).read_text("utf-8"))

    def test_stage_one_page_has_override_and_no_generate_button(self):
        _, pid = self._prod("Saved style")
        autorun._run_style(self.cfg, pid)
        with mock.patch.object(studio, "provider_ready", return_value=True), \
                mock.patch.object(studio, "providers",
                                  return_value=[{"name": "x", "label": "x"}]):
            html = self.client.get(f"/studio/{pid}?stage=style").get_data(
                as_text=True)
        self.assertIn("Override writing style", html)
        self.assertNotIn("/style/generate", html)


if __name__ == "__main__":
    unittest.main()
