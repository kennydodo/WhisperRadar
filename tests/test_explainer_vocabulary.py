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

    def test_chain(self):
        d = build({"id": "h", "layout": "chain", "items": [
            {"label": "Lab", "icon": "flask-conical", "tag": "own", "independent": False},
            {"label": "You", "icon": "user", "tag": "unchecked", "via": "headline"}]})
        self.assertTrue({"h_ic0", "h_pill0", "h_tg1", "h_cn1", "h_vi1"} <= ids(d))
        va.parse_doc(d, "", {})

    def test_ladder_marks_where_we_are(self):
        d = build({"id": "l", "layout": "ladder", "here_label": "today", "items": [
            {"label": "Claim", "here": True}, {"label": "Independent", "note": "result"}]})
        self.assertTrue({"l_rung0", "l_rung1", "l_here", "l_hl", "l_nt1"} <= ids(d))
        va.parse_doc(d, "", {})

    def test_frame_and_pill_shapes(self):
        class S:
            id, kind, curve, dashed, side = "x", "frame", 0, False, ""
        self.assertGreater(len(shapes.shape_strokes(S, (10, 10, 400, 200), None, 4)), 4)   # many dashes
        S.kind = "pill"
        self.assertEqual(len(shapes.shape_strokes(S, (10, 10, 400, 100), None, 4)), 1)

    def test_writer_knows_the_layouts(self):
        for lay in ("cards", "group", "chips", "callout", "chain", "ladder"):
            self.assertIn(lay, es.GENRE_LAYOUTS["tech"])
            self.assertIn(lay, es.GENRE_LAYOUTS["finance"])
            self.assertIn(lay, es.TEXT_LAYOUTS)


if __name__ == "__main__":
    unittest.main()


class CameraTests(unittest.TestCase):
    PLAN = {"scenes": [
        {"id": "a", "cue_range": [1, 1], "layout": "hero", "title": "T", "region": [0, 0, 0.5, 0.5],
         "items": [{"label": "x", "icon": "user"}], "camera": "region"},
        {"id": "b", "cue_range": [2, 2], "layout": "hero", "title": "T", "region": [0.5, 0, 0.5, 0.5],
         "base": "a", "items": [{"label": "y", "icon": "user"}]},
        {"id": "c", "cue_range": [3, 3], "layout": "hero", "title": "T", "base": "b", "camera": "overview",
         "region": [0, 0.5, 1, 0.5], "items": [{"label": "z", "icon": "user"}]}]}

    def test_camera_only_when_asked(self):
        d = recipes.build(self.PLAN, CUES, {})
        by = {s["id"]: s for s in d["scenes"]}
        self.assertEqual(by["a"]["camera"]["to"]["zoom"], 2.0)
        self.assertNotIn("camera", by["b"])                  # region + base alone never move the view
        self.assertEqual(by["b"]["base"], "a")
        self.assertEqual(by["c"]["camera"]["from"]["zoom"], 1.0)

    def test_pan_continues_from_the_previous_view(self):
        plan = {"scenes": [dict(self.PLAN["scenes"][0]),
                           dict(self.PLAN["scenes"][1], camera="region")]}
        d = recipes.build(plan, CUES, {})
        self.assertEqual(d["scenes"][1]["camera"]["from"]["center"], [0.25, 0.25])

    def test_channel_can_switch_cameras_off(self):
        d = recipes.build(self.PLAN, CUES, {"camera": "off"})
        self.assertFalse(any("camera" in s for s in d["scenes"]))

    def test_no_region_no_camera_scenes_unchanged(self):
        plain = {"scenes": [{"id": "a", "cue_range": [1, 1], "layout": "hero", "items": [{"label": "x"}]}]}
        d = recipes.build(plain, CUES, {})
        self.assertNotIn("camera", d["scenes"][0])
        self.assertNotIn("base", d["scenes"][0])

    def test_document_validates_and_bad_base_is_rejected(self):
        d = recipes.build(self.PLAN, CUES, {})
        va.parse_doc(d, "", {})
        d["scenes"][0]["base"] = "b"                        # not an earlier scene
        with self.assertRaises(va.ExplainerError):
            va.parse_doc(d, "", {})

    def test_view_math(self):
        from whisperradar.video.explainer import camera as cm
        cam = {"from": {"zoom": 1, "center": [0.5, 0.5]}, "to": {"zoom": 2, "center": [0.25, 0.25]}, "move_ms": 1000}
        self.assertEqual(cm.view_at(cam, 0, 30)[0], 1.0)
        z, cx, cy = cm.view_at(cam, 60, 30)
        self.assertAlmostEqual(z, 2.0)
        self.assertAlmostEqual(cx, 0.25)
        self.assertEqual(cm.supersample(cam), 2)
        self.assertEqual(cm.supersample(None), 1)
