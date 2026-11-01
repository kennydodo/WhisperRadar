"""Spoken points beside a motion diagram: shape, limits, anchors, order, key terms."""
import copy
import unittest

from whisperradar import explainer_scenes as es
from whisperradar import explainer_terms as et
from whisperradar.video.explainer import recipes, remotion_backend as rb
from whisperradar.video.explainer.validate import ExplainerError
from tests.test_explainer_general_motion import CUES, FLOW, BARS, SPLIT, plan


def with_points(scene, pts, **extra):
    s = copy.deepcopy(scene)
    s["points"] = pts
    s.update(extra)
    return s


P3 = [{"text": "A browser asks first", "at": "A browser asks"},
      {"text": "A miss goes to the server", "at": "If the cache misses"},
      {"text": "The page comes back", "at": "The page comes back"}]


def build(scene):
    return recipes.build(plan(scene), es._cue_objs(CUES))


class PointsTests(unittest.TestCase):
    def test_points_become_timed_params(self):
        doc = build(with_points(FLOW, P3))
        pts = doc["scenes"][0]["remotion"]["params"]["points"]
        self.assertEqual([p["text"] for p in pts], [p["text"] for p in P3])
        self.assertTrue(all(a["start_ms"] < b["start_ms"] for a, b in zip(pts, pts[1:])))
        self.assertLessEqual(pts[0]["start_ms"], 500)         # said on the scene's opening words

    def test_scene_without_points_is_unchanged(self):
        self.assertNotIn("points", build(FLOW)["scenes"][0]["remotion"]["params"])

    def test_bare_strings_are_normalised(self):
        p = plan(with_points(FLOW, ["A browser asks first"]))
        es.normalize_plan(p)
        self.assertEqual(p["scenes"][0]["points"], [{"text": "A browser asks first"}])

    def test_too_many_points_for_the_mode(self):
        many = [{"text": f"point {i}", "at": w} for i, w in enumerate(
            ["A browser asks", "If the cache misses", "The page comes back", "Rent takes 1200",
             "Food takes 450", "Savings get 600"])]
        with self.assertRaises(ExplainerError) as cm:
            build(with_points(FLOW, many))
        self.assertIn("too many", " ".join(cm.exception.errors))

    def test_caps_by_mode(self):
        pts = lambda n: [{"text": f"p{i}", "start_ms": 1000 * i} for i in range(n)]
        self.assertEqual(rb._check_points({"points": pts(4)}, "x"), [])
        self.assertTrue(rb._check_points({"points": pts(5)}, "x"))
        self.assertEqual(rb._check_points({"points": pts(5), "points_mode": "all"}, "x"), [])
        self.assertTrue(rb._check_points({"points": pts(6), "points_mode": "all"}, "x"))

    def test_place_is_validated(self):
        ok = {"points": [{"text": "a", "start_ms": 0}]}
        self.assertEqual(rb._check_points(dict(ok, points_place="below"), "x"), [])
        self.assertTrue(rb._check_points(dict(ok, points_place="middle"), "x"))

    def test_last_point_needs_reading_time(self):
        late = [{"text": "A browser asks first", "at": "A browser asks"},
                {"text": "Then the page comes back to the browser", "at": "same way"}]
        with self.assertRaises(ExplainerError) as cm:
            recipes.build(plan(with_points(FLOW, late), SPLIT), es._cue_objs(CUES))
        self.assertIn("too late to be read", " ".join(cm.exception.errors))

    def test_unknown_mode_and_long_text_are_rejected(self):
        self.assertTrue(rb._check_points({"points": [{"text": "a", "start_ms": 0}], "points_mode": "spin"}, "x"))
        self.assertTrue(rb._check_points({"points": [{"text": "x" * 61, "start_ms": 0}]}, "x"))

    def test_points_on_the_same_moment_are_rejected(self):
        two = [{"text": "one", "at": "A browser asks"}, {"text": "two", "at": "A browser asks"}]
        with self.assertRaises(ExplainerError):
            build(with_points(FLOW, two))

    def test_point_with_words_not_in_the_narration_is_a_plain_error(self):
        with self.assertRaises(ExplainerError):
            build(with_points(FLOW, [{"text": "x", "at": "words nobody says here"}]))

    def test_points_do_not_yet_count_as_shown_key_terms(self):
        # conservative until the check knows WHEN a point is visible
        scene = with_points(BARS, [{"text": "NCM stands for Next Concept Modeling", "at": "Imagine method A"}])
        self.assertNotIn("NCM", et.scene_text(scene))

    def test_point_text_and_place_change_the_scene_fingerprint(self):
        a = build(with_points(FLOW, P3))["scenes"][0]["remotion"]
        b = build(with_points(FLOW, [dict(P3[0], text="Something else")] + P3[1:]))["scenes"][0]["remotion"]
        c = build(with_points(FLOW, P3, points_place="below"))["scenes"][0]["remotion"]
        d = build(with_points(FLOW, P3, points_mode="all"))["scenes"][0]["remotion"]
        fps = {rb.fingerprint({"params": x["params"]}, 1920, 1080, "p") for x in (a, b, c, d)}
        self.assertEqual(len(fps), 4)

    def test_migration_keeps_a_hand_edited_point(self):
        from whisperradar.video.explainer import render
        fresh = build(with_points(FLOW, P3))
        edited = copy.deepcopy(fresh)
        edited["scenes"][0]["remotion"]["params"]["points"][1]["text"] = "My own wording"
        self.assertTrue(render._same_content(fresh, copy.deepcopy(fresh)))
        self.assertFalse(render._same_content(edited, fresh))     # an edit is not "generated"

    def test_checker_accepts_a_plan_with_points(self):
        faults = es.check(plan(with_points(FLOW, P3)), CUES, 60.0, {"scene_genre": "tech"}, [])
        self.assertFalse([f for f in faults if "point" in f.lower()], faults)


if __name__ == "__main__":
    unittest.main()
