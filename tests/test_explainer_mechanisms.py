"""Mechanism layouts: numbers are checked, sizes are computed, output validates."""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from whisperradar.video import parsing  # noqa: E402
from whisperradar.video.explainer import recipes  # noqa: E402
from whisperradar.video.explainer import validate as va  # noqa: E402
from whisperradar.video.explainer.validate import ExplainerError  # noqa: E402

SRT = "".join(f"{i}\n00:00:{(i - 1) * 8:02d},000 --> 00:00:{i * 8:02d},000\nline {i}\n\n"
              for i in range(1, 5))


def _build(scene):
    base = {"id": "m1", "cue_range": [1, 4], "title": "Memory"}
    base.update(scene)
    return recipes.build({"theme": "chalkboard", "scenes": [base]}, parsing.parse_srt(SRT))


def _err(scene):
    try:
        _build(scene)
    except ExplainerError as e:
        return " ".join(e.errors)
    return ""


MEM = {"layout": "memory", "capacity": 24, "unit": "GB", "items": [
    {"label": "Model weights", "gb": 14, "cue": 1},
    {"label": "Context cache", "gb": 6, "grows_from": 2, "cue": 2}]}


class MemoryTests(unittest.TestCase):
    def test_builds_validates_and_widths_are_proportional(self):
        doc = _build(MEM)
        va.parse_doc(doc)                      # raises when the engine would reject it
        objs = {o["id"]: o for o in doc["scenes"][0]["objects"]}
        w0, w1 = objs["m1_seg0"]["box"][2], objs["m1_seg1"]["box"][2]
        self.assertAlmostEqual((w0 + 0.004) / (w1 + 0.004), 14 / 6, places=2)

    def test_growth_emits_stretch(self):
        ev = [e for e in _build(MEM)["scenes"][0]["events"] if e["action"] == "stretch"]
        self.assertEqual(len(ev), 1)
        self.assertAlmostEqual(ev[0]["from_amount"], 2 / 6)
        self.assertEqual(ev[0]["amount"], 1.0)

    def test_overflow_rejected_unless_allowed(self):
        big = dict(MEM, items=[{"label": "A", "gb": 20, "cue": 1}, {"label": "B", "gb": 8, "cue": 2}])
        self.assertIn("add up to 28", _err(big))
        ok = _build(dict(big, allow_overflow=True))
        self.assertTrue(any("does not fit" in o.get("text", "") for o in ok["scenes"][0]["objects"]))

    def test_bad_inputs(self):
        self.assertIn("capacity", _err(dict(MEM, capacity=0)))
        self.assertIn("gb > 0", _err(dict(MEM, items=[{"label": "A", "gb": -1, "cue": 1}])))
        self.assertIn("last segment", _err(dict(MEM, items=[
            {"label": "A", "gb": 4, "grows_from": 1, "cue": 1}, {"label": "B", "gb": 4, "cue": 2}])))


class PrecisionTests(unittest.TestCase):
    P = {"layout": "precision", "params_b": 7, "items": [
        {"label": "16-bit", "bits": 16, "cue": 1}, {"label": "8-bit", "bits": 8, "cue": 2},
        {"label": "4-bit", "bits": 4, "cue": 3}]}

    def test_sizes_are_computed(self):
        texts = [o.get("text", "") for o in _build(self.P)["scenes"][0]["objects"]]
        self.assertIn("14 GB", texts)
        self.assertIn("7 GB", texts)
        self.assertIn("3.5 GB", texts)
        self.assertIn("7B x 4 bits / 8 = 3.5 GB", texts)
        self.assertTrue(any("overhead" in t for t in texts))

    def test_bad_inputs(self):
        self.assertIn("params_b", _err(dict(self.P, params_b="x")))
        self.assertIn("bits > 0", _err(dict(self.P, items=[{"label": "z", "bits": 0, "cue": 1}])))


class OffloadTests(unittest.TestCase):
    O = {"layout": "offload", "layers": 32, "items": [
        {"label": "GPU VRAM", "layers": 20, "cue": 1}, {"label": "System RAM", "layers": 12, "cue": 2}]}

    def test_builds_with_ranges(self):
        texts = [o.get("text", "") for o in _build(self.O)["scenes"][0]["objects"]]
        self.assertIn("layers 1-20", texts)
        self.assertIn("layers 21-32", texts)
        self.assertTrue(any("simplified" in t for t in texts))

    def test_counts_must_add_up(self):
        self.assertIn("hold 31 layers", _err(dict(self.O, items=[
            {"label": "GPU", "layers": 20, "cue": 1}, {"label": "RAM", "layers": 11, "cue": 2}])))

    def test_json_round_trip(self):
        doc = _build(self.O)
        self.assertEqual(json.loads(json.dumps(doc)), doc)


if __name__ == "__main__":
    unittest.main()
