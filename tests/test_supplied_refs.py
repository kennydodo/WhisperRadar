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


class ReferencesOffTests(unittest.TestCase):
    """A channel with references off must get NO references - strict."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_prompt_forbids_refs_when_disabled(self):
        prompt = studio.shotlist_prompt("BRIEF", "1: hello",
                                        supplied_refs=["MAYA.png"],
                                        allow_refs=False)
        self.assertIn("REFERENCES ARE DISABLED", prompt)
        self.assertIn("describe every character", prompt.lower())
        # the supplied-refs block must NOT appear when refs are off
        self.assertNotIn("SUPPLIED REFERENCE FILES", prompt)

    def test_uses_refs_detects_registry_prompts_and_attachments(self):
        self.assertFalse(studio.shotlist_uses_refs({"images": [
            {"file": "a.png", "prompt": "x"}]}))
        self.assertTrue(studio.shotlist_uses_refs(
            {"refs": {"OBJ_CAT": None}}))
        self.assertTrue(studio.shotlist_uses_refs(
            {"refPrompts": {"CH_COCO": "a cat"}}))
        self.assertTrue(studio.shotlist_uses_refs({"images": [
            {"file": "a.png", "prompt": "x", "refs": ["OBJ_CAT"]}]}))

    def test_flowbatch_job_drops_a_ref_with_no_file(self):
        # S01_01 attaches OBJ_CAT, but OBJ_CAT was never generated (null path,
        # no file) - the job must not carry it, so the batch cannot stall
        (self.pdir / "shotlist.json").write_text(json.dumps({
            "refs": {"OBJ_CAT": None},
            "refPrompts": {"OBJ_CAT": "a ginger cat"},
            "images": [{"file": "S01_01_SCN_ZI.png", "prompt": "a cat",
                        "refs": ["OBJ_CAT"]}],
        }), encoding="utf-8")
        from whisperradar.config import load_config
        job_path, _ = studio.prepare_flowbatch_job(
            load_config(), self.pdir, 999999)
        job = json.loads(job_path.read_text(encoding="utf-8"))
        self.assertNotIn("refs", job["images"][0])


if __name__ == "__main__":
    unittest.main()
