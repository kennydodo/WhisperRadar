"""Probe image: large enough that the content check can accept a real GPU."""
import tempfile
import unittest
from pathlib import Path

import base  # noqa: F401  (sys.path + isolated DATABASE_URL)
from PIL import Image
from services import upscaler


class ProbeImage(unittest.TestCase):
    def test_probe_is_large_enough_for_the_64x64_content_check(self):
        self.assertGreaterEqual(upscaler._PROBE_SIZE, 64)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.png"
            upscaler._write_probe_png(path)
            with Image.open(path) as image:
                self.assertEqual(image.size, (upscaler._PROBE_SIZE, upscaler._PROBE_SIZE))
                self.assertEqual(image.mode, "RGB")

    def test_gradient_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.png"
            second = Path(tmp) / "b.png"
            upscaler._write_probe_png(first)
            upscaler._write_probe_png(second)
            self.assertEqual(first.read_bytes(), second.read_bytes())


if __name__ == "__main__":
    unittest.main()
