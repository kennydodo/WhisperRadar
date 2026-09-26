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


if __name__ == "__main__":
    unittest.main()
