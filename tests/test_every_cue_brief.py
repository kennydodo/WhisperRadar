"""The "Every cue" brief preset: one shot and one new image per cue, applied to the
standard template at render time so the main brief is untouched."""
import unittest
from pathlib import Path

from whisperradar import briefs, studio

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "whisperradar" / "brief_template.md").read_text(encoding="utf-8")


def _cues(n):
    return [{"index": i, "start": f"00:00:{(i - 1) * 3:02d},000", "end": f"00:00:{i * 3:02d},000",
             "text": f"cue {i}"} for i in range(1, n + 1)]


def _shots(spec):
    return {"shots": [{"cues": c, "asset": a, "scene": "S01", "motion": "ZI"} for c, a in spec]}


class BriefTests(unittest.TestCase):
    def test_standard_brief_is_unchanged_and_every_cue_replaces_the_grouping_rules(self):
        std = briefs.render_brief(TEMPLATE, briefs.STANDARD)
        ec = briefs.render_brief(TEMPLATE, briefs.EVERY_CUE)
        self.assertIn("One cue = one image is failure", std)
        self.assertNotIn("ONE CUE = ONE IMAGE", std)
        self.assertIn("SECTION 2 — ONE CUE = ONE IMAGE", ec)
        self.assertNotIn("One cue = one image is failure", ec)
        self.assertIn("exactly as many shots as cues", ec)
        self.assertNotIn("{{", ec)
        # the rest of the brief (output format, style, motion) is still there
        self.assertIn("### shotlist.json", ec)
        self.assertIn("SECTION 12", ec)

    def test_preset_is_selectable_and_only_it_is_per_cue(self):
        self.assertIs(briefs.get_profile("every_cue"), briefs.EVERY_CUE)
        self.assertTrue(briefs.EVERY_CUE.per_cue)
        self.assertFalse(any(p.per_cue for k, p in briefs.MOTION_PRESETS.items() if k != "every_cue"))

    def test_pacing_note_names_the_cue_count(self):
        note = briefs.pacing_note(briefs.EVERY_CUE, 360, 120, 12, 0.8)
        self.assertIn("exactly 120 shots", note)


class GateTests(unittest.TestCase):
    def test_one_shot_per_cue_passes_and_fragmentation_is_not_a_fault(self):
        cues = _cues(8)
        data = _shots([(str(i), f"S01_{i:02d}_SCN_ZI.png") for i in range(1, 9)])
        faults, _ = studio.shotlist_pacing(data, cues, 12.0, briefs.EVERY_CUE)
        self.assertEqual([], [f for f in faults if "cue" in f.lower() and "fragment" in f.lower()])
        self.assertFalse(any("span several cues" in f or "shots for" in f for f in faults))

    def test_the_standard_profile_still_calls_it_fragmentation(self):
        cues = _cues(8)
        data = _shots([(str(i), f"S01_{i:02d}_SCN_ZI.png") for i in range(1, 9)])
        faults, _ = studio.shotlist_pacing(data, cues, 12.0, briefs.STANDARD)
        self.assertTrue(any("fragmentation" in f for f in faults))

    def test_grouped_shots_and_reused_images_are_faults(self):
        cues = _cues(6)
        data = _shots([("1-2", "A.png"), ("3", "B.png"), ("4", "B.png"), ("5", "C.png"), ("6", "D.png")])
        faults, _ = studio.shotlist_pacing(data, cues, 12.0, briefs.EVERY_CUE)
        text = "\n".join(faults)
        self.assertIn("span several cues", text)
        self.assertIn("5 shots for 6 cues", text)
        self.assertIn("used by more than one shot", text)


if __name__ == "__main__":
    unittest.main()
