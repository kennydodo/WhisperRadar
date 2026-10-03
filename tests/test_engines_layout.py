"""The engines live inside the repo: config defaults and the API start-up."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import services  # noqa: E402
from whisperradar.config import load_config  # noqa: E402


class EnginesLayout(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")

    def test_defaults_point_inside_the_repo(self):
        self.assertEqual(Path(self.cfg.flowbatch_repo),
                         ROOT / "engines" / "flowbatch")
        self.assertEqual(Path(self.cfg.renderly_api_dir),
                         ROOT / "engines" / "renderly-api")

    def test_engine_folders_are_in_the_repo(self):
        self.assertTrue((Path(self.cfg.flowbatch_repo) / "package.json").exists())
        self.assertTrue((Path(self.cfg.renderly_api_dir) / "main.py").exists())

    def test_renderly_dir_is_the_api_folder(self):
        self.assertEqual(services._renderly_dir(self.cfg),
                         Path(self.cfg.renderly_api_dir))

    def test_renderly_dir_none_when_missing(self):
        self.cfg.renderly_api_dir = tempfile.mkdtemp()
        self.assertIsNone(services._renderly_dir(self.cfg))

    def _start(self, up_after_spawn=True):
        mgr = services.ServiceManager()
        msgs = []
        with mock.patch.object(mgr, "_spawn") as spawn, \
                mock.patch.object(services, "_up",
                                  return_value=up_after_spawn), \
                mock.patch.object(services, "READY_TIMEOUT", 1):
            ok = mgr._start_renderly(self.cfg, msgs.append)
        return ok, spawn, msgs

    def test_spawns_uvicorn_in_the_api_folder(self):
        root = Path(tempfile.mkdtemp())
        (root / "main.py").write_text("")
        self.cfg.renderly_api_dir = str(root)
        with mock.patch("importlib.util.find_spec", return_value=object()):
            ok, spawn, _ = self._start()
        self.assertTrue(ok)
        name, cmd, cwd = spawn.call_args[0][:3]
        self.assertEqual(name, "renderly")
        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(cmd[1:4], ["-m", "uvicorn", "main:app"])
        self.assertEqual(cwd, root)

    def test_refuses_with_a_hint_when_packages_are_missing(self):
        root = Path(tempfile.mkdtemp())
        (root / "main.py").write_text("")
        self.cfg.renderly_api_dir = str(root)
        with mock.patch("importlib.util.find_spec", return_value=None):
            ok, spawn, msgs = self._start()
        self.assertFalse(ok)
        spawn.assert_not_called()
        self.assertTrue(any("setup.cmd" in m for m in msgs))


if __name__ == "__main__":
    unittest.main()
