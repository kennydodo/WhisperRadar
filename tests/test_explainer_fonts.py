"""Font resolution: repo fonts/ folder, project-relative, system fonts.

Layout order (docs/explainer/user-guide.md "Fonts"):
1. the path as given (absolute works anywhere);
2. repo-level fonts/ folder (drop Google Fonts here, reference by file name);
3. project-relative (per-production font files);
4. C:/Windows/Fonts.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import wr_tmp  # noqa: E402,F401

from whisperradar.video.explainer import themes  # noqa: E402
from whisperradar.video.explainer.layout import find_font, layout_text  # noqa: E402
from whisperradar.video.explainer.validate import ExplainerError  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FindFontTests(unittest.TestCase):
    def setUp(self):
        self._dirs = []

    def tearDown(self):
        for d in self._dirs:
            wr_tmp.cleanup(d)

    def _temp_dir(self, prefix):
        d = tempfile.TemporaryDirectory(prefix=prefix)
        self._dirs.append(d)
        return d.name

    def _make_font_copy(self, folder: str, name: str) -> str:
        import shutil
        src = themes.resolve("finance_dark").heading_font
        self.assertTrue(os.path.isfile(src))
        dst = os.path.join(folder, name)
        shutil.copyfile(src, dst)
        return dst

    def test_absolute_path_wins(self):
        src = themes.resolve("finance_dark").heading_font
        self.assertEqual(find_font(src), os.path.abspath(src))

    def test_repo_fonts_folder(self):
        fonts_dir = os.path.join(REPO, "fonts")
        os.makedirs(fonts_dir, exist_ok=True)
        self._make_font_copy(fonts_dir, "TestRepoFont.ttf")
        try:
            self.assertEqual(find_font("fonts/TestRepoFont.ttf"),
                             os.path.abspath(os.path.join(
                                 fonts_dir, "TestRepoFont.ttf")))
            # bare file name also finds it
            self.assertEqual(find_font("TestRepoFont.ttf"),
                             os.path.abspath(os.path.join(
                                 fonts_dir, "TestRepoFont.ttf")))
        finally:
            os.remove(os.path.join(fonts_dir, "TestRepoFont.ttf"))

    def test_project_relative(self):
        proj = self._temp_dir("explainer_fonts_")
        self._make_font_copy(proj, "TestProjFont.ttf")
        got = find_font("TestProjFont.ttf", project_folder=proj)
        self.assertEqual(got, os.path.join(proj, "TestProjFont.ttf"))

    def test_missing_font_returns_empty(self):
        self.assertEqual(find_font("DefinitelyNotAFontHere.ttf"), "")

    def test_layout_uses_project_font_and_errors_are_readable(self):
        theme = themes.resolve("finance_dark")
        theme.body_font = "fonts/MissingFont.ttf"
        errors = []
        tl = layout_text("Hello", (10, 10, 200, 60), "body", theme, 180,
                         180 / 1080.0, "Scene s1, object t1", errors,
                         project_folder=self._temp_dir("explainer_fonts_"))
        self.assertTrue(any("font file not found" in e for e in errors))
        self.assertTrue(any("MissingFont.ttf" in e for e in errors))

    def test_layout_uses_repo_font_when_found(self):
        fonts_dir = os.path.join(REPO, "fonts")
        os.makedirs(fonts_dir, exist_ok=True)
        self._make_font_copy(fonts_dir, "TestRepoFont2.ttf")
        try:
            theme = themes.resolve("finance_dark")
            theme.body_font = "TestRepoFont2.ttf"
            errors = []
            tl = layout_text("Hello", (10, 10, 200, 60), "body", theme, 180,
                             180 / 1080.0, "Scene s1, object t1", errors)
            self.assertEqual(errors, [])
            self.assertTrue(tl.font_path.endswith("TestRepoFont2.ttf"))
        finally:
            os.remove(os.path.join(fonts_dir, "TestRepoFont2.ttf"))

    def test_traversal_still_blocked_for_project_fonts(self):
        proj = self._temp_dir("explainer_fonts_")
        self.assertEqual(
            find_font("../outside.ttf", project_folder=proj), "")


if __name__ == "__main__":
    unittest.main()
