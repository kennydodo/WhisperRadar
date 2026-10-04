"""The "Run till finish" popup must work on every stage page.

Its script called currentEngine(), which only the images stage defines, so on
any other stage (shots, refs, ...) the popup threw a ReferenceError as soon as
the plan had images to render and never opened - Run till finish did nothing.

Run: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class AutorunPopupEngineTests(unittest.TestCase):
    def test_popup_does_not_depend_on_the_images_stage_function(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(ROOT / "config.yaml")
            cfg.db_path = Path(tmp) / "wr.db"
            cfg.studio_dir = Path(tmp) / "studio"
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            chan = db.create_own_channel(conn, "Chan")
            pid = db.create_production(conn, "P", "general", None, None)
            db.update_production(conn, pid, own_channel_id=chan)
            conn.close()
            client = create_app(cfg).test_client()
            for stage in ("style", "shots", "refs"):
                html = client.get(f"/studio/{pid}?stage={stage}").get_data(
                    as_text=True)
                self.assertIn("autorunmodal", html, stage)
                self.assertNotIn("currentEngine", html, stage)
                self.assertIn("arEngine()", html, stage)
            html = client.get(f"/studio/{pid}?stage=images").get_data(
                as_text=True)
            self.assertIn("arEngine()", html)


if __name__ == "__main__":
    unittest.main()
