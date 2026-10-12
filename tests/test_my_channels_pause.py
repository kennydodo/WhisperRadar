"""My Channels: Active/Paused tabs and pause/activate (one row or ticked rows)."""
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class MyChannelsPauseTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(tempfile.mkdtemp()) / "wr.db"
        self.cfg.studio_dir = Path(self.cfg.db_path).parent / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        self.ids = {n: db.create_own_channel(self.conn, n) for n in ("Alpha", "Bravo", "Charlie")}
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()

    def _active(self, name):
        return db.get_own_channel(self.conn, self.ids[name])["active"]

    def test_tabs_split_active_and_paused(self):
        db.update_own_channel(self.conn, self.ids["Bravo"], active=0)
        active = self.client.get("/my-channels").get_data(as_text=True)
        self.assertIn("Active (2)", active)
        self.assertIn("Paused (1)", active)
        self.assertIn("Alpha", active)
        self.assertNotIn("Bravo", active.split('id="own-bulk"')[1])
        paused = self.client.get("/my-channels?state=paused").get_data(as_text=True)
        self.assertIn("Bravo", paused.split('id="own-bulk"')[1])
        self.assertNotIn("Charlie", paused.split('id="own-bulk"')[1])
        self.assertIn("Activate selected", paused)

    def test_single_row_pause_and_activate(self):
        r = self.client.post("/my-channels/state", data={"id": self.ids["Alpha"], "active": "0"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._active("Alpha"), 0)
        r = self.client.post("/my-channels/state",
                             data={"id": self.ids["Alpha"], "active": "1", "state": "paused"})
        self.assertTrue(r.headers["Location"].startswith("/my-channels?state=paused&msg="))
        self.assertEqual(self._active("Alpha"), 1)

    def test_bulk_pause_ticked_rows_only(self):
        r = self.client.post("/my-channels/state",
                             data={"id": [self.ids["Alpha"], self.ids["Charlie"]], "active": "0"})
        self.assertIn("2%20channels%20paused", r.headers["Location"])
        self.assertEqual(self._active("Alpha"), 0)
        self.assertEqual(self._active("Charlie"), 0)
        self.assertEqual(self._active("Bravo"), 1)

    def test_nothing_ticked_changes_nothing(self):
        r = self.client.post("/my-channels/state", data={"active": "0"})
        self.assertIn("error=", r.headers["Location"])
        self.assertEqual(self._active("Alpha"), 1)

    def test_paused_channel_leaves_studio_picker(self):
        db.update_own_channel(self.conn, self.ids["Bravo"], active=0)
        html = self.client.get("/studio").get_data(as_text=True)
        self.assertNotIn(">Bravo<", html)


if __name__ == "__main__":
    unittest.main()
