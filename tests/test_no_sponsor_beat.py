import unittest

from whisperradar import studio


class NoSponsorTests(unittest.TestCase):
    def test_style_analysis_ignores_sponsor_segments(self):
        p = studio.style_prompt("T", "science", "some transcript text here")
        self.assertIn("IGNORE every sponsor read", p)
        self.assertIn("ONLY the closing call to action", p)

    def test_style_from_scratch_has_no_sponsor_beat(self):
        p = studio.style_prompt_from_scratch("T", "science")
        self.assertIn("No sponsor read", p)

    def test_script_writer_never_writes_ads_or_invents_offers(self):
        p = studio.script_prompt(
            "T", "g", "facts",
            "## Structure\n- 35-40%: Mid-roll sponsor CTA (course promotion)")
        self.assertIn("Write NO sponsor read", p)
        self.assertIn("even if the style guide mentions one", p)


if __name__ == "__main__":
    unittest.main()


class PromoCheckTests(unittest.TestCase):
    def test_finds_promotion_at_any_position(self):
        from whisperradar import plan
        self.assertEqual(plan.promo_mentions("Plain narration. Of course it works."), [])
        for text in ("Today's video is sponsored by Acme.",
                     "This video is sponsored by Acme.",
                     "Thanks to Acme for sponsoring this one.",
                     "Our sponsor today is Acme.",
                     "Use code BRAIN20 for 20% off.",
                     "Scan the QR code on screen.",
                     "The link is in the description.",
                     "Join my course and learn more.",
                     "Brought to you by our friends."):
            self.assertTrue(plan.promo_mentions(text), text)

    def test_ordinary_words_are_not_flagged(self):
        from whisperradar import plan
        for text in ("The shrine was sponsored by local merchants.",
                     "Sponsorship deals changed the sport.",
                     "He found a sponsor for the expedition.",
                     "Of course the brain keeps working.",
                     "Subscribe and watch the next video.",
                     "The course of the river changed.",
                     "A rule of thumb for rest."):
            self.assertEqual(plan.promo_mentions(text), [], text)

    def test_judge_prompt_makes_promotion_a_must_fix(self):
        p = studio.rating_prompt("T", "g", "script", "src", "style", 0.0)
        self.assertIn("NO PROMOTION, AT ANY POSITION", p)
        self.assertIn("must_fix", p)


class DropPromoLinesTests(unittest.TestCase):
    def test_sponsor_points_never_reach_the_writer(self):
        from whisperradar import plan
        guide = ("## Structure\n- 0-5%: hook\n"
                 "- 35-40%: Mid-roll sponsor CTA (course promotion)\n"
                 "- 40-100%: payoff\n")
        out = plan.drop_promo_lines(guide)
        self.assertNotIn("sponsor", out.lower())
        self.assertIn("hook", out)
        self.assertIn("payoff", out)
        p = studio.script_prompt("T", "g", "facts", guide)
        self.assertNotIn("Mid-roll sponsor", p)
