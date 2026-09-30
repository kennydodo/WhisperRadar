"""The /studio/pick-folder endpoint (server-side OS folder chooser).

The real dialog blocks until the user interacts, so these tests mock
studio.pick_folder to check the endpoint wiring/JSON, not the dialog itself.

Run: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class PickFolderTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(tempfile.mkdtemp()) / "wr.db"
        self.app = create_app(self.cfg)
        self.app.testing = True
        self.client = self.app.test_client()
        self._orig = studio.pick_folder

    def tearDown(self):
        studio.pick_folder = self._orig

    def test_returns_the_chosen_path(self):
        studio.pick_folder = lambda initial=None: r"D:\VideoProjects\my-video"
        resp = self.client.post("/studio/pick-folder", data={"initial": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(),
                         {"path": r"D:\VideoProjects\my-video"})

    def test_forwards_the_initial_dir(self):
        seen = {}

        def fake(initial=None):
            seen["initial"] = initial
            return None

        studio.pick_folder = fake
        self.client.post("/studio/pick-folder",
                         data={"initial": r"D:\VideoProjects"})
        self.assertEqual(seen["initial"], r"D:\VideoProjects")

    def test_cancel_returns_null(self):
        studio.pick_folder = lambda initial=None: None
        resp = self.client.post("/studio/pick-folder")
        self.assertEqual(resp.get_json(), {"path": None})

    def test_no_gui_returns_an_error(self):
        def boom(initial=None):
            raise RuntimeError("no desktop session")

        studio.pick_folder = boom
        resp = self.client.post("/studio/pick-folder")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(),
                         {"path": None, "error": "no desktop session"})


class ChannelFolderPickerTests(unittest.TestCase):
    """The channel settings page (bible folder / refs folder) must offer the
    same native picker the working-folder fields already do - a browser page
    cannot read a real filesystem path itself, so the channel form fields
    need it just as much as the production form did."""

    def setUp(self):
        from whisperradar import db

        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(tempfile.mkdtemp()) / "wr.db"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.chan = db.create_own_channel(conn, "Test Channel")
        conn.commit()
        conn.close()
        self.app = create_app(self.cfg)
        self.app.testing = True
        self.client = self.app.test_client()

    def test_bible_and_refs_folder_fields_have_browse_buttons(self):
        html = self.client.get("/my-channels").get_data(as_text=True)
        self.assertIn(f'id="bible-dir-{self.chan}"', html)
        self.assertIn(f'id="refs-dir-{self.chan}"', html)
        self.assertIn(f"pickFolder('bible-dir-{self.chan}')", html)
        self.assertIn(f"pickFolder('refs-dir-{self.chan}')", html)
        # the page defines its own copy of pickFolder() - it is a standalone
        # template, not one that inherits studio.html's script block
        self.assertIn("function pickFolder", html)
        self.assertIn("/studio/pick-folder", html)


if __name__ == "__main__":
    unittest.main()
