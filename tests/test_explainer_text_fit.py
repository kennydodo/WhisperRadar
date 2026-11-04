import unittest

from whisperradar import explainer_scenes as es
from whisperradar.video.explainer import recipes


class FitTextTests(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(recipes.fit_text("Monthly bill", [0.1, 0.1, 0.4, 0.08]),
                         ("Monthly bill", 1))

    def test_long_point_in_a_narrow_box_breaks_onto_two_lines(self):
        text, n = recipes.fit_text("Figure assumes nonstop usage every single month of the year",
                                   [0.5, 0.3, 0.335, 0.11])
        self.assertEqual(n, 2)
        self.assertIn("\n", text)

    def test_existing_breaks_are_kept(self):
        self.assertEqual(recipes.fit_text("a b\nc d", [0, 0, 0.1, 0.05]), ("a b\nc d", 2))

    def test_unbreakable_text_is_left_for_the_checker(self):
        text, n = recipes.fit_text("Supercalifragilisticexpialidocious", [0, 0, 0.1, 0.05])
        self.assertEqual(n, 1)

    def test_checker_names_text_that_is_still_too_small(self):
        built = {"scenes": [{"id": "s1", "objects": [
            {"type": "handwriting", "text": "Supercalifragilisticexpialidocious",
             "box": [0, 0, 0.1, 0.05]},
            {"type": "handwriting", "text": "Fine", "box": [0, 0, 0.4, 0.1]}]}]}
        faults = es.text_fit_faults(built)
        self.assertEqual(len(faults), 1)
        self.assertIn("Supercali", faults[0])


if __name__ == "__main__":
    unittest.main()
