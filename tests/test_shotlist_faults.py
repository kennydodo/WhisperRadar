"""The shotlist gate's reference rules (no LLM, no filesystem).

The rule added today: a shotlist that declares SUPPLIED references (registry
entries with a path) but attaches none of them is a failure - the brief calls a
plan that never features a supplied character a failure, and the refs stage
would copy the files for nothing.

Run: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import studio  # noqa: E402
from whisperradar import autorun  # noqa: E402
from unittest import mock  # noqa: E402


def _shotlist(refs=None, image_refs=None, images=2):
    imgs, shots = [], []
    for i in range(images):
        name = f"S01_{i + 1:02d}_SCN_PR.png"
        entry = {"file": name, "prompt": f"a detailed prompt for shot number {i}"}
        if image_refs and i < len(image_refs) and image_refs[i]:
            entry["refs"] = image_refs[i]
        imgs.append(entry)
        shots.append({"cues": str(i + 1), "asset": name, "scene": "S01",
                      "motion": "PR"})
    data = {"shots": shots, "images": imgs}
    if refs is not None:
        data["refs"] = refs
    return data


class SuppliedRefsTests(unittest.TestCase):
    def test_declared_but_unused_supplied_refs_are_a_fault(self):
        data = _shotlist(refs={"MAYA": "refs/MAYA.jpeg"},
                         image_refs=[None, None])
        faults = studio.shotlist_structural_faults(data, 2)
        self.assertTrue(any("supplied reference" in f for f in faults), faults)

    def test_using_one_supplied_ref_is_enough(self):
        data = _shotlist(refs={"MAYA": "refs/MAYA.jpeg"},
                         image_refs=[["MAYA"], None])
        faults = studio.shotlist_structural_faults(data, 2)
        self.assertFalse(any("supplied reference" in f for f in faults), faults)

    def test_a_plan_with_no_refs_is_fine(self):
        data = _shotlist(refs={}, image_refs=[None, None])
        faults = studio.shotlist_structural_faults(data, 2)
        self.assertFalse(any("reference" in f for f in faults), faults)

    def test_an_undeclared_used_ref_is_still_a_fault(self):
        data = _shotlist(refs={}, image_refs=[["MAYA"], None])
        faults = studio.shotlist_structural_faults(data, 2)
        self.assertTrue(any("not declared" in f for f in faults), faults)


class ParseTests(unittest.TestCase):
    def test_a_raw_control_character_in_a_prompt_still_parses(self):
        # the planner sometimes emits a literal newline inside a prompt string;
        # json rejects that by default, which would fail the whole plan
        text = ('{"shots": [{"cues": "1", "asset": "S01_01_SCN_ZI.png", '
                '"scene": "S01", "motion": "ZI"}], "images": [{"file": '
                '"S01_01_SCN_ZI.png", "prompt": "line one\nline two"}]}')
        data, _ = studio.parse_shotlist_output(text)
        self.assertEqual(len(data["images"]), 1)

    def test_a_trailing_comma_still_parses(self):
        # a long plan occasionally slips one (a dropped "refs" leaves `"}",`)
        text = ('{"shots": [{"cues": "1", "asset": "S01_01_SCN_ZI.png", '
                '"scene": "S01", "motion": "ZI"}], "images": [{"file": '
                '"S01_01_SCN_ZI.png", "prompt": "a scene", }, ]}')
        data, _ = studio.parse_shotlist_output(text)
        self.assertEqual(len(data["images"]), 1)


class ContinuationTests(unittest.TestCase):
    def test_the_continuation_carries_the_rule_and_the_tail(self):
        # the brief's Section 11: continue in the next message until every cue
        # is covered, instead of throwing a cut-off plan away
        p = studio.continuation_prompt("BASE PROMPT", "some partial json")
        self.assertIn("BASE PROMPT", p)
        self.assertIn("CUT OFF", p)
        self.assertIn("some partial json", p)
        self.assertIn("Continue from EXACTLY", p)


class AlignmentPromptTests(unittest.TestCase):
    """A single still frame cannot show everything a sentence says (a cat
    appearing every morning, something wired into every human, an urge
    happening inside someone's head). The completeness judge used to treat
    ALL of that as a missing element, which meant narration-heavy content
    could never pass the 90% detail-completeness gate no matter how good the
    prompts were - it wasn't the prompts, it was the judge asking for the
    impossible. These lock in the carve-outs added to fix that."""

    def _prompt(self):
        chunk = [{"cues": "1-2", "asset": "S01_01_SCN_PR.png",
                 "prompt": "DAY. A cat sits near a bus stop."}]
        cues = {1: "There's a stray cat that", 2: "sits near a bus stop"}
        return studio.alignment_prompt(chunk, cues)

    def test_recurrence_and_frequency_are_excluded(self):
        p = self._prompt()
        self.assertIn("recurrence or frequency", p)
        self.assertIn("every morning", p)

    def test_duration_and_timespan_are_excluded(self):
        p = self._prompt()
        self.assertIn("duration or a span of time", p)
        self.assertIn("over decades", p)

    def test_universal_and_statistical_claims_are_excluded(self):
        p = self._prompt()
        self.assertIn("universal or statistical claims", p)

    def test_internal_invisible_states_are_excluded(self):
        p = self._prompt()
        self.assertIn("internal, invisible states", p)
        self.assertIn("before conscious awareness", p)

    def test_causal_explanation_is_excluded_in_favor_of_showing_that(self):
        p = self._prompt()
        self.assertIn("causal or explanatory claims", p.lower())

    def test_the_shot_and_narration_still_appear(self):
        p = self._prompt()
        self.assertIn("S01_01_SCN_PR.png", p)

    def test_exact_numbers_ranges_and_dates_are_excluded(self):
        # a real run flagged prompts "missing" for things like the exact
        # "25-150 Hz" purr-frequency range and "1943" - but this channel's
        # own style.md bans readable numbers/labels in every image, so the
        # judge was demanding a figure the prompt is contractually forbidden
        # from rendering. A relative visual (a gauge, a bar) must be enough.
        p = self._prompt()
        self.assertIn("exact numbers, ranges, dates or units", p)
        self.assertIn("25-150 Hz", p)

    def test_the_channel_style_is_handed_to_the_judge_when_given(self):
        # the judge itself decides the number-exclusion using the style's
        # OWN text policy, rather than a blanket "never required" rule that
        # would be too lenient for a channel whose style allows on-image text
        p = studio.alignment_prompt(
            [{"cues": "1-1", "asset": "S01_01_SCN_PR.png", "prompt": "p"}],
            {1: "text"},
            style_guide="Text policy: no readable words or numbers.")
        self.assertIn("CHANNEL VISUAL STYLE", p)
        self.assertIn("no readable words or numbers", p)
        self.assertIn("WHEN THE CHANNEL STYLE BELOW BANS", p)

    def test_no_style_guide_omits_the_channel_style_block(self):
        p = self._prompt()
        self.assertNotIn("CHANNEL VISUAL STYLE", p)
        self.assertIn("bus stop", p)
        self.assertIn("A cat sits near a bus stop.", p)


class ShotlistFeedbackTests(unittest.TestCase):
    """A re-plan is a FRESH call with no memory of the previous attempt, so
    it can only fix the weak shots actually named in the feedback string -
    truncating that list silently strands the rest at the same weak verdict
    forever, which is why detail % used to plateau across attempts."""

    def _review(self, weak_count):
        weak = [{"asset": f"S01_{i:03d}_SCN_ZI.png", "verdict": "weak",
                 "missing": ["subject"], "reason": "too vague",
                 "narration": f"cue {i}"} for i in range(weak_count)]
        return {"faults": [], "matched": 0, "total": weak_count,
                "weak": weak, "ratio": 0.0, "warnings": []}

    def test_all_weak_shots_are_listed_not_just_the_first_ten(self):
        review = self._review(15)
        feedback = autorun._shotlist_feedback(review, min_align=0.8)
        for i in range(15):
            self.assertIn(f"S01_{i:03d}_SCN_ZI.png", feedback)

    def test_hitting_the_upstream_cap_notes_there_may_be_more(self):
        review = self._review(30)
        feedback = autorun._shotlist_feedback(review, min_align=0.8)
        self.assertIn("and any other prompt that under-specifies", feedback)

    def test_no_stray_data_argument_required(self):
        # data was an unused parameter on the old signature; calling with
        # just (review, min_align) must work
        review = self._review(1)
        self.assertTrue(autorun._shotlist_feedback(review, 0.8))


class ReviewShotlistJudgeFailureTests(unittest.TestCase):
    """A judge call that hard-fails (a gateway error, not a real verdict)
    must not be indistinguishable from a genuinely clean review. Faults come
    from structural/pacing checks that run independent of the judge, so a
    dead judge and a perfect plan both show "0 faults" - `unreviewed` is the
    only way a caller can tell those apart."""

    def _data(self, n):
        shots = [{"cues": f"{i}-{i}", "asset": f"S01_{i:02d}_SCN_ZI.png"}
                 for i in range(1, n + 1)]
        images = [{"file": s["asset"], "prompt": f"a prompt about scene {i}"}
                 for i, s in enumerate(shots, start=1)]
        return {"shots": shots, "images": images}

    def _cues(self, n):
        return [{"index": i, "start": "00:00:00,000", "end": "00:00:02,000",
                "text": f"cue {i}"} for i in range(1, n + 1)]

    def test_a_total_judge_failure_is_reported_as_fully_unreviewed(self):
        def fake_llm(cfg, prompt, provider=None, temperature=1.0,
                    max_tokens=None):
            raise RuntimeError("Unsupported parameter: 'temperature' is not "
                               "supported with this model.")

        data = self._data(5)
        with mock.patch.object(studio, "llm_generate", fake_llm):
            review = studio.review_shotlist(object(), data, self._cues(5),
                                            None, chunk_size=20)
        self.assertEqual(review["unreviewed"], 5)
        self.assertEqual(review["matched"], 0)
        self.assertEqual(review["weak"], [])  # never graded - not "weak"
        # faults come from structural/pacing checks, computed independent of
        # whether the judge ran at all - a caller must not read an empty
        # fault list here as "the plan is fine" (see test_shotlist_pacing.py
        # for those checks in isolation; this fixture happens to also trip
        # the fragmentation heuristic, which is not what this test is about)
        self.assertIsNotNone(review["error"])

    def test_a_partial_judge_failure_only_marks_its_own_chunk_unreviewed(self):
        calls = {"n": 0}

        def fake_llm(cfg, prompt, provider=None, temperature=1.0,
                    max_tokens=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("Unsupported parameter: 'temperature' is "
                                   "not supported with this model.")
            return ('{"shots": [{"asset": "S01_21_SCN_ZI.png", '
                    '"verdict": "ok"}]}')

        data = self._data(21)  # chunk_size=20 -> two chunks (20 + 1)
        with mock.patch.object(studio, "llm_generate", fake_llm):
            review = studio.review_shotlist(object(), data, self._cues(21),
                                            None, chunk_size=20)
        self.assertEqual(review["unreviewed"], 20)  # the failed first chunk
        self.assertEqual(review["matched"], 1)       # the second chunk's ok
        self.assertEqual(review["weak"], [])

    def test_a_clean_review_reports_zero_unreviewed(self):
        def fake_llm(cfg, prompt, provider=None, temperature=1.0,
                    max_tokens=None):
            return '{"shots": [{"asset": "S01_01_SCN_ZI.png", "verdict": "ok"}]}'

        with mock.patch.object(studio, "llm_generate", fake_llm):
            review = studio.review_shotlist(object(), self._data(1),
                                            self._cues(1), None)
        self.assertEqual(review["unreviewed"], 0)

    def test_no_shots_at_all_reports_zero_unreviewed(self):
        review = studio.review_shotlist(object(), {"shots": [], "images": []},
                                        [], None)
        self.assertEqual(review["unreviewed"], 0)


if __name__ == "__main__":
    unittest.main()
