"""Lucide icon library + chalkboard theme for the explainer's whiteboard mode."""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from whisperradar.video.explainer import cli  # noqa: E402
from whisperradar.video.explainer import strokes_library as lib  # noqa: E402
from whisperradar.video.explainer import themes  # noqa: E402
from whisperradar.video.explainer import validate as va  # noqa: E402
from whisperradar.video.explainer.validate import ExplainerError  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class LibraryTests(unittest.TestCase):
    def test_the_set_is_big_and_licensed(self):
        self.assertGreater(len(lib.lucide_names()), 1900)
        lic = os.path.join(os.path.dirname(lib.LUCIDE_FILE), "LICENSE-lucide.txt")
        self.assertTrue(os.path.isfile(lic))
        with open(lic, encoding="utf-8") as fh:
            self.assertIn("ISC", fh.read())

    def test_every_icon_loads_and_stays_inside_its_box(self):
        for name in lib.lucide_names():
            asset = lib.load(f"lucide:{name}")
            self.assertEqual(asset.viewBox, (100.0, 100.0))
            self.assertTrue(asset.strokes, name)
            for s in asset.strokes:
                # nothing is a zero-length "dot" the pen could not draw
                self.assertGreater(s.length, 0.05, f"{name}: zero-length stroke")
                for x, y in s.points:
                    self.assertTrue(math.isfinite(x) and math.isfinite(y), name)
                    self.assertTrue(-1 <= x <= 101 and -1 <= y <= 101,
                                    f"{name}: point ({x}, {y}) outside the box")

    def test_icons_schedule_like_any_other_asset(self):
        asset = lib.load("lucide:server")
        sched = lib.strokes_schedule(asset, 0, 90)
        self.assertEqual(len(sched), len(asset.strokes))
        self.assertAlmostEqual(sched[-1][2], 90.0, places=6)

    def test_name_is_forgiving_about_case_and_separators(self):
        for ref in ("lucide:Piggy Bank", "lucide:piggy_bank", "lucide: piggy-bank "):
            self.assertEqual(lib.load(ref).name, "lucide:piggy-bank")

    def test_unknown_icon_names_close_matches(self):
        with self.assertRaises(ValueError) as ctx:
            lib.load("lucide:piggybank")
        msg = str(ctx.exception)
        self.assertIn("unknown lucide icon", msg)
        self.assertIn("piggy-bank", msg)
        self.assertIn("cli icons", msg)

    def test_builtins_and_files_still_work(self):
        self.assertEqual(lib.load("builtin:house").name, "builtin:house")
        with self.assertRaises(ValueError):
            lib.load("builtin:rocket")


class SearchTests(unittest.TestCase):
    def names(self, q, limit=12):
        return [n for n, _ in lib.lucide_search(q, limit)]

    def test_exact_name_ranks_first(self):
        self.assertEqual(self.names("server")[0], "server")
        self.assertEqual(self.names("lock")[0], "lock")

    def test_tags_find_icons_by_meaning(self):
        self.assertIn("piggy-bank", self.names("savings"))
        self.assertIn("lock", self.names("password"))

    def test_all_words_must_match(self):
        self.assertEqual(self.names("piggy money"), ["piggy-bank"])
        self.assertEqual(self.names("zzzzqqq"), [])

    def test_empty_query_and_limit(self):
        self.assertEqual(lib.lucide_search(""), [])
        self.assertLessEqual(len(lib.lucide_search("arrow", 5)), 5)

    def test_cli_prints_matches(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["icons", "piggy"])
        self.assertEqual(rc, 0)
        self.assertIn("lucide:piggy-bank", buf.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["icons", "zzzzqqq"]), 1)


def _doc(asset="lucide:wallet", theme="chalkboard"):
    doc = {
        "schema_version": 2, "style": "whiteboard",
        "canvas": {"width": 320, "height": 180},
        "scenes": [{
            "id": "s", "shot_id": "S01_01", "cue_range": [1, 1],
            "objects": [{"id": "ic", "type": "stroke_asset", "asset": asset,
                         "box": [0.25, 0.2, 0.5, 0.7]}],
            "events": [{"target": "ic", "action": "draw_strokes",
                        "at": {"cue": 1, "edge": "start"}, "duration_ms": 1500}]}]}
    if theme:
        doc["theme"] = theme
    return doc


class ValidatorTests(unittest.TestCase):
    def test_lucide_asset_validates_and_counts_strokes(self):
        doc = va.parse_doc(_doc("lucide:server"))
        obj = doc.scenes[0].objects[0]
        self.assertEqual(obj._stroke_count, len(lib.load("lucide:server").strokes))

    def test_unknown_icon_is_a_readable_validation_error(self):
        with self.assertRaises(ExplainerError) as ctx:
            va.parse_doc(_doc("lucide:piggybank"))
        errs = " ".join(ctx.exception.errors)
        self.assertIn("ic", errs)
        self.assertIn("piggy-bank", errs)

    def test_a_too_fast_icon_is_still_rejected(self):
        data = _doc("lucide:server")
        data["scenes"][0]["events"][0]["duration_ms"] = 200
        with self.assertRaises(ExplainerError):
            va.parse_doc(data)


class ThemeTests(unittest.TestCase):
    def test_chalkboard_is_a_dark_board_with_light_chalk(self):
        t = themes.resolve("chalkboard")
        self.assertEqual(t.name, "chalkboard")

        def lum(h):
            h = h.lstrip("#")
            r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
            return 0.2126 * r + 0.7152 * g + 0.0722 * b
        self.assertLess(lum(t.background), 40)
        self.assertGreater(lum(t.foreground), 200)
        self.assertGreaterEqual(t.stroke_width, 4)

    def test_doc_can_select_it_and_the_light_board_stays_the_default(self):
        self.assertEqual(va.parse_doc(_doc()).theme.name, "chalkboard")
        self.assertEqual(va.parse_doc(_doc(theme=None)).theme.name, "whiteboard_light")


def _bright(img) -> int:
    """Number of near-white pixels (chalk)."""
    return sum(1 for v in img.convert("L").tobytes() if v > 200)


class RenderTests(unittest.TestCase):
    def test_storyboard_frame_shows_chalk_lines_on_the_dark_board(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "subtitles.srt"), "w", encoding="utf-8") as fh:
                fh.write("1\n00:00:00,000 --> 00:00:04,000\none\n")
            doc = _doc("lucide:piggy-bank")
            doc["canvas"] = {"width": 640, "height": 360}
            with open(os.path.join(tmp, "explainer.json"), "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            out = os.path.join(tmp, "sb")
            self.assertEqual(cli.cmd_storyboard(tmp, out), 0)
            final = Image.open(os.path.join(out, "s_final.png")).convert("RGB")
            start = Image.open(os.path.join(out, "s_start.png")).convert("RGB")
            bg = themes.resolve("chalkboard").background.lstrip("#")
            bg = tuple(int(bg[i:i + 2], 16) for i in (0, 2, 4))
            self.assertEqual(final.getpixel((2, 2)), bg)
            bright = _bright(final)
            self.assertGreater(bright, 300)                      # the chalk lines
            self.assertLess(_bright(start), bright / 4)


if __name__ == "__main__":
    unittest.main()
