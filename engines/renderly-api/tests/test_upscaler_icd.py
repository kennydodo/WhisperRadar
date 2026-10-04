"""Vulkan ICD selection: only ICDs whose driver exists are forced into the loader."""
import json
import tempfile
import unittest
from pathlib import Path

import base  # noqa: F401  (sys.path + isolated DATABASE_URL)
from services import upscaler


class UsableIcdFiles(unittest.TestCase):
    def test_keeps_only_icds_whose_driver_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            driver = tmp_path / "driver.dll"
            driver.write_text("")
            (tmp_path / "nv-vk64.json").write_text(
                json.dumps({"ICD": {"library_path": str(driver)}})
            )
            (tmp_path / "igvk64.json").write_text(
                json.dumps({"ICD": {"library_path": str(tmp_path / "absent.dll")}})
            )
            (tmp_path / "device_cache.json").write_text(json.dumps({"device": 0}))

            names = [Path(f).name for f in upscaler._usable_icd_files(tmp_path)]
            self.assertEqual(names, ["nv-vk64.json"])

    def test_unreadable_icds_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "broken.json").write_text("{ not json")
            (tmp_path / "empty.json").write_text(json.dumps({}))
            self.assertEqual(upscaler._usable_icd_files(tmp_path), [])

    def test_no_env_override_when_no_icd_is_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "nv-vk64.json").write_text(
                json.dumps({"ICD": {"library_path": str(tmp_path / "nope.dll")}})
            )
            self.assertEqual(upscaler._usable_icd_files(tmp_path), [])

    def test_shipped_icds_are_either_installed_or_skipped(self):
        for file in upscaler._usable_icd_files():
            library = json.loads(Path(file).read_text(encoding="utf-8"))["ICD"]["library_path"]
            self.assertTrue(Path(library).exists(), file)


if __name__ == "__main__":
    unittest.main()
