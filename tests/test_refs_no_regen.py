"""A reference image that already exists in refs\\ is never generated again.

A re-plan can blank the shotlist registry entry of a ref generated earlier.
The refs stage then saw "no usable file + has a prompt" and paid for the same
image a second time (and uploaded a duplicate into the Flow project).

Run: python -m unittest discover -s tests
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402


class RefsNotRegenerated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pdir = Path(self.tmp)
        (self.pdir / "refs").mkdir()
        self.write_shotlist({"CH_MAYA": None, "BG_KITCHEN": None})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_shotlist(self, registry):
        (self.pdir / "shotlist.json").write_text(json.dumps({
            "images": [{"file": "S01_01.png", "prompt": "x",
                        "refs": ["CH_MAYA", "BG_KITCHEN"]}],
            "refs": registry,
            "refPrompts": {"CH_MAYA": "a woman", "BG_KITCHEN": "a kitchen"},
        }), encoding="utf-8")

    def registry(self):
        return json.loads((self.pdir / "shotlist.json")
                          .read_text(encoding="utf-8"))["refs"]

    def test_both_missing_are_generated(self):
        self.assertEqual(set(studio.refs_to_generate(self.pdir)),
                         {"CH_MAYA", "BG_KITCHEN"})

    def test_a_file_in_refs_is_not_generated_even_with_a_blank_registry(self):
        (self.pdir / "refs" / "CH_MAYA.png").write_bytes(b"png")
        self.assertEqual(set(studio.refs_to_generate(self.pdir)),
                         {"BG_KITCHEN"})

    def test_any_image_extension_and_case_counts(self):
        (self.pdir / "refs" / "ch_maya.JPG").write_bytes(b"jpg")
        self.assertNotIn("CH_MAYA", studio.refs_to_generate(self.pdir))

    def test_adopt_points_the_registry_at_the_file(self):
        (self.pdir / "refs" / "CH_MAYA.jpg").write_bytes(b"jpg")
        self.assertEqual(studio.adopt_disk_refs(self.pdir), ["CH_MAYA"])
        self.assertEqual(self.registry()["CH_MAYA"], "refs/CH_MAYA.jpg")
        self.assertIsNone(self.registry()["BG_KITCHEN"])
        self.assertTrue(studio.shotlist_refs(self.pdir)["CH_MAYA"]["provided"])
        # nothing left to adopt the second time
        self.assertEqual(studio.adopt_disk_refs(self.pdir), [])

    def test_adopt_repairs_a_registry_path_whose_file_is_gone(self):
        self.write_shotlist({"CH_MAYA": "refs/old/CH_MAYA.png",
                             "BG_KITCHEN": None})
        (self.pdir / "refs" / "CH_MAYA.png").write_bytes(b"png")
        self.assertEqual(studio.adopt_disk_refs(self.pdir), ["CH_MAYA"])
        self.assertEqual(self.registry()["CH_MAYA"], "refs/CH_MAYA.png")

    def test_deleting_the_file_makes_it_generatable_again(self):
        f = self.pdir / "refs" / "CH_MAYA.png"
        f.write_bytes(b"png")
        self.assertNotIn("CH_MAYA", studio.refs_to_generate(self.pdir))
        f.unlink()
        self.assertIn("CH_MAYA", studio.refs_to_generate(self.pdir))

    def test_the_engines_refuse_to_regenerate_a_ref_handed_to_them(self):
        """Even a caller that passes a stale list pays for nothing."""
        (self.pdir / "refs" / "CH_MAYA.png").write_bytes(b"png")
        refs = {"CH_MAYA": {"prompt": "a woman"}}
        cfg = load_config(ROOT / "config.yaml")
        logs = []
        with mock.patch.object(studio, "flowbatch_ready",
                               side_effect=AssertionError("must not start")):
            out = studio.run_flowbatch_refs(cfg, self.pdir, 1, refs,
                                            log=logs.append)
        self.assertEqual(out, {"generated": [], "missing": []})
        self.assertTrue(any("not generated again" in m for m in logs))
        with mock.patch.object(studio, "run_imagegen",
                               side_effect=AssertionError("must not render")):
            out = studio.run_renderly_refs(cfg, self.pdir, 1, refs,
                                           log=logs.append)
        self.assertEqual(out, {"generated": [], "missing": []})


if __name__ == "__main__":
    unittest.main()
