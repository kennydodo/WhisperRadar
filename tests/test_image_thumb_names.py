"""Images stage: hovering a thumbnail shows the image's file name."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class ImageThumbNameTests(unittest.TestCase):
    def test_every_thumbnail_carries_its_file_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(ROOT / "config.yaml")
            cfg.db_path = Path(tmp) / "wr.db"
            cfg.studio_dir = Path(tmp) / "studio"
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            chan = db.create_own_channel(conn, "Ch")
            pid = db.create_production(conn, "P", "general", None, None)
            db.update_production(conn, pid, own_channel_id=chan, stage="images")
            conn.commit()
            conn.close()
            img = studio.prod_dir(cfg, pid) / "images"
            img.mkdir(parents=True, exist_ok=True)
            for name in ("S01_01_SCN_ZI.png", "S01_02_CU_ZO.png"):
                (img / name).write_bytes(b"x")
            html = create_app(cfg).test_client().get(
                f"/studio/{pid}?stage=images").get_data(as_text=True)
            for name in ("S01_01_SCN_ZI", "S01_02_CU_ZO"):  # no extension
                self.assertIn(f'class="thumb" title="{name}"', html)
                self.assertIn(f'onclick="wrCopyName(this)">{name}</span>', html)


if __name__ == "__main__":
    unittest.main()
