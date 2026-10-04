"""The motion MIX: caps are also targets, for every motion profile.

A plan with no ST, no PU/PD and nothing but ZI satisfies every cap, which is
how production 13 got 211 ZI shots. The brief now states the mix as a target
and the pacing check fails a plan that ignores it (plans of 20+ shots).
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import briefs, studio  # noqa: E402
from whisperradar.external_prompts import _SHOTLIST_OUTPUT_RULES  # noqa: E402

CUSTOM = briefs.custom_profile({
    "allowed": ["ST", "ZI", "ZO", "PU", "PD"], "st_max_share": 0.15,
    "tilt_max_share": 0.1})


def _ts(t):
    m, s = divmod(int(t), 60)
    return f"00:{m:02d}:{s:02d},000"


def _plan(codes, secs=3):
    """Two cues per shot, `secs` long each (a 6s hold; one cue per shot would
    trip the fragmentation check) - except ST shots, which take one cue (3s,
    under the 5s ST limit). `codes` is the motion per shot."""
    shots, n = [], 0
    for i, m in enumerate(codes):
        span = 1 if m == "ST" else 2
        shots.append({"cues": f"{n + 1}-{n + span}" if span > 1 else str(n + 1),
                      "asset": f"S01_{i + 1:02d}_SCN_{m}.png",
                      "scene": "S01", "motion": m})
        n += span
    cues = [{"index": i + 1, "start": _ts(i * secs), "end": _ts((i + 1) * secs),
             "text": "c"} for i in range(n)]
    images = [{"file": s["asset"], "prompt": "p"} for s in shots]
    return {"shots": shots, "images": images}, cues


def _faults(codes, profile, **kw):
    data, cues = _plan(codes, **kw)
    return studio.shotlist_pacing(data, cues, 12, profile)[0]


def _mixed(n=40):
    # 15% ST, 10% PU/PD, the rest ZI/ZO split evenly
    st, tilt = round(n * 0.15), round(n * 0.10)
    rest = n - st - tilt
    return (["ST"] * st + ["PU"] * (tilt // 2) + ["PD"] * (tilt - tilt // 2)
            + ["ZI"] * (rest // 2) + ["ZO"] * (rest - rest // 2))


class Targets(unittest.TestCase):
    def test_custom_profile_targets(self):
        self.assertEqual(briefs.mix_targets(CUSTOM),
                         [(("ST",), 0.15), (("PU", "PD"), 0.10)])
        self.assertTrue(briefs.zoom_balanced(CUSTOM))

    def test_per_code_overrides_stay_caps(self):
        # PL/PR are capped low on purpose (and cost API credits): never a target
        codes = [c for cs, _ in briefs.mix_targets(briefs.STANDARD) for c in cs]
        self.assertEqual(codes, ["ST"])

    def test_static_profile_has_no_mix(self):
        self.assertEqual(briefs.mix_text(briefs.MOTION_PRESETS["static"]), "")

    def test_zoom_split_needs_both_zooms(self):
        only_zi = briefs.custom_profile({"allowed": ["ST", "ZI"],
                                         "st_max_share": 0.1})
        self.assertFalse(briefs.zoom_balanced(only_zi))
        self.assertNotIn("ZI and ZO", briefs.mix_text(only_zi))


class BriefText(unittest.TestCase):
    def test_every_non_static_preset_states_the_mix(self):
        template = briefs.read_template()
        for key, profile in briefs.MOTION_PRESETS.items():
            text = briefs.render_brief(template, profile, "")
            self.assertEqual("Aim for this mix" in text,
                             bool(briefs.mix_text(profile)), key)
            self.assertNotIn("{{", text, key)

    def test_custom_brief_has_the_numbers(self):
        text = briefs.render_brief(briefs.read_template(), CUSTOM, "")
        self.assertIn("ST about 15%", text)
        self.assertIn("PU/PD about 10% together", text)
        self.assertIn("between 40% and 60%", text)
        # once in the motion section and once in the checklist
        self.assertEqual(text.count("Aim for this mix"), 2)

    def test_planner_asks_for_a_file_named_shotlist_json(self):
        self.assertIn("`shotlist.json`", _SHOTLIST_OUTPUT_RULES)


class Gate(unittest.TestCase):
    def test_all_zi_fails_the_mix(self):
        text = " | ".join(_faults(["ZI"] * 40, CUSTOM))
        self.assertIn("ST is only 0%", text)
        self.assertIn("PU/PD is only 0%", text)
        self.assertIn("ZI is 100% of the ZI+ZO shots", text)

    def test_a_balanced_plan_passes(self):
        self.assertEqual(_faults(_mixed(40), CUSTOM), [])

    def test_zoom_split_bounds(self):
        base = ["ST"] * 6 + ["PU"] * 2 + ["PD"] * 2
        ok = base + ["ZI"] * 12 + ["ZO"] * 18      # 40% ZI
        bad = base + ["ZI"] * 10 + ["ZO"] * 20     # 33% ZI
        self.assertEqual(_faults(ok, CUSTOM), [])
        self.assertTrue(any("ZI is 33%" in f for f in _faults(bad, CUSTOM)))

    def test_floor_is_half_the_share(self):
        # 15% ST -> floor 7.5%: 3 of 40 (7.5%) passes, 2 of 40 (5%) fails
        def plan(st):
            rest = 40 - st - 4
            return (["ST"] * st + ["PU"] * 2 + ["PD"] * 2
                    + ["ZI"] * (rest // 2) + ["ZO"] * (rest - rest // 2))
        self.assertEqual(_faults(plan(3), CUSTOM), [])
        self.assertTrue(any(f.startswith("ST is only")
                            for f in _faults(plan(2), CUSTOM)))

    def test_small_plans_are_exempt(self):
        self.assertEqual(_faults(["ZI"] * 19, CUSTOM), [])

    def test_applies_to_the_standard_profile_too(self):
        faults = _faults(["ZI"] * 30, briefs.STANDARD)
        self.assertTrue(any(f.startswith("ST is only") for f in faults))
        self.assertTrue(any("ZI is 100%" in f for f in faults))

    def test_static_channel_is_untouched(self):
        static = briefs.MOTION_PRESETS["static"]
        faults = _faults(["ST"] * 30, static)
        self.assertFalse([f for f in faults if "mix" in f or "only" in f])


if __name__ == "__main__":
    unittest.main()
