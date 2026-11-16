"""Each image prompt shows what its own cues say (fidelity.py). No network."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import beats, fidelity  # noqa: E402

CUES = [{"index": n, "text": t} for n, t in enumerate([
    "A branch snaps somewhere in the dark.",
    "The deer freeze at the edge of the clearing.",
    "Inside the tent, a flashlight flickers and dies.",
    "The backpack lies open beside the cold fire.",
    "By morning the camp is empty.",
    "Compound interest makes money grow faster every year.",
], 1)]


def plan(*images, shots=None):
    files = [i["file"] for i in images]
    shots = shots or [{"cues": str(n), "asset": f} for n, f in enumerate(files, 1)]
    return {"shots": shots, "images": list(images)}


def img(name, shows, prompt):
    return {"file": name, "shows": shows, "prompt": prompt}


DEER = img("S01_01_SCN_ST.png", ["snapped branch", "dark forest"],
           "A snapped branch on the ground of a dark forest at night.")
FREEZE = img("S01_02_SCN_ZI.png", ["deer frozen mid-step", "edge of the clearing"],
             "Three deer frozen mid-step at the edge of a clearing, ears up.")


class FaultTests(unittest.TestCase):
    def test_a_grounded_plan_has_no_faults(self):
        self.assertEqual(fidelity.faults(plan(DEER, FREEZE), CUES), [])

    def test_an_image_without_shows_is_asked_for_it(self):
        bare = {"file": "S01_01_SCN_ST.png", "prompt": "A snapped branch."}
        got = fidelity.faults(plan(bare), CUES)
        self.assertEqual(len(got), 1)
        self.assertIn("S01_01_SCN_ST.png", got[0])
        self.assertIn('add "shows"', got[0])
        self.assertIn("cue 1", got[0])
        one = fidelity.faults(plan(img("S01_01_SCN_ST.png", ["branch"], "A branch.")), CUES)
        self.assertIn('add "shows"', one[0])                      # one item is too few

    def test_shows_must_be_written_into_the_prompt(self):
        thin = img("S01_02_SCN_ZI.png", ["deer frozen mid-step", "edge of the clearing"],
                   "A quiet woodland scene in soft light.")
        got = fidelity.faults(plan(DEER, thin), CUES)
        self.assertEqual(len(got), 1)
        self.assertIn("does not mention", got[0])
        self.assertIn('"deer frozen mid-step"', got[0])

    def test_a_prompt_about_the_next_cues_is_named_as_drift(self):
        early = img("S01_01_SCN_ST.png", ["deer frozen mid-step", "edge of the clearing"],
                    "Deer frozen mid-step at the edge of a clearing.")
        late = img("S01_02_SCN_ZI.png", ["flashlight", "tent"], "A flashlight inside a tent.")
        got = fidelity.faults(plan(early, late), CUES)
        self.assertEqual(len(got), 2)
        self.assertIn("They match cue 2 (the next shot)", got[0])
        self.assertIn("wrong cues", got[0])
        self.assertNotIn("They match", got[1])                    # no later shot to match

    def test_one_grounded_item_is_enough_so_a_metaphor_passes(self):
        coins = img("S02_01_INF_ZI.png", ["growing stacks of coins", "money growing each year"],
                    "Stacks of coins growing taller left to right, money growing each year.")
        shots = [{"cues": "1-5", "asset": DEER["file"]}, {"cues": "6", "asset": coins["file"]}]
        self.assertEqual(fidelity.faults(plan(DEER, coins, shots=shots), CUES), [])

    def test_a_reused_image_must_fit_the_cues_it_is_reused_over(self):
        shots = [{"cues": "1", "asset": DEER["file"]}, {"cues": "2", "asset": FREEZE["file"]},
                 {"cues": "3-4", "asset": DEER["file"]}, {"cues": "5-6", "asset": FREEZE["file"]}]
        got = fidelity.faults(plan(DEER, FREEZE, shots=shots), CUES)
        self.assertEqual(len(got), 2)
        self.assertIn("is reused at cues 3-4", got[0])
        self.assertIn("their own image", got[0])
        again = [{"cues": "1", "asset": DEER["file"]}, {"cues": "2", "asset": FREEZE["file"]},
                 {"cues": "3-5", "asset": FREEZE["file"]}]
        cues = CUES[:4] + [{"index": 5, "text": "The deer are still at the clearing."}]
        self.assertEqual(fidelity.faults(plan(DEER, FREEZE, shots=again), cues), [])

    def test_sentences_are_not_items_and_the_list_is_capped(self):
        long = img("S01_01_SCN_ST.png", ["a snapped branch lying on the forest floor in the dark with "
                                         "leaves all around it", "dark forest"], "A snapped branch, dark forest.")
        self.assertIn("a few words", fidelity.faults(plan(long), CUES)[0])
        many = [{"file": f"S01_{n:02d}_SCN_ST.png", "prompt": "x"} for n in range(1, 41)]
        cues = [{"index": n, "text": "a branch snaps"} for n in range(1, 41)]
        got = fidelity.faults(plan(*many), cues, limit=5)
        self.assertEqual(len(got), 6)
        self.assertIn("35 more", got[-1])

    def test_numbers_and_plurals_do_not_break_matching(self):
        cues = [{"index": 1, "text": "Three wolves circled 2 tents for 40 minutes."}]
        ok = img("S01_01_SCN_ST.png", ["wolf circling", "two tents"], "A wolf circling two tents at dusk.")
        self.assertEqual(fidelity.faults(plan(ok), cues), [])


class BriefTests(unittest.TestCase):
    def test_the_block_names_the_field_and_the_rule(self):
        block = fidelity.brief_block()
        self.assertIn('"shows"', block)
        self.assertIn("narration's own words", block)
        self.assertLess(len(block), 1200)

    def test_a_beat_carries_its_shows_onto_the_image(self):
        import inspect
        self.assertIn('image["shows"] = b["shows"]', inspect.getsource(beats.build_shotlist))


class WiringTests(unittest.TestCase):
    """Off by default; with studio.prompt_fidelity the planner is told about
    "shows" and the app's own checks include the fidelity faults."""

    def setUp(self):
        from tests import test_external_prompts as base
        self.base = base.Base("setUp")
        self.base.setUp()
        self.addCleanup(self.base.tearDown)
        self.cfg, self.pid, self.ep = self.base.cfg, self.base.pid, base.ep
        cues = base.studio.parse_srt_cues(base.SRT)
        self.n = len(cues)
        words = cues[0]["text"].split()
        self.good = {"style": "flat", "shots": [{"cues": f"1-{self.n}", "asset": "S01_01_SCN_ZI.png", "scene": "S01",
                                                 "motion": "ZI"}],
                     "images": [{"file": "S01_01_SCN_ZI.png", "shows": [" ".join(words[:3]), " ".join(words[-3:])],
                                 "prompt": "A picture of " + cues[0]["text"]}]}

    def local(self, data):
        import json
        return self.ep.shotlist_judge_prompt(self.cfg, self.pid, json.dumps(data), [])[1]

    def test_off_by_default(self):
        self.assertFalse(self.cfg.studio_prompt_fidelity)
        self.assertNotIn('"shows"', self.ep.shotlist_planner_prompt(self.cfg, self.pid))
        bare = {**self.good, "images": [{"file": "S01_01_SCN_ZI.png", "prompt": "Anything at all."}]}
        self.assertFalse([f for f in self.local(bare) if "shows" in f])

    def test_on_the_planner_is_told_and_the_checks_run(self):
        self.cfg.studio_prompt_fidelity = True
        self.assertIn("EACH IMAGE SHOWS WHAT ITS OWN CUES SAY", self.ep.shotlist_planner_prompt(self.cfg, self.pid))
        self.assertFalse([f for f in self.local(self.good) if "shows" in f])
        bare = {**self.good, "images": [{"file": "S01_01_SCN_ZI.png", "prompt": "Anything at all."}]}
        got = [f for f in self.local(bare) if "shows" in f]
        self.assertEqual(len(got), 1)
        self.assertIn("S01_01_SCN_ZI.png", got[0])


if __name__ == "__main__":
    unittest.main()
