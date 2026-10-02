"""The planning brief tells the model to keep image prompts inside the image
generator's content policy (Flow refuses violating prompts, leaving holes)."""
import unittest

from whisperradar import briefs


class BriefContentPolicyTests(unittest.TestCase):
    def setUp(self):
        self.text = briefs.render_brief(briefs.read_template())

    def test_policy_section_present(self):
        self.assertIn("IMAGE CONTENT POLICY", self.text)
        for topic in ("Real, named people", "gore", "Nudity", "Self-harm",
                      "brands, logos"):
            self.assertIn(topic, self.text)

    def test_checklist_item_present(self):
        self.assertIn("Content policy (Section 11A)", self.text)

    def test_policy_precedes_checklist(self):
        self.assertLess(self.text.index("SECTION 11A"),
                        self.text.index("SECTION 12"))


if __name__ == "__main__":
    unittest.main()
