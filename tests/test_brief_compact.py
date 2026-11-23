"""The compact planning brief: same slots and anchors as the standard one,
every motion profile renders, and it is much shorter. Selected with
studio.manifest_brief in config.yaml; the standard template is untouched."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import briefs, skeleton  # noqa: E402

COMPACT = ROOT / "whisperradar" / "brief_template_compact.md"
SLOT = re.compile(r"\{\{([A-Z_]+)\}\}")


class CompactBriefTests(unittest.TestCase):
    def setUp(self):
        self.old = briefs.read_template()
        self.new = briefs.read_template(COMPACT)

    def profiles(self):
        return [v for v in vars(briefs).values() if isinstance(v, briefs.MotionProfile)]

    def test_it_has_the_same_slots(self):
        self.assertEqual(set(SLOT.findall(self.new)), set(SLOT.findall(self.old)))

    def test_every_profile_renders_and_is_at_least_a_third_shorter(self):
        self.assertGreaterEqual(len(self.profiles()), 8)
        for p in self.profiles():
            with self.subTest(profile=p.key):
                old = briefs.render_brief(self.old.strip(), p, "A host opens the video.")
                new = briefs.render_brief(self.new.strip(), p, "A host opens the video.")
                self.assertNotRegex(new, SLOT)
                self.assertIn("A host opens the video.", new)
                self.assertLess(len(new), len(old) * 0.67)

    def test_the_anchors_other_code_relies_on_are_there(self):
        self.assertNotIn("\n---\n", self.new)                 # a '---' line would cut the brief (load_manifest_brief)
        for head in ("## SECTION 1 ", "## SECTION 5 — DESIGN VISUALS THAT EXPLAIN", "## SECTION 8 "):
            self.assertIn(head, self.new)
        self.assertRegex(self.new, r"(?m)^2\. Grouping by visual idea")     # the every-cue brief rewrites this line
        for name in ("8", "11A", "11B", "11C"):                              # the skeleton planner quotes these
            self.assertGreater(len(skeleton._section(name, self.new)), 200)
        self.assertTrue(skeleton._ref_subject_rule(self.new))

    def test_no_rule_was_dropped(self):
        must = ["BIBLE GATE", "covered exactly once", "last shot ends at the final cue", "1800 CHARACTERS",
                "2400 CHARACTERS", "100 invented refs", "^(CH|BG|OBJ)_[A-Z0-9]+(_[A-Z0-9]+)*$", "never more than **10**",
                "first name is the dominant subject", "INF and PROC diagrams carry none", "must be cast",
                "One cue = one image is a failure", "a number, statistic or price arrives", "numerical coverage is REQUIRED",
                "Never rename or renumber", "Never end mid-token", "Real, named people", "Real brands, logos",
                "VISIBLE gesture", "eyes, mouth, brow, posture", "CROSSFADE", "never on the last shot",
                "No style words", "`shots` first, `images` second"]
        for phrase in must:
            self.assertIn(phrase, self.new, phrase)

    def test_the_reveal_and_overlay_blocks_still_land_before_section_8(self):
        p = next(p for p in self.profiles() if p.key == "standard")
        from dataclasses import replace
        text = briefs.render_brief(self.new.strip(), replace(p, reveal=True, text_overlays=True))
        self.assertLess(text.index("SECTION 7B"), text.index("## SECTION 8 "))
        self.assertLess(text.index("SECTION 7C"), text.index("## SECTION 8 "))


if __name__ == "__main__":
    unittest.main()
