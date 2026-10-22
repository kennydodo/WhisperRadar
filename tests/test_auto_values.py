"""Automatic on-screen values: numbers, years and amounts found in the cue text."""
import unittest

from tests.test_video_shotlist import IMAGES, do_parse, subtitles  # noqa: F401
from whisperradar.video import auto_values as av
from whisperradar.video import shotlist as sl
from whisperradar.video import models as m
from whisperradar.video.options import TextOptions, TransitionOptions


class FindTests(unittest.TestCase):
    def test_alignment_space_before_a_decimal_point_is_removed(self):
        self.assertEqual(["$3.42 a day"], av.find_values("Coffee costs $3 .42 a day"))
        self.assertEqual(["$102.50"], av.find_values("added $102 .50 on"))
        self.assertEqual(["0.01%"], av.find_values("a ratio of 0 .01 %"))

    def test_alignment_spaces_inside_money_are_removed(self):
        self.assertEqual(["$43,680", "$100,000"], av.find_values("$43 ,680 and $100 ,000"))
    def test_values_as_spoken(self):
        self.assertEqual(["170 kilograms"], av.find_values("Estimated around 170 kilograms, it was big."))
        self.assertEqual(["four times"], av.find_values("The tiger can weigh four times as much."))
        self.assertEqual(["1940", "$240,000", "7%"],
                         av.find_values("In 1940 the Act passed, and $240,000 at 7% a year."))
        self.assertEqual(["$200 a month", "30 years"], av.find_values("Invest $200 a month for 30 years"))
        self.assertEqual(["1990s", "12 million", "1985"],
                         av.find_values("In the 1990s, 12 million people left in 1985."))

    def test_a_spaced_percent_sign_is_tidied(self):
        self.assertEqual(["15%"], av.find_values("It rose 15 % last year."))

    def test_no_values(self):
        self.assertEqual([], av.find_values("A wolf rarely hunts alone, but a pack can."))
        self.assertEqual([], av.find_values("He had 3 dogs."))      # a lone single digit is not a value


class BuildTests(unittest.TestCase):
    def test_decimal_split_across_cues_is_reconstructed(self):
        items = av.build([(0, 2, "it costs $3"), (2, 3, ".42."), (3, 5, "a day")], 0, 5)
        self.assertEqual(items[0]["text"], "$3.42")
        items = av.build([(0, 2, "a ratio of 0"), (2, 3, ".01"), (3, 4, "%"), (4, 6, "is low.")], 0, 6)
        self.assertEqual(items[0]["text"], "0.01%")

    def test_split_amount_and_unit_are_reconstructed(self):
        items = av.build([(0, 3, "A total of $43"), (3, 4, ",680 for 30"),
                          (4, 7, "years.")], 0, 7)
        self.assertEqual(items[0]["text"], "$43,680")
        self.assertEqual(items[1]["text"], "30 years")

    def test_three_values_in_one_cue_are_not_silently_dropped(self):
        items = av.build([(0, 6, "$520 for 84 months is $43,680.")], 0, 6)
        self.assertIn("$520", items[0]["text"])
        self.assertIn("84 months", items[0]["text"])
        self.assertIn("$43,680", items[0]["text"])
    CUES = [(10.0, 12.0, "In 1940 the Act passed,"), (12.0, 14.0, "and it changed everything."),
            (14.0, 16.0, "Then $200 a month for 7% a year.")]

    def test_it_starts_with_the_cue_and_lasts_while_the_sentence_goes_on(self):
        items = av.build(self.CUES, 9.0, 18.0)
        self.assertEqual("1940", items[0]["text"])
        self.assertEqual((1.0, 5.0), (items[0]["start"], items[0]["end"]))   # cue 1 + the rest of its sentence, cut when the next value starts

    def test_two_values_in_one_cue_share_one_overlay_and_never_run_past_the_shot(self):
        items = av.build(self.CUES, 9.0, 16.5)
        self.assertEqual("$200 a month\n7%", items[1]["text"])
        self.assertLessEqual(items[1]["end"], 7.5)

    def test_minimum_and_maximum_seconds(self):
        short = av.build([(0.0, 0.5, "Only 25 percent.")], 0.0, 10.0, min_seconds=2.0)
        self.assertEqual(2.0, short[0]["end"] - short[0]["start"])
        long_ = av.build([(0.0, 30.0, "It costs 500 dollars.")], 0.0, 60.0, max_seconds=8.0)
        self.assertEqual(8.0, long_[0]["end"] - long_[0]["start"])


class ExpandTests(unittest.TestCase):
    def _manifest(self, text, subs, auto=True):
        doc = do_parse(text)[0]
        issues: list = []
        opts = TextOptions()
        opts.auto_values = auto
        return sl.expand(doc, subs, IMAGES, TransitionOptions(), issues, opts)

    def _subs(self):
        base = subtitles()
        return base

    def test_off_by_default_and_overlay_cue_is_resolved(self):
        subs = self._subs()
        n = len(subs)
        text = ('{"shots": [{"cues": "1-%d", "asset": "a.png", "overlays": '
                '[{"text": "Name", "cue": 2}]}]}' % n)
        man = self._manifest(text, subs, auto=False)
        ov = man.timeline[0].overlays
        self.assertEqual(1, len(ov))
        self.assertNotIn("cue", ov[0])
        cue2 = subs[1]
        self.assertAlmostEqual(cue2.start_seconds - subs[0].start_seconds, ov[0]["start"], places=2)
        self.assertAlmostEqual(cue2.end_seconds - subs[0].start_seconds, ov[0]["end"], places=2)

    def test_auto_values_are_added_only_when_the_option_is_on(self):
        subs = [m.SubtitleBlock(1, 0.0, 4.0, "It weighs 170 kilograms."),
                m.SubtitleBlock(2, 4.2, 9.0, "That is a lot.")]
        text = '{"shots": [{"cues": "1-2", "asset": "a.png"}]}'
        on = self._manifest(text, subs, auto=True).timeline[0].overlays
        self.assertEqual(["170 kilograms"], [o["text"] for o in on])
        self.assertEqual(0.0, on[0]["start"])
        self.assertIsNone(self._manifest(text, subs, auto=False).timeline[0].overlays)

    def test_a_value_the_writer_already_shows_is_not_doubled(self):
        subs = [m.SubtitleBlock(1, 0.0, 4.0, "It weighs 170 kilograms.")]
        text = '{"shots": [{"cues": "1", "asset": "a.png", "overlays": [{"text": "170 kilograms"}]}]}'
        ov = self._manifest(text, subs, auto=True).timeline[0].overlays
        self.assertEqual(1, len(ov))

    def test_bad_cue_is_a_warning_not_a_crash(self):
        issues: list = []
        doc = sl.parse('{"shots": [{"cues": "1", "asset": "a", "overlays": [{"text": "x", "cue": "abc"}]}]}',
                       issues, 1800, 2400)
        self.assertTrue(any(i.code == "SHOTLIST_OVERLAY_INVALID" for i in issues))
        self.assertIsNone(doc.shots[0].overlays[0]["cue"])


if __name__ == "__main__":
    unittest.main()
