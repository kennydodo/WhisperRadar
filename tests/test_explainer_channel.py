"""Explainer look in the channel settings form (My channels > Explainer)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402,F401

from whisperradar import db  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.video import settings_spec as ss  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class ExplainerChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.oc = db.create_own_channel(conn, "Chan")
        conn.close()
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def saved(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        try:
            return ss.load_channel(conn, self.oc)
        finally:
            conn.close()

    def post(self, **fields):
        data = {"id": str(self.oc), "explainer_present": "1", **fields}
        return self.client.post("/my-channels/edit", data=data)

    def test_the_form_has_an_explainer_section_with_fonts(self):
        html = self.client.get("/my-channels").get_data(as_text=True)
        self.assertIn(f'id="edit-{self.oc}-explainer"', html)
        self.assertIn('name="explainer_hand_font"', html)
        self.assertIn("Gaegu-Bold.ttf", html)
        self.assertIn("pen strokes", html)
        self.assertIn("chalkboard", html)

    def test_saving_stores_the_choice_in_the_database_and_reloads_it(self):
        r = self.post(explainer_theme="chalkboard", explainer_hand_font="Caveat-Bold.ttf",
                      explainer_heading_font="", explainer_body_font="Kalam-Bold.ttf")
        self.assertEqual(r.status_code, 302)
        self.assertIn("msg=", r.headers["Location"])
        self.assertEqual(self.saved(), {"explainer.theme": "chalkboard",
                                        "explainer.hand_font": "Caveat-Bold.ttf",
                                        "explainer.body_font": "Kalam-Bold.ttf"})
        html = self.client.get("/my-channels").get_data(as_text=True)
        self.assertIn('value="Caveat-Bold.ttf" selected', html)

    def test_pen_speed_scene_count_and_diagram_style_are_saved(self):
        r = self.post(explainer_pen_speed="fast", explainer_scenes_per_video="many",
                      explainer_diagram_style="minimal")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.saved(), {"explainer.pen_speed": "fast", "explainer.scenes_per_video": "many",
                                        "explainer.diagram_style": "minimal"})
        html = self.client.get("/my-channels").get_data(as_text=True)
        self.assertIn('value="fast" selected', html)
        self.assertIn("pen speed", html)

    def test_bad_pen_speed_is_refused(self):
        r = self.post(explainer_pen_speed="warp")
        self.assertIn("error=", r.headers["Location"])
        self.assertEqual(self.saved(), {})

    def test_channel_type_normal_or_explainer(self):
        html = self.client.get("/my-channels").get_data(as_text=True)
        self.assertIn('name="explainer_mode"', html)
        self.assertIn("normal channel", html)
        self.post(explainer_mode="whiteboard", explainer_theme="chalkboard")
        self.assertEqual(self.saved()["visual_mode"], "whiteboard")
        self.post(explainer_mode="images")
        self.assertEqual(self.saved(), {"visual_mode": "images"})
        self.post(explainer_mode="")
        self.assertEqual(self.saved(), {})
        self.assertIn("error=", self.post(explainer_mode="movie").headers["Location"])

    def test_pen_strokes_and_blank_inherit(self):
        self.post(explainer_hand_font="strokes", explainer_theme="chalkboard")
        self.assertEqual(self.saved()["explainer.hand_font"], "strokes")
        self.post(explainer_hand_font="", explainer_theme="")
        self.assertEqual(self.saved(), {})

    def test_other_video_options_are_kept(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        ss.save_channel(conn, self.oc, {"motion.easing": "linear"})
        conn.close()
        self.post(explainer_theme="chalkboard")
        self.assertEqual(self.saved(), {"motion.easing": "linear",
                                        "explainer.theme": "chalkboard"})

    def test_a_bad_value_is_refused_with_a_message(self):
        r = self.post(explainer_theme="neon")
        self.assertIn("error=", r.headers["Location"])
        self.assertEqual(self.saved(), {})

    def test_a_form_without_the_section_leaves_it_alone(self):
        self.post(explainer_theme="chalkboard")
        r = self.client.post("/my-channels/edit", data={"id": str(self.oc), "name": "Chan"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.saved(), {"explainer.theme": "chalkboard"})

    def test_font_preview_route_serves_only_repo_fonts(self):
        self.assertEqual(self.client.get("/explainer/fonts/Gaegu-Bold.ttf").status_code, 200)
        self.assertEqual(self.client.get("/explainer/fonts/..%2Fconfig.yaml").status_code, 404)
        self.assertEqual(self.client.get("/explainer/fonts/nope.ttf").status_code, 404)


if __name__ == "__main__":
    unittest.main()


class ExplainerColourTests(unittest.TestCase):
    def _doc(self, look):
        import tempfile, json, os
        from whisperradar.video.explainer import validate as v
        d = {"schema_version": 2, "style": "whiteboard", "theme": "chalkboard",
             "scenes": [{"id": "a", "shot_id": "a", "cue_range": [1, 2], "objects": [
                 {"id": "t", "type": "text", "text": "Hi", "style": "title", "box": [0.1, 0.1, 0.8, 0.2],
                  "color": "heading"},
                 {"id": "u", "type": "text", "text": "Yo", "style": "label", "box": [0.1, 0.4, 0.8, 0.2],
                  "color": "text"}], "events": []}]}
        return v.parse_doc(d, "", look)

    def test_default_names_are_chalk(self):
        from whisperradar.video.explainer import schema as sc
        doc = self._doc({})
        self.assertEqual(sc.resolve_color("heading", doc.theme), sc.resolve_color("yellow", doc.theme))
        self.assertEqual(sc.resolve_color("text", doc.theme), sc.resolve_color("white", doc.theme))

    def test_channel_colours_apply(self):
        from whisperradar.video.explainer import schema as sc
        doc = self._doc({"heading_color": "orange", "text_color": "#AABBCC", "background": "#112233"})
        self.assertEqual(sc.resolve_color("heading", doc.theme), sc.resolve_color("orange", doc.theme))
        self.assertEqual(sc.resolve_color("text", doc.theme), "#AABBCC")
        self.assertEqual(doc.theme.background, "#112233")
        self.assertIn("text_color", doc.theme.to_dict())

    def test_unset_colours_keep_the_theme_dict_unchanged(self):
        self.assertNotIn("heading_color", self._doc({}).theme.to_dict())

    def test_bad_colour_is_a_readable_error(self):
        from whisperradar.video.explainer.validate import ExplainerError
        with self.assertRaises(ExplainerError) as cm:
            self._doc({"text_color": "banana"})
        self.assertIn("text color", cm.exception.errors[0])

    def test_options_validate_colours(self):
        from whisperradar.video import options as o
        p = o.ProjectOptions()
        p.explainer.heading_color = "nope"
        p.explainer.background = "orange"
        errs = p.validate()
        self.assertTrue(any("heading colour" in e for e in errs))
        self.assertTrue(any("board colour" in e for e in errs))
