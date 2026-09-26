"""Supplied-reference injection into the shotlist planning prompt.

The planner has no filesystem access, so the files the user put in the
production's refs\\ folder (an uploaded character, seeded refs) are listed in
the prompt - otherwise the planner can only learn about them through bible
conventions and silently plans around a character it never saw.

Run: python -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisperradar import studio  # noqa: E402


class SuppliedRefsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lists_uploaded_files_only(self):
        (self.pdir / "refs").mkdir()
        (self.pdir / "refs" / "MAYA.png").write_bytes(b"x")
        (self.pdir / "refs" / "host.jpg").write_bytes(b"x")
        self.assertEqual(studio.find_supplied_refs(self.pdir),
                         ["MAYA.png", "host.jpg"])

    def test_excludes_generator_output(self):
        (self.pdir / "refs").mkdir()
        (self.pdir / "refs" / "MAYA.png").write_bytes(b"x")
        (self.pdir / "refs" / "BG_KITCHEN.png").write_bytes(b"x")
        (self.pdir / "refs_generated.json").write_text(
            json.dumps(["BG_KITCHEN"]), encoding="utf-8")
        self.assertEqual(studio.find_supplied_refs(self.pdir), ["MAYA.png"])

    def test_no_refs_folder(self):
        self.assertEqual(studio.find_supplied_refs(self.pdir), [])

    def test_prompt_lists_the_exact_paths(self):
        prompt = studio.shotlist_prompt(
            "BRIEF", "1: hello", supplied_refs=["MAYA.png", "host.jpg"])
        self.assertIn("SUPPLIED REFERENCE FILES", prompt)
        self.assertIn("- refs/MAYA.png", prompt)
        self.assertIn("- refs/host.jpg", prompt)
        self.assertIn("EXACTLY the path shown", prompt)

    def test_prompt_without_supplied_refs_has_no_block(self):
        prompt = studio.shotlist_prompt("BRIEF", "1: hello")
        self.assertNotIn("SUPPLIED REFERENCE FILES", prompt)


if __name__ == "__main__":
    unittest.main()
