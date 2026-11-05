import unittest

from whisperradar.video.explainer import font_styles as fs, recipes, validate as va

PLAN = {"scenes": [{"id": "a", "cue_range": [1, 1], "layout": "row", "title": "Hello",
                    "items": [{"label": "One"}, {"label": "Two"}]}]}


class FakeCue:
    index = 1
    start_seconds = 0.0
    end_seconds = 6.0
    text = "Hello one two"


def _theme(look):
    data = recipes.build(PLAN, [FakeCue()], look)
    return data, va.parse_doc(data, "", look)


class FontStyleTests(unittest.TestCase):
    def test_default_is_video1(self):
        self.assertEqual(fs.resolve({}), fs.STYLES["video1"])

    def test_no_look_leaves_theme(self):
        self.assertIsNone(fs.resolve(None))

    def test_own_fonts_keep_their_look(self):
        self.assertIsNone(fs.resolve({"hand_font": "Neucha-Regular.ttf"}))

    def test_explicit_style_beats_own_fonts(self):
        self.assertEqual(fs.resolve({"font_style": "clean", "hand_font": "x.ttf"}), fs.STYLES["clean"])

    def test_default_title_is_marker_and_text_sans(self):
        data, doc = _theme({})
        titles = [o for o in data["scenes"][0]["objects"] if o["id"].endswith("_title")]
        self.assertEqual(titles[0]["font"], "CaveatBrush-Regular.ttf")
        self.assertTrue(doc.theme.hand_font.endswith("Inter-Bold.ttf") or doc.theme.hand_font == "Inter-Bold.ttf"
                        or "Inter-Bold" in doc.theme.hand_font)

    def test_clean_has_no_title_font(self):
        data, _ = _theme({"font_style": "clean"})
        self.assertFalse(any(o.get("font") for o in data["scenes"][0]["objects"]))

    def test_unknown_style_rejected(self):
        with self.assertRaises(va.ExplainerError):
            _theme({"font_style": "comic"})


if __name__ == "__main__":
    unittest.main()
