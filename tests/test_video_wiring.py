import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests import video_golden_support as g
from whisperradar import studio


class EngineSwitch(unittest.TestCase):
    def test_default_is_csharp(self):
        self.assertFalse(studio._python_video(SimpleNamespace()))
        self.assertFalse(studio._python_video(SimpleNamespace(studio_video_engine="csharp")))
        self.assertTrue(studio._python_video(SimpleNamespace(studio_video_engine="python")))

    def test_python_preview_plans_then_renders(self):
        with tempfile.TemporaryDirectory() as d:
            folder = g.make_project("fees", d, "fees")
            calls = {}

            def fake_render(path, preview, **kw):
                calls["args"] = (path, preview)
                return True, str(Path(path) / "out" / "preview.mp4"), []

            with mock.patch("whisperradar.video.ffprobe.ffprobe_duration_seconds",
                            return_value=8432 / 30), \
                 mock.patch("whisperradar.video.render_project.render_final", fake_render):
                studio._python_preview(Path(folder))
            self.assertEqual(calls["args"], (str(folder), True))
            self.assertTrue((Path(folder) / "out" / "timeline.json").exists())

    def test_python_preview_raises_on_plan_failure(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):
                studio._python_preview(Path(d))


if __name__ == "__main__":
    unittest.main()
