"""Render resolution presets: Flow native 1376x768 is SELECTABLE, not default.

A channel/production can pick "Flow native" so a non-upscaled run assembles at
Flow's own master size; the default stays "2k"."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db, settings, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402


class RenderResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.chan = db.create_own_channel(conn, "Ch",
                                          render_resolution="flow-native")
        self.pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, self.pid, own_channel_id=self.chan)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_flow_native_is_selectable(self):
        self.assertEqual(studio.RENDER_RESOLUTIONS["flow-native"], (1376, 768))
        self.assertEqual(studio.RENDER_RESOLUTION_LABELS["flow-native"],
                         "1376x768 (Flow native)")

    def test_flow_native_is_not_the_default(self):
        spec = next(e for e in settings.SPEC if e["key"] == "render_resolution")
        self.assertEqual(spec["default"], "2k")
        self.assertIn("flow-native", spec["choices"])

    def test_selecting_it_writes_the_native_size(self):
        studio.apply_render_resolution(self.cfg, self.pid)
        opts = json.loads(
            (studio.prod_dir(self.cfg, self.pid) / "imgtovideo.json")
            .read_text(encoding="utf-8"))
        self.assertEqual((opts["output"]["width"], opts["output"]["height"]),
                         (1376, 768))

    def test_flow_native_forces_no_upscale(self):
        from whisperradar import autorun
        self.assertEqual(
            autorun._upscale_for({"render_resolution": "flow-native",
                                  "upscale": 3}), 0)
        # any other resolution keeps the channel's upscale
        self.assertEqual(
            autorun._upscale_for({"render_resolution": "2k", "upscale": 3}), 3)


if __name__ == "__main__":
    unittest.main()
