"""Explicit channel caps and opt-in pacing limits, without hidden targets."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import briefs, studio  # noqa: E402
from whisperradar.external_prompts import _SHOTLIST_OUTPUT_RULES  # noqa: E402

CUSTOM = briefs.custom_profile({
    "allowed": ["ST", "ZI", "ZO", "PU", "PD"], "st_max_share": 0.15,
    "tilt_max_share": 0.1, "mix_mode": "caps"})


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


class ExplicitSettings(unittest.TestCase):
    def test_caps_do_not_impose_minimum_usage(self):
        self.assertEqual(_faults(["ZI"] * 40, CUSTOM), [])
        self.assertEqual(briefs.mix_text(CUSTOM), "")

    def test_blank_custom_caps_do_not_inherit_limits(self):
        profile = briefs.custom_profile({"allowed": list(briefs.MOTION_CODES)})
        self.assertIsNone(profile.code_max_share)
        self.assertIsNone(profile.st_max_share)
        self.assertIsNone(profile.st_max_hold)
        self.assertIsNone(profile.static_long_hold)
        self.assertEqual(profile.code_share_overrides, {})
        self.assertEqual(_faults(["PU"] * 40, profile), [])
        self.assertEqual(_faults(["ST"] * 40, profile), [])

    def test_configured_caps_are_still_enforced(self):
        profile = briefs.custom_profile({"allowed": ["ZI", "ZO"], "code_max_share": .8})
        self.assertEqual(_faults(["ZI"] * 32 + ["ZO"] * 8, profile), [])
        self.assertTrue(any("motion ZI" in f for f in _faults(["ZI"] * 38 + ["ZO"] * 2, profile)))
        self.assertTrue(any("together" in f for f in _faults(["PU"] * 40, CUSTOM)))

    def test_brief_states_caps_without_hidden_targets(self):
        text = briefs.render_brief(briefs.read_template(), CUSTOM, "")
        self.assertIn("ST is at most 15%", text)
        self.assertIn("PU/PD at most ~10%", text)
        self.assertNotIn("Aim for this mix", text)
        self.assertNotIn("40% and 60%", text)

    def test_single_cue_limit_is_opt_in(self):
        from dataclasses import replace
        data, cues = _plan(["ZI"] * 10)
        for i, shot in enumerate(data["shots"]):
            shot["cues"] = str(i + 1)
        cues = cues[:10]
        self.assertFalse(any("single cue" in f for f in studio.shotlist_pacing(data, cues, 12, CUSTOM)[0]))
        prof = replace(CUSTOM, single_cue_share=.5)
        self.assertTrue(any("single cue" in f for f in studio.shotlist_pacing(data, cues, 12, prof)[0]))

    def test_minimum_hold_uses_configured_allowance(self):
        from dataclasses import replace
        data, cues = _plan(["ZI"] * 10)
        strict = replace(CUSTOM, min_hold=8, short_hold_share=0, short_hold_tolerance=0)
        relaxed = replace(strict, short_hold_share=1)
        self.assertTrue(any("hold under" in f for f in studio.shotlist_pacing(data, cues, 12, strict)[0]))
        self.assertFalse(any("hold under" in f for f in studio.shotlist_pacing(data, cues, 12, relaxed)[0]))

    def test_judge_uses_same_policy_and_no_hidden_targets(self):
        from whisperradar.external_prompts import _hard_rules
        prof = briefs.resolve_profile("custom", 8, 12, custom={
            "allowed": ["ZI", "ZO"], "short_hold_share": .2, "single_cue_share": .7})
        plan = {"profile": prof, "max_hold": 12, "allow_refs": False, "min_align": .8}
        text = _hard_rules(plan, {}, {}, 20)
        self.assertIn("20%", text)
        self.assertIn("70%", text)
        self.assertNotIn("MOTION TARGETS", text)
        prof = briefs.resolve_profile("custom", custom={"allowed": ["ZI", "ZO"]})
        plan["profile"] = prof
        self.assertNotIn("FRAGMENTATION:", _hard_rules(plan, {}, {}, 20))

    def test_pacing_policy_resolves_for_presets(self):
        prof = briefs.resolve_profile("standard", 8, 12,
            custom={"short_hold_share": .2, "single_cue_share": .7})
        self.assertEqual(prof.short_hold_share, .2)
        self.assertEqual(prof.single_cue_share, .7)
        text = briefs.render_brief(briefs.read_template(), prof, "")
        self.assertIn("at most 25%", text)
        self.assertIn("At most 75%", text)


class ApproximateMixTests(unittest.TestCase):
    def profile(self, **extra):
        return briefs.custom_profile({"allowed": ["ZI", "ZO", "PU", "PD"],
            "tilt_max_share": .15, **extra})

    def test_eighty_five_percent_zoom_accepts_both_boundaries(self):
        profile = self.profile()
        self.assertEqual(profile.share_targets, ((("PU", "PD"), .15), (("ZI", "ZO"), .85)))
        for zoom in (80, 82, 85, 88, 90):
            with self.subTest(zoom=zoom):
                self.assertEqual(_faults(["ZI"] * zoom + ["PU"] * (100 - zoom), profile), [])
        for zoom in (79, 91):
            text = " | ".join(_faults(["ZI"] * zoom + ["PU"] * (100 - zoom), profile))
            self.assertIn("ZI/ZO together", text)
            self.assertIn("80%-90%", text)
            self.assertIn("PU/PD together", text)

    def test_each_remaining_family_gets_its_own_tolerance(self):
        profile = briefs.custom_profile({"allowed": ["ZI", "ZO", "PL", "PR", "PU", "PD"],
                                         "pan_max_share": .10, "tilt_max_share": .05})
        self.assertEqual(_faults(["ZI"] * 80 + ["PL"] * 12 + ["PU"] * 8, profile), [])
        self.assertTrue(any("PL/PR together" in f for f in
            _faults(["ZI"] * 80 + ["PL"] * 16 + ["PU"] * 4, profile)))

    def test_zero_and_fractional_tolerances_are_honoured(self):
        strict = self.profile(share_tolerance=0)
        self.assertEqual(_faults(["ZI"] * 85 + ["PU"] * 15, strict), [])
        self.assertTrue(_faults(["ZI"] * 86 + ["PU"] * 14, strict))
        profile = self.profile(share_tolerance=.025)
        self.assertIn("82.5%-87.5%", briefs.mix_text(profile))
        self.assertEqual(_faults(["ZI"] * 86 + ["PU"] * 14, profile), [])
        self.assertTrue(_faults(["ZI"] * 88 + ["PU"] * 12, profile))

    def test_no_hidden_split_or_target_for_blank_families(self):
        profile = self.profile()
        self.assertEqual(_faults(["ZO"] * 85 + ["PD"] * 15, profile), [])
        blank = briefs.custom_profile({"allowed": ["ZI", "ZO", "PU", "PD"]})
        self.assertEqual(blank.share_targets, ())
        partial = briefs.custom_profile({"allowed": list(briefs.MOTION_CODES), "tilt_max_share": .15})
        self.assertEqual(partial.share_targets, ((("PU", "PD", "PV"), .15),))

    def test_judge_and_writer_receive_the_same_inclusive_ranges(self):
        from whisperradar.external_prompts import _hard_rules
        profile = self.profile()
        plan = {"profile": profile, "max_hold": 12, "allow_refs": False, "min_align": .8}
        judge = _hard_rules(plan, {}, {}, 200)
        writer = briefs.render_brief(briefs.read_template(), profile)
        for text in (judge, writer):
            self.assertIn("80%-90%", text)
            self.assertIn("10%-20%", text)
            self.assertIn("exact percentages are not required", text)
            self.assertNotIn("PU/PD together may be at most", text)

    def test_caps_mode_stays_one_sided_and_uses_tolerance(self):
        profile = self.profile(mix_mode="caps")
        self.assertEqual(profile.share_targets, ())
        self.assertEqual(_faults(["ZI"] * 100, profile), [])
        self.assertEqual(_faults(["ZI"] * 80 + ["PU"] * 20, profile), [])
        self.assertTrue(_faults(["ZI"] * 79 + ["PU"] * 21, profile))


class PacingToleranceTests(unittest.TestCase):
    def profile(self, **overrides):
        return briefs.resolve_profile("custom", custom={"allowed": ["ZI"],
            "short_hold_share": .5, "single_cue_share": .5, **overrides})

    def test_single_cue_boundary_and_disabled_limit(self):
        for single in (50, 55, 56):
            data, cues = _plan(["ZI"] * 100)
            n = 0
            for i, shot in enumerate(data["shots"]):
                span = 1 if i < single else 2
                shot["cues"] = str(n + 1) if span == 1 else f"{n + 1}-{n + 2}"
                n += span
            cues = cues[:n]
            faults = studio.shotlist_pacing(data, cues, 12, self.profile())[0]
            self.assertEqual(any("single cue" in f for f in faults), single > 55)
        profile = self.profile(single_cue_tolerance=0)
        self.assertEqual(briefs.pacing_share_cap(profile, "single_cue"), .5)
        disabled = briefs.resolve_profile("custom", custom={"allowed": ["ZI"], "single_cue_tolerance": .1})
        self.assertIsNone(briefs.pacing_share_cap(disabled, "single_cue"))

    def test_minimum_hold_boundary_excludes_final_shot(self):
        from dataclasses import replace
        profile = replace(self.profile(), min_hold=8, single_cue_share=None)
        for short in (50, 55, 56):
            data, cues = _plan(["ZI"] * 101)
            t = 0
            for i in range(101):
                duration = 6 if i < short or i == 100 else 10
                for j in range(2):
                    cue = cues[i * 2 + j]
                    cue["start"], cue["end"] = _ts(t), _ts(t + duration / 2)
                    t += duration / 2
            faults = studio.shotlist_pacing(data, cues, 12, profile)[0]
            self.assertEqual(any("hold under" in f for f in faults), short > 55)

    def test_writer_and_judge_show_the_effective_pacing_caps(self):
        from dataclasses import replace
        from whisperradar.external_prompts import _hard_rules
        profile = replace(self.profile(short_hold_tolerance=.025, single_cue_tolerance=.10), min_hold=8)
        plan = {"profile": profile, "max_hold": 12, "allow_refs": False, "min_align": .8}
        for text in (briefs.render_brief(briefs.read_template(), profile), _hard_rules(plan, {}, {}, 100)):
            self.assertIn("52.5%", text)
            self.assertIn("60%", text)
            self.assertIn("tolerance", text)


if __name__ == "__main__":
    unittest.main()
