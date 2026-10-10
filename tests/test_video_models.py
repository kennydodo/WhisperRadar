import json
import unittest
from pathlib import Path

from whisperradar.video import models as m
from whisperradar.video import options as o
from whisperradar.video import timeline_json as tj

FIX = Path(__file__).parent / "fixtures" / "video"
CASES = sorted(p.name for p in FIX.iterdir() if (p / "timeline.json").exists())


def _norm(x):
    """Numbers compare by value (C# writes 2560, Python 2560.0)."""
    if isinstance(x, dict):
        return {k: _norm(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_norm(v) for v in x]
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):
        return round(float(x), 9)
    return x


class TimelineRoundTrip(unittest.TestCase):
    def test_golden_timelines_round_trip(self):
        self.assertTrue(CASES)
        for case in CASES:
            with self.subTest(case=case):
                raw = json.loads((FIX / case / "timeline.json").read_text(encoding="utf-8-sig"))
                raw.setdefault("sounds", [])  # older C# builds wrote no sounds list
                back = tj.load_from_json(json.dumps(raw)).to_dict()
                self.assertEqual(_norm(raw), _norm(back))

    def test_rejects_other_schema(self):
        with self.assertRaises(ValueError):
            tj.load_from_json('{"schema_version": 2}')


class OptionsTests(unittest.TestCase):
    def test_defaults_validate(self):
        self.assertEqual(o.ProjectOptions().validate(), [])

    def test_golden_options_load(self):
        for case in CASES:
            p = FIX / case / "imgtovideo.json"
            if p.exists():
                with self.subTest(case=case):
                    opts = o.load(p)
                    self.assertEqual(opts.validate(), [])

    def test_partial_json_overlays_defaults(self):
        opts = o.load_from_json('{"schema_version":1,"output":{"width":1920,"height":1080}}')
        self.assertEqual((opts.output.width, opts.output.height, opts.output.fps), (1920, 1080, 30.0))
        self.assertEqual(opts.naming.image_extensions, [".png"])

    def test_no_version_still_loads_and_bad_version_raises(self):
        self.assertEqual(o.load_from_json('{"planner":"v1"}').planner, "v1")
        with self.assertRaises(ValueError):
            o.load_from_json('{"schema_version": 9}')

    def test_validation_messages(self):
        opts = o.ProjectOptions()
        opts.output.width = 1921
        opts.timing.floor_image_seconds = 9
        opts.render.encoder = "x"
        opts.sound.default_pop = "nope"
        self.assertEqual(len(opts.validate()), 4)

    def test_scene_id(self):
        n = o.NamingOptions()
        self.assertEqual(n.scene_id(3), "S03")
        n.number_padding = 9
        self.assertEqual(n.scene_id(3), "S0003")

    def test_save_load(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "imgtovideo.json"
            a = o.ProjectOptions()
            a.motion.easing = m.LINEAR
            o.save(a, p)
            self.assertEqual(o.load(p).to_dict(), a.to_dict())


class ModelTests(unittest.TestCase):
    def test_rect(self):
        r = m.Rect(0, 0, 10, 5)
        self.assertTrue(r.is_inside(m.Rect(0, 0, 10, 5)))
        self.assertFalse(r.is_inside(m.Rect(1, 0, 10, 5)))
        self.assertEqual(r.to_dict()["right"], 10)

    def test_codes(self):
        self.assertEqual(m.motion_from_suffix("pv"), m.PAN_REVEAL)
        self.assertIsNone(m.motion_from_suffix("xx"))
        self.assertEqual(m.image_type_from_code("cu"), "close_up")
        self.assertEqual(m.minimum_size(m.PAN_UP), (2304, 2160))
        self.assertEqual(m.to_xfade_name("fade_black"), "fadeblack")


if __name__ == "__main__":
    unittest.main()
