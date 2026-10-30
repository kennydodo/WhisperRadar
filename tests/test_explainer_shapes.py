"""Chalk colours and hand-drawn shapes for the explainer's whiteboard mode."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from whisperradar.video.explainer import cli, shapes, themes  # noqa: E402
from whisperradar.video.explainer import schema as sc  # noqa: E402
from whisperradar.video.explainer import validate as va  # noqa: E402
from whisperradar.video.explainer.validate import ExplainerError  # noqa: E402


def _doc(objects, events=None):
    return {"schema_version": 2, "style": "whiteboard", "theme": "chalkboard",
            "canvas": {"width": 640, "height": 360},
            "scenes": [{"id": "s", "shot_id": "S01_01", "cue_range": [1, 1],
                        "objects": objects, "events": events or []}]}


def _errors(data):
    with_err = None
    try:
        va.parse_doc(data)
    except ExplainerError as e:
        with_err = " ".join(e.errors)
    return with_err


class ColourTests(unittest.TestCase):
    def test_named_roles_and_hex_resolve(self):
        t = themes.resolve("chalkboard")
        self.assertEqual(sc.resolve_color("yellow", t), sc.PALETTE_DARK["yellow"])
        self.assertEqual(sc.resolve_color("Accent", t), t.accent)
        self.assertEqual(sc.resolve_color("#aabbcc", t), "#AABBCC")
        for bad in ("chartreuse", "#12", "#GGGGGG", "", None, 5):
            self.assertIsNone(sc.resolve_color(bad, t))

    def test_light_board_gets_darker_chalk(self):
        t = themes.resolve("whiteboard_light")
        self.assertEqual(sc.resolve_color("yellow", t), sc.PALETTE_LIGHT["yellow"])

    def test_unknown_colour_is_a_readable_error(self):
        err = _errors(_doc([{"id": "a", "type": "handwriting", "text": "HI",
                             "box": [0.1, 0.1, 0.2, 0.1], "color": "chartreuse"}]))
        self.assertIn("unknown color", err)
        self.assertIn("yellow", err)


class ShapeValidationTests(unittest.TestCase):
    def shape(self, **kw):
        d = {"id": "sh", "type": "shape", "kind": "card", "box": [0.1, 0.1, 0.3, 0.3]}
        d.update(kw)
        return d

    def test_every_kind_validates_and_draws_inside_the_canvas(self):
        for kind in shapes.KINDS:
            d = self.shape(kind=kind)
            if kind in shapes.NEEDS_POINTS:
                d["points"] = [[0.2, 0.2], [0.6, 0.5]]
            doc = va.parse_doc(_doc([d]))
            spec = doc.scenes[0].objects[0]
            pts = [(0.2 * 640, 0.2 * 360), (0.6 * 640, 0.5 * 360)]
            strokes = shapes.shape_strokes(spec, (64, 36, 192, 108), pts, 5)
            self.assertTrue(strokes, kind)
            for s in strokes:
                for x, y in s.points:
                    self.assertTrue(-40 <= x <= 680 and -40 <= y <= 400, (kind, x, y))

    def test_arrow_needs_no_box_only_two_points(self):
        d = {"id": "ar", "type": "shape", "kind": "arrow",
             "points": [[0.2, 0.2], [0.6, 0.5]]}
        va.parse_doc(_doc([d]))
        d["points"] = [[0.2, 0.2]]
        self.assertIn("needs points", _errors(_doc([d])))

    def test_bad_inputs_are_named(self):
        self.assertIn("shape kind must be one of", _errors(_doc([self.shape(kind="star")])))
        self.assertIn("dashed is for line", _errors(_doc([self.shape(dashed=True)])))
        self.assertIn("side must be", _errors(_doc([self.shape(kind="brace", side="up")])))
        self.assertIn("curve must be", _errors(_doc([self.shape(kind="card", curve=4)])))

    def test_wobble_is_deterministic_per_id_and_differs_between_ids(self):
        def pts(oid):
            doc = va.parse_doc(_doc([self.shape(id=oid)]))
            spec = doc.scenes[0].objects[0]
            return shapes.shape_strokes(spec, (64, 36, 192, 108), [], 5)[0].points
        self.assertEqual(pts("a1"), pts("a1"))
        self.assertNotEqual(pts("a1"), pts("b2"))

    def test_too_fast_shape_is_not_rejected_but_actions_must_fit(self):
        ev = [{"target": "sh", "action": "draw_strokes", "at": {"cue": 1, "edge": "start"},
               "duration_ms": 600}]
        va.parse_doc(_doc([self.shape()], ev))


class RenderTests(unittest.TestCase):
    def test_coloured_shapes_render_in_their_colour(self):
        from PIL import Image
        objs = [{"id": "c", "type": "shape", "kind": "card", "box": [0.1, 0.2, 0.4, 0.4],
                 "color": "yellow"},
                {"id": "x", "type": "shape", "kind": "cross", "box": [0.6, 0.3, 0.2, 0.3],
                 "color": "red"}]
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "subtitles.srt"), "w", encoding="utf-8") as fh:
                fh.write("1\n00:00:00,000 --> 00:00:04,000\none\n")
            with open(os.path.join(tmp, "explainer.json"), "w", encoding="utf-8") as fh:
                json.dump(_doc(objs, [
                    {"target": "c", "action": "draw_strokes", "at": {"cue": 1, "edge": "start"},
                     "duration_ms": 800},
                    {"target": "x", "action": "draw_strokes", "at": {"cue": 1, "edge": "start"},
                     "duration_ms": 400}]), fh)
            out = os.path.join(tmp, "sb")
            self.assertEqual(cli.cmd_storyboard(tmp, out), 0)
            img = Image.open(os.path.join(out, "s_final.png")).convert("RGB")

            def count(rgb):
                return sum(1 for p in img.getdata()
                           if all(abs(p[i] - rgb[i]) < 40 for i in range(3)))
            yellow = tuple(int(sc.PALETTE_DARK["yellow"][i:i + 2], 16) for i in (1, 3, 5))
            red = tuple(int(sc.PALETTE_DARK["red"][i:i + 2], 16) for i in (1, 3, 5))
            self.assertGreater(count(yellow), 200)
            self.assertGreater(count(red), 80)


class HandFontTests(unittest.TestCase):
    def hw(self, **kw):
        d = {"id": "h", "type": "handwriting", "text": "Hello 3/4",
             "box": [0.1, 0.1, 0.5, 0.2]}
        d.update(kw)
        return d

    def test_chalkboard_defaults_to_a_real_hand_font(self):
        doc = va.parse_doc(_doc([self.hw()]))
        self.assertTrue(doc.theme.hand_font.endswith(".ttf"))
        self.assertEqual(va.hand_font_for(doc.scenes[0].objects[0], doc.theme),
                         doc.theme.hand_font)

    def test_channel_can_pick_font_or_the_pen_strokes(self):
        data = _doc([self.hw()])
        data["fonts"] = {"hand": "Caveat-Bold.ttf"}
        self.assertEqual(va.parse_doc(data).theme.hand_font, "Caveat-Bold.ttf")
        data["fonts"] = {"hand": "strokes"}
        self.assertEqual(va.parse_doc(data).theme.hand_font, "")

    def test_object_can_override_the_scene_font(self):
        doc = va.parse_doc(_doc([self.hw(font="Kalam-Bold.ttf")]))
        self.assertEqual(doc.scenes[0].objects[0].font, "Kalam-Bold.ttf")
        # forcing the strokes brings back the limited glyph set (no "=")
        self.assertIn("no handwriting glyph",
                      _errors(_doc([self.hw(font="strokes", text="a = b")])))

    def test_missing_font_is_a_readable_error(self):
        self.assertIn("font file not found",
                      _errors(_doc([self.hw(font="Nope.ttf")])))
        data = _doc([self.hw()])
        data["fonts"] = {"hand": "Nope.ttf"}
        self.assertIn("fonts.hand", _errors(data))

    def test_font_text_writes_on_and_is_blank_before_it_starts(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "subtitles.srt"), "w", encoding="utf-8") as fh:
                fh.write("1\n00:00:00,000 --> 00:00:04,000\none\n")
            with open(os.path.join(tmp, "explainer.json"), "w", encoding="utf-8") as fh:
                json.dump(_doc([self.hw(color="yellow")], [
                    {"target": "h", "action": "draw_strokes",
                     "at": {"cue": 1, "edge": "start"}, "duration_ms": 1200}]), fh)
            out = os.path.join(tmp, "sb")
            self.assertEqual(cli.cmd_storyboard(tmp, out), 0)
            start = Image.open(os.path.join(out, "s_start.png")).convert("RGB")
            final = Image.open(os.path.join(out, "s_final.png")).convert("RGB")
            bg = start.getpixel((2, 2))
            lit = lambda im: sum(1 for p in im.getdata() if p != bg)  # noqa: E731
            self.assertLess(lit(start), lit(final) * 0.3)      # only the first sliver
            self.assertGreater(lit(final), 1000)


class ChannelLookTests(unittest.TestCase):
    """The look is a channel setting saved in the database; explainer.json is
    only an optional per-production override."""

    def obj(self):
        return {"id": "h", "type": "handwriting", "text": "Hi 3/4", "box": [0.1, 0.1, 0.4, 0.2]}

    def test_channel_settings_pick_theme_and_fonts(self):
        data = _doc([self.obj()])
        data.pop("theme")
        look = {"theme": "whiteboard_light", "hand_font": "Caveat-Bold.ttf"}
        doc = va.parse_doc(data, "", look)
        self.assertEqual(doc.theme.name, "whiteboard_light")
        self.assertEqual(doc.theme.hand_font, "Caveat-Bold.ttf")

    def test_the_documents_own_choice_wins_for_that_production(self):
        data = _doc([self.obj()])                    # names theme "chalkboard"
        data["fonts"] = {"hand": "Kalam-Bold.ttf"}
        doc = va.parse_doc(data, "", {"theme": "whiteboard_light",
                                      "hand_font": "Caveat-Bold.ttf"})
        self.assertEqual(doc.theme.name, "chalkboard")
        self.assertEqual(doc.theme.hand_font, "Kalam-Bold.ttf")

    def test_channel_can_force_the_pen_strokes(self):
        doc = va.parse_doc(_doc([self.obj()]), "", {"hand_font": "strokes"})
        self.assertEqual(doc.theme.hand_font, "")

    def test_bad_channel_values_are_readable_errors(self):
        err = _errors_look(_doc([self.obj()]), {"hand_font": "Nope.ttf"})
        self.assertIn("channel explainer.hand: font file not found", err)
        err = _errors_look(dict(_doc([self.obj()]), theme=None), {"theme": "neon"})
        self.assertIn("channel explainer theme", err)

    def test_database_to_render_round_trip(self):
        from whisperradar import db
        from whisperradar.video import settings_spec as ss
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(__import__("pathlib").Path(tmp) / "wr.db")
            db.init_db(conn)
            oc = db.create_own_channel(conn, "Chan")
            ss.save_channel(conn, oc, {"explainer.theme": "chalkboard",
                                       "explainer.hand_font": "Caveat-Bold.ttf"})
            values = ss.effective(conn, oc)
            conn.close()
            self.assertEqual(ss.validate(values), [])
            pdir = os.path.join(tmp, "prod")
            os.makedirs(pdir)
            ss.apply_to_project(pdir, values)
            data = _doc([self.obj()])
            data.pop("theme")
            with open(os.path.join(pdir, "explainer.json"), "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            doc = va.load(os.path.join(pdir, "explainer.json"), pdir)
            self.assertEqual(doc.theme.name, "chalkboard")
            self.assertEqual(doc.theme.hand_font, "Caveat-Bold.ttf")

    def test_settings_page_offers_the_choices(self):
        from whisperradar.video import settings_spec as ss
        self.assertIn("chalkboard", ss.BY_PATH["explainer.theme"]["choices"])
        self.assertIn("strokes", ss.BY_PATH["explainer.hand_font"]["choices"])
        self.assertIn("Gaegu-Bold.ttf", ss.BY_PATH["explainer.hand_font"]["choices"])
        self.assertTrue(ss.validate({"explainer.theme": "neon"}))


def _errors_look(data, look):
    try:
        va.parse_doc(data, "", look)
    except ExplainerError as e:
        return " ".join(e.errors)
    return ""


class SpotlightTests(unittest.TestCase):
    def shot(self, events):
        from PIL import Image
        objs = [{"id": "c", "type": "shape", "kind": "card", "box": [0.1, 0.2, 0.5, 0.5],
                 "color": "yellow"}]
        base = {"target": "c", "at": {"cue": 1, "edge": "start"}}
        evs = [dict(base, action="draw_strokes", duration_ms=800)] + events
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "subtitles.srt"), "w", encoding="utf-8") as fh:
                fh.write("1\n00:00:00,000 --> 00:00:04,000\none\n")
            with open(os.path.join(tmp, "explainer.json"), "w", encoding="utf-8") as fh:
                json.dump(_doc(objs, evs), fh)
            out = os.path.join(tmp, "sb")
            self.assertEqual(cli.cmd_storyboard(tmp, out), 0)
            img = Image.open(os.path.join(out, "s_final.png")).convert("RGB")
            return sum(p[0] for p in img.getdata())            # total red ink + board

    def test_dim_fades_the_ink_back_and_undim_restores_it(self):
        base = {"target": "c", "at": {"cue": 1, "edge": "start"}, "offset_ms": 1000,
                "duration_ms": 400}
        full = self.shot([])
        dimmed = self.shot([dict(base, action="dim", amount=0.8)])
        back = self.shot([dict(base, action="dim", amount=0.8),
                          dict(base, action="undim", offset_ms=2000)])
        self.assertLess(dimmed, full)
        self.assertLess(dimmed - 18 * 640 * 360, (full - 18 * 640 * 360) * 0.6)
        self.assertGreater(back, full * 0.97)

    def test_dim_validation(self):
        obj = {"id": "c", "type": "shape", "kind": "card", "box": [0.1, 0.2, 0.5, 0.5]}
        ev = {"target": "c", "action": "dim", "at": {"cue": 1, "edge": "start"},
              "duration_ms": 300, "amount": 3}
        self.assertIn("amount must be", _errors(_doc([obj], [ev])))
        lab = {"id": "l", "type": "label", "text": "HI", "box": [0.1, 0.1, 0.2, 0.1]}
        ev2 = {"target": "l", "action": "dim", "at": {"cue": 1, "edge": "start"}}
        self.assertIn("cannot target", _errors(_doc([lab], [ev2])))


if __name__ == "__main__":
    unittest.main()


class WriteOnTailsTests(unittest.TestCase):
    def test_tall_font_keeps_descenders_inside_the_line(self):
        import os
        from whisperradar.video.explainer import pipe
        font = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fonts", "ShadowsIntoLight-Regular.ttf")
        mask, extents = pipe._write_layer(font, "gypq", (10, 10, 600, 100), (700, 200), 1.0)
        x0, x1, y0, y1 = extents[0]
        ink = mask.getbbox()
        self.assertLessEqual(ink[3], y1, "the tails of g/y/p/q must not be cut by the reveal band")
        self.assertGreaterEqual(ink[1], y0)


class TextLookTests(unittest.TestCase):
    """Text weight and letter spacing change the drawn text, and default to thin and normal."""

    def _font(self):
        import glob
        fonts = sorted(glob.glob(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                              "fonts", "Handlee-Regular.ttf")))
        if not fonts:
            self.skipTest("Handlee not available")
        return fonts[0]

    def test_weight_and_spacing_change_the_mask(self):
        from whisperradar.video.explainer import pipe
        f = self._font()
        box = (10, 10, 900, 120)
        thin, ex_thin = pipe._write_layer(f, "Memory fits", box, (1000, 200), 0.0, 0.04)
        bold, _ = pipe._write_layer(f, "Memory fits", box, (1000, 200), 1.0, 0.04)
        wide, ex_wide = pipe._write_layer(f, "Memory fits", box, (1000, 200), 0.0, 0.09)
        ink = lambda m: sum(m.histogram()[1:])
        self.assertGreater(ink(bold), ink(thin))
        self.assertGreater(ex_wide[0][1], ex_thin[0][1])

    def test_defaults_are_thin_and_normal(self):
        from whisperradar.video.explainer import pipe
        self.assertEqual(pipe.WEIGHT_FACTOR[""], 0.0)
        self.assertEqual(pipe.TRACKING_EM[""], pipe.TRACKING_EM["normal"])
        self.assertLess(pipe.TRACKING_EM["tight"], pipe.TRACKING_EM["wide"])

    def test_option_validation(self):
        from whisperradar.video import options as o
        opts = o.ProjectOptions()
        opts.explainer.text_weight = "huge"
        self.assertTrue(any("text weight" in e for e in opts.validate()))
        opts.explainer.text_weight, opts.explainer.letter_spacing = "bold", "wide"
        self.assertEqual([e for e in opts.validate() if "text weight" in e or "letter spacing" in e], [])


class StraightLinesTests(unittest.TestCase):
    """A graphic (motion_graphics) channel draws straight, clean lines; hand-drawn keeps the wobble."""

    def _spec(self, kind, **kw):
        from whisperradar.video.explainer import schema as sc
        return sc.ObjectSpec(id="x1", type=sc.SHAPE, kind=kind, **kw) if hasattr(sc, "ObjectSpec") else None

    def test_straight_removes_wobble_and_bend(self):
        from whisperradar.video.explainer import shapes
        class S:   # the few fields shape_strokes reads
            id, kind, side, curve, dashed = "x1", "arrow", None, 0.4, False
        wob = shapes.shape_strokes(S, (0, 0, 10, 10), [(100, 100), (500, 300)], 5.0)
        flat = shapes.shape_strokes(S, (0, 0, 10, 10), [(100, 100), (500, 300)], 5.0, straight=True)
        def off_line(strokes):     # largest distance of the shaft from the straight chord
            pts = strokes[0].points
            (x0, y0), (x1, y1) = (100, 100), (500, 300)
            ln = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
            return max(abs((x1 - x0) * (y0 - y) - (x0 - x) * (y1 - y0)) / ln for x, y in pts)
        self.assertGreater(off_line(wob), 5)
        self.assertLess(off_line(flat), 0.01)

    def test_card_is_not_wobbly_when_straight(self):
        from whisperradar.video.explainer import shapes
        class S:
            id, kind, side, curve, dashed = "c1", "card", None, 0.0, False
        a = shapes.shape_strokes(S, (100, 100, 400, 200), [], 5.0, straight=True)
        b = shapes.shape_strokes(S, (100, 100, 400, 200), [], 5.0)
        self.assertNotEqual([p for s in a for p in s.points], [p for s in b for p in s.points])

    def test_graphic_channel_sets_line_style(self):
        import json, tempfile
        from whisperradar.video.explainer import validate as va
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "imgtovideo.json"), "w") as fh:
            json.dump({"visual_mode": "motion_graphics", "explainer": {"theme": "chalkboard"}}, fh)
        look = va.project_look(tmp)
        self.assertEqual(look["visual_mode"], "motion_graphics")
        doc = {"schema_version": 1, "style": "whiteboard", "scenes": [{
            "id": "a", "shot_id": "S01_01", "cue_range": [1, 1], "objects": [], "events": []}]}
        from whisperradar.video.explainer import schema as sc
        doc["schema_version"] = sc.SCHEMA_VERSION
        self.assertEqual(va.parse_doc(doc, tmp, look).theme.line_style, "straight")
        look["visual_mode"] = "whiteboard"
        self.assertEqual(va.parse_doc(doc, tmp, look).theme.line_style, "")
