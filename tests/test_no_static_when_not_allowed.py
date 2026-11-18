"""A channel set to zoom codes only (ZI/ZO) is never asked for, or required to
use, the static ST code - not by the brief, the examples, the judge or the
reveal feature."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import briefs, external_prompts, skeleton  # noqa: E402

PROFILES = {
    "zooms_only preset": lambda: briefs.get_profile("zooms_only"),
    "custom ZI/ZO": lambda: briefs.custom_profile({"allowed": ["ZI", "ZO"]}),
    "custom ZO": lambda: briefs.custom_profile({"allowed": ["ZO"]}),
}


class NoStaticTests(unittest.TestCase):
    def test_briefs_only_mention_st_to_forbid_it(self):
        for name in ("brief_template.md", "brief_template_compact.md"):
            text = (ROOT / "whisperradar" / name).read_text(encoding="utf-8")
            for label, make in PROFILES.items():
                with self.subTest(template=name, profile=label):
                    out = briefs.render_brief(text.strip(), make(), "A host opens the video.")
                    for line in out.split("\n"):
                        if re.search(r"\bST\b|_ST\b|static", line, re.I):
                            self.assertRegex(
                                line, r"(?i)never|no static|not allowed|outside this list|no ST|only (ZI|ZO)",
                                line[:160])
                    self.assertNotIn("_ST.png", out)

    def test_examples_use_an_allowed_code(self):
        text = (ROOT / "whisperradar" / "brief_template.md").read_text(encoding="utf-8")
        out = briefs.render_brief(text.strip(), briefs.get_profile("zooms_only"), "x")
        self.assertIn("S04_03_INF_ZO.png", out)

    def test_judge_rules_do_not_cap_or_require_st(self):
        for label, make in PROFILES.items():
            prof = make()
            self.assertIsNone(prof.st_max_share, label)
            self.assertIsNone(prof.st_max_hold, label)
            self.assertIsNone(prof.static_long_hold, label)
            self.assertNotIn("ST", prof.allowed)

    def test_reveal_shots_need_st_so_they_are_off_without_it(self):
        prof = briefs.resolve_profile("zooms_only", reveal=2)
        self.assertFalse(prof.reveal)
        self.assertFalse(prof.reveal_strict)
        keep = briefs.resolve_profile("flow_safe", reveal=1)      # has ST
        self.assertTrue(keep.reveal)

    def test_planner_only_assigns_allowed_codes(self):
        for label, make in PROFILES.items():
            prof = make()
            cues = [{"index": i + 1, "start": f"00:00:{i * 3:02d},000",
                     "end": f"00:00:{i * 3 + 3:02d},000", "text": f"cue {i}"} for i in range(24)]
            shots = skeleton.build(cues, prof, prof.max_hold or 12.0)
            self.assertTrue(all(s.motion in prof.allowed for s in shots), label)


if __name__ == "__main__":
    unittest.main()
