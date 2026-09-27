"""Whisper device/compute-type selection - no hardcoded float16.

Run: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import transcribe  # noqa: E402


class ComputeTypeTests(unittest.TestCase):
    def _with_supported(self, supported, device):
        with mock.patch("ctranslate2.get_supported_compute_types",
                        return_value=supported):
            return transcribe.pick_compute_type(device)

    def test_prefers_float16_when_the_gpu_supports_it(self):
        self.assertEqual(
            self._with_supported({"float16", "int8_float32", "float32"},
                                 "cuda"),
            "float16")

    def test_pascal_gpu_without_float16_uses_int8_float32(self):
        # a GTX 1060 reports exactly this set
        self.assertEqual(
            self._with_supported({"int8", "int8_float32", "float32"}, "cuda"),
            "int8_float32")

    def test_cpu_uses_int8(self):
        self.assertEqual(
            self._with_supported({"int8", "int16", "int8_float32", "float32"},
                                 "cpu"),
            "int8")

    def test_probe_failure_falls_back_safely(self):
        with mock.patch("ctranslate2.get_supported_compute_types",
                        side_effect=RuntimeError("boom")):
            self.assertEqual(transcribe.pick_compute_type("cpu"), "int8")
            self.assertEqual(transcribe.pick_compute_type("cuda"), "float16")

    def test_pick_always_returns_a_supported_type(self):
        with mock.patch("ctranslate2.get_supported_compute_types",
                        return_value={"int8"}):
            self.assertEqual(transcribe.pick_compute_type("cuda"), "int8")


if __name__ == "__main__":
    unittest.main()
