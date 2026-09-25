"""Pacing check (before images) and the 5-minute images resume.

The pacing check turns the planning brief's own rules into faults the shotlist
gate enforces BEFORE anything renders: a long hold cap, a long STATIC hold, ST
only on short holds and ~10% of shots, no motion code above ~40%, and no
fragmentation. Plus a warning when the count is low for the narration length.

Run: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import autorun, studio  # noqa: E402


def _ts(t):
    m, s = divmod(int(t), 60)
    return f"00:{m:02d}:{s:02d},000"


def _cues(count, secs):
    return [{"index": i + 1, "start": _ts(i * secs), "end": _ts((i + 1) * secs),
             "text": f"cue {i + 1}"} for i in range(count)]


def _shots(spec):
    return {"shots": [{"cues": f"{first}-{last}", "asset": asset,
                       "scene": "S01", "motion": motion}
                      for asset, first, last, motion in spec],
            "images": [{"file": asset, "prompt": "p"}
                       for asset, _, _, _ in spec]}


class PacingTests(unittest.TestCase):
    def test_a_well_paced_plan_has_no_faults(self):
        cues = _cues(12, 5)  # 60s
        data = _shots([("S01_01_SCN_ZI.png", 1, 2, "ZI"),
                       ("S01_02_SCN_ZO.png", 3, 4, "ZO"),
                       ("S01_03_SCN_PL.png", 5, 6, "PL"),
                       ("S01_04_SCN_PR.png", 7, 8, "PR"),
                       ("S01_05_INF_PU.png", 9, 10, "PU"),
                       ("S01_06_INF_PV.png", 11, 12, "PV")])
        faults, warnings = studio.shotlist_pacing(data, cues)
        self.assertEqual(faults, [])
        self.assertEqual(warnings, [])

    def test_a_long_hold_tells_the_planner_where_and_how_to_split(self):
        cues = _cues(12, 5)  # 60s
        data = _shots([("S01_01_SCN_ZI.png", 1, 6, "ZI"),
                       ("S01_02_SCN_ZO.png", 7, 12, "ZO")])
        faults, warnings = studio.shotlist_pacing(data, cues)
        text = "\n".join(faults)
        self.assertTrue(any("hold longer than" in f for f in faults), faults)
        self.assertIn("split around cue", text)           # where, in cue numbers
        self.assertIn("S01_01_SCN_ZI.png cues 1-6", text)  # the exact shot + range
        self.assertIn("next unused sub-beat", text)       # naming the new image
        self.assertIn("cues drive it", text)              # the count is never fixed
        self.assertEqual(warnings, [])

    def test_a_long_static_hold_is_a_fault(self):
        cues = _cues(12, 5)
        data = _shots([("S01_01_SCN_ST.png", 1, 6, "ST"),
                       ("S01_02_SCN_ZI.png", 7, 12, "ZI")])
        faults, _ = studio.shotlist_pacing(data, cues)
        self.assertTrue(any("STATIC hold" in f for f in faults), faults)

    def test_too_much_st_is_a_fault(self):
        cues = _cues(10, 2)
        data = _shots([(f"S01_0{i}_SCN_ST.png", i, i, "ST") for i in range(1, 7)])
        faults, _ = studio.shotlist_pacing(data, cues)
        self.assertTrue(any("of shots (cap" in f for f in faults), faults)

    def test_one_motion_dominating_is_a_fault(self):
        cues = _cues(6, 2)
        data = _shots([(f"S01_0{i}_SCN_ZI.png", i, i, "ZI") for i in range(1, 7)])
        faults, _ = studio.shotlist_pacing(data, cues)
        self.assertTrue(any("motion ZI is" in f for f in faults), faults)

    def test_fragmentation_is_a_fault(self):
        cues = _cues(6, 2)
        motions = ["ZI", "ZO", "PL", "PR", "PU", "PV"]
        data = _shots([(f"S01_0{i}_SCN_{m}.png", i, i, m)
                       for i, m in enumerate(motions, 1)])
        faults, _ = studio.shotlist_pacing(data, cues)
        self.assertTrue(any("single cue" in f for f in faults), faults)

    def test_no_image_may_hold_past_the_default_12s(self):
        cues = _cues(120, 10)  # 1200s = 20 min
        data = _shots([(f"S01_{i:02d}_SCN_ZI.png", i * 6 + 1, i * 6 + 6, "ZI")
                       for i in range(1, 21)])  # 20 shots of 60s
        faults, warnings = studio.shotlist_pacing(data, cues)
        self.assertEqual(warnings, [])
        self.assertTrue(any("longer than the 12s maximum" in f for f in faults),
                        faults)

    def test_dense_plans_are_legal_when_every_hold_is_short(self):
        # the count is never fixed: 6 images a minute is fine if each holds 10s
        cues = _cues(24, 5)  # 120s
        motions = ["ZI", "ZO", "PL", "PR", "PU", "PV"]
        data = _shots([(f"S01_{i + 1:02d}_SCN_{motions[i % 6]}.png",
                        i * 2 + 1, i * 2 + 2, motions[i % 6])
                       for i in range(12)])
        faults, warnings = studio.shotlist_pacing(data, cues)
        self.assertEqual(faults, [])
        self.assertEqual(warnings, [])

    def test_the_max_hold_is_tunable(self):
        cues = _cues(18, 10)  # 180s; six 30s shots
        motions = ["ZI", "ZO", "PL", "PR", "PU", "PV"]
        data = _shots([(f"S01_0{i}_SCN_{m}.png", i * 3 + 1, i * 3 + 3, m)
                       for i, m in enumerate(motions, 1)])
        self.assertTrue(studio.shotlist_pacing(data, cues, 12.0)[0])
        self.assertEqual(studio.shotlist_pacing(data, cues, 45.0)[0], [])


class FeedbackTests(unittest.TestCase):
    def test_the_feedback_carries_the_faults_and_no_previous_plan(self):
        # a re-plan is a FRESH call; dumping the previous plan makes the model
        # demand the exact prompts it was told to preserve
        review = {"faults": ["2 shot(s) hold longer than the 12s maximum"],
                  "ratio": 1.0, "matched": 2, "total": 2, "weak": []}
        data = _shots([("S01_01_SCN_ZI.png", 1, 6, "ZI"),
                       ("S01_02_SCN_ZO.png", 7, 12, "ZO")])
        fb = autorun._shotlist_feedback(review, 0.9, data)
        self.assertIn("FAULTS (must be zero)", fb)
        self.assertIn("hold longer than", fb)
        self.assertNotIn("S01_01_SCN_ZI.png", fb)


class ImageResumeTests(unittest.TestCase):
    def test_only_flow_missing_errors_resume(self):
        exc = RuntimeError("3 of 45 image(s) were not produced - see debug")
        self.assertTrue(autorun._should_resume_images(exc, 1))
        self.assertTrue(autorun._should_resume_images(
            exc, autorun.IMAGE_RESUME_ROUNDS - 1))
        self.assertFalse(autorun._should_resume_images(
            exc, autorun.IMAGE_RESUME_ROUNDS))
        self.assertFalse(autorun._should_resume_images(RuntimeError("boom"), 1))

    def test_the_break_is_five_minutes(self):
        self.assertEqual(autorun.IMAGE_RESUME_PAUSE_SECONDS, 300)


if __name__ == "__main__":
    unittest.main()
