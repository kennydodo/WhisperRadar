"""The shotlist gate's reference rules (no LLM, no filesystem).

The rule added today: a shotlist that declares SUPPLIED references (registry
entries with a path) but attaches none of them is a failure - the brief calls a
plan that never features a supplied character a failure, and the refs stage
would copy the files for nothing.

Run: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import studio  # noqa: E402


def _shotlist(refs=None, image_refs=None, images=2):
    imgs, shots = [], []
    for i in range(images):
        name = f"S01_{i + 1:02d}_SCN_PR.png"
        entry = {"file": name, "prompt": f"a detailed prompt for shot number {i}"}
        if image_refs and i < len(image_refs) and image_refs[i]:
            entry["refs"] = image_refs[i]
        imgs.append(entry)
        shots.append({"cues": str(i + 1), "asset": name, "scene": "S01",
                      "motion": "PR"})
    data = {"shots": shots, "images": imgs}
    if refs is not None:
        data["refs"] = refs
    return data


class SuppliedRefsTests(unittest.TestCase):
    def test_declared_but_unused_supplied_refs_are_a_fault(self):
        data = _shotlist(refs={"MAYA": "refs/MAYA.jpeg"},
                         image_refs=[None, None])
        faults = studio.shotlist_structural_faults(data, 2)
        self.assertTrue(any("supplied reference" in f for f in faults), faults)

    def test_using_one_supplied_ref_is_enough(self):
        data = _shotlist(refs={"MAYA": "refs/MAYA.jpeg"},
                         image_refs=[["MAYA"], None])
        faults = studio.shotlist_structural_faults(data, 2)
        self.assertFalse(any("supplied reference" in f for f in faults), faults)

    def test_a_plan_with_no_refs_is_fine(self):
        data = _shotlist(refs={}, image_refs=[None, None])
        faults = studio.shotlist_structural_faults(data, 2)
        self.assertFalse(any("reference" in f for f in faults), faults)

    def test_an_undeclared_used_ref_is_still_a_fault(self):
        data = _shotlist(refs={}, image_refs=[["MAYA"], None])
        faults = studio.shotlist_structural_faults(data, 2)
        self.assertTrue(any("not declared" in f for f in faults), faults)


class ParseTests(unittest.TestCase):
    def test_a_raw_control_character_in_a_prompt_still_parses(self):
        # the planner sometimes emits a literal newline inside a prompt string;
        # json rejects that by default, which would fail the whole plan
        text = ('{"shots": [{"cues": "1", "asset": "S01_01_SCN_ZI.png", '
                '"scene": "S01", "motion": "ZI"}], "images": [{"file": '
                '"S01_01_SCN_ZI.png", "prompt": "line one\nline two"}]}')
        data, _ = studio.parse_shotlist_output(text)
        self.assertEqual(len(data["images"]), 1)

    def test_a_trailing_comma_still_parses(self):
        # a long plan occasionally slips one (a dropped "refs" leaves `"}",`)
        text = ('{"shots": [{"cues": "1", "asset": "S01_01_SCN_ZI.png", '
                '"scene": "S01", "motion": "ZI"}], "images": [{"file": '
                '"S01_01_SCN_ZI.png", "prompt": "a scene", }, ]}')
        data, _ = studio.parse_shotlist_output(text)
        self.assertEqual(len(data["images"]), 1)


if __name__ == "__main__":
    unittest.main()
