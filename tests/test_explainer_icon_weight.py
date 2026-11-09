import unittest

from whisperradar.video.explainer import pipe, validate, schema as sc


class IconWeightTests(unittest.TestCase):
    def test_default_icon_is_lighter_than_the_old_one(self):
        self.assertLess(pipe.ICON_FACTOR[""], 1.0)
        self.assertEqual(pipe.ICON_FACTOR["normal"], 1.0)
        self.assertLess(pipe.ICON_FACTOR["thin"], pipe.ICON_FACTOR["light"])
        self.assertGreater(pipe.ICON_FACTOR["bold"], 1.0)

    def test_pen_nib_dot_is_off(self):
        self.assertFalse(pipe.SHOW_PEN_NIB)

    def test_theme_carries_icon_weight_only_when_set(self):
        t = sc.ThemeSpec(name="x", background="#000000", foreground="#ffffff", muted="#888888",
                         accent="#00aaff", positive="#00ff00", negative="#ff0000",
                         heading_font="a.ttf", body_font="a.ttf", mono_font="a.ttf")
        self.assertNotIn("icon_weight", t.to_dict())
        t.icon_weight = "thin"
        self.assertEqual(t.to_dict()["icon_weight"], "thin")


if __name__ == "__main__":
    unittest.main()


class TextSizeTests(unittest.TestCase):
    def test_text_size_smaller_shrinks_the_written_size(self):
        import os
        font = "/usr/share/fonts/truetype/crosextra/Carlito-Regular.ttf"
        if not os.path.isfile(font):
            self.skipTest("no test font")
        box = (0, 0, 1200, 120)
        a = pipe._fit_text(font, "Hello world", box, 0, 0.04, pipe.TEXT_SIZE["normal"])[1]
        b = pipe._fit_text(font, "Hello world", box, 0, 0.04, pipe.TEXT_SIZE["small"])[1]
        self.assertLess(b, a)

    def test_system_fonts_offered_only_when_present(self):
        from whisperradar.video import settings_spec
        self.assertIn("calibril.ttf", settings_spec.SYSTEM_FONTS)
        names = settings_spec._font_choices()
        self.assertTrue(all(n in names or not __import__("os").path.isfile("C:/Windows/Fonts/" + n)
                            for n in settings_spec.SYSTEM_FONTS))
