import unittest

from whisperradar import explainer_scenes as es
from whisperradar.video.explainer import recipes, shapes, validate as va


class Cue:
    def __init__(self, i):
        self.index, self.start_seconds, self.end_seconds, self.text = i, (i - 1) * 20.0, i * 20.0, "x"


CUES = [Cue(i) for i in range(1, 5)]


def build(scene):
    return recipes.build({"scenes": [dict(scene, cue_range=[1, 1])]}, CUES, {})


def ids(doc):
    return {o["id"] for o in doc["scenes"][0]["objects"]}


class VocabularyTests(unittest.TestCase):
    def test_cards(self):
        d = build({"id": "c", "layout": "cards", "title": "T", "items": [
            {"label": "A", "icon": "user", "rows": ["one", {"text": "two"}]},
            {"label": "B", "icon": "clock", "rows": ["three"]}]})
        self.assertTrue({"c_card0", "c_hd0", "c_rw0_1", "c_card1"} <= ids(d))
        va.parse_doc(d, "", {})

    def test_cards_limits(self):
        with self.assertRaises(va.ExplainerError):
            build({"id": "c", "layout": "cards", "items": [{"label": "A"}]})

    def test_group_has_dashed_frame(self):
        d = build({"id": "g", "layout": "group", "caption": "Lab", "items": [
            {"label": "A", "icon": "user"}, {"label": "B", "icon": "clock"}]})
        self.assertIn("g_frame", ids(d))
        va.parse_doc(d, "", {})

    def test_chips_wrap_many(self):
        d = build({"id": "k", "layout": "chips", "items": [{"label": f"Name{i}"} for i in range(10)]})
        self.assertEqual(len([i for i in ids(d) if i.startswith("k_pill")]), 10)
        va.parse_doc(d, "", {})

    def test_callout(self):
        d = build({"id": "q", "layout": "callout", "items": [{"label": "A sentence " * 8, "note": "source"}]})
        self.assertTrue({"q_box", "q_tag", "q_lb0", "q_nt0"} <= ids(d))
        va.parse_doc(d, "", {})

    def test_frame_and_pill_shapes(self):
        class S:
            id, kind, curve, dashed, side = "x", "frame", 0, False, ""
        self.assertGreater(len(shapes.shape_strokes(S, (10, 10, 400, 200), None, 4)), 4)   # many dashes
        S.kind = "pill"
        self.assertEqual(len(shapes.shape_strokes(S, (10, 10, 400, 100), None, 4)), 1)

    def test_writer_knows_the_layouts(self):
        for lay in ("cards", "group", "chips", "callout"):
            self.assertIn(lay, es.GENRE_LAYOUTS["tech"])
            self.assertIn(lay, es.GENRE_LAYOUTS["finance"])
            self.assertIn(lay, es.TEXT_LAYOUTS)


if __name__ == "__main__":
    unittest.main()
