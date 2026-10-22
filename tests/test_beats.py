import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whisperradar import beats, studio  # noqa: E402

TEXTS = [
    "You can sit in a restaurant in Louisiana and",          # 1
    "order alligator for dinner, not as survival food.",     # 2
    "But crocodile meat is a different story entirely,",     # 3
    "and the reason is not the taste.",                      # 4
    "It is the industry behind the leather handbag.",        # 5
    "That industry decided what ends up on a plate.",        # 6
]


def make_cues(secs=4.0):
    out = []
    for i, t in enumerate(TEXTS):
        a, b = i * secs, (i + 1) * secs
        out.append({"index": i + 1, "start": f"00:00:{a:06.3f}".replace(".", ","),
                    "end": f"00:00:{b:06.3f}".replace(".", ","), "text": t})
    return out


def beat(sw, scene=1, typ="SCN", motion="ZI", prompt="A drawing."):
    return {"starts_with": sw, "scene": scene, "type": typ, "motion": motion, "prompt": prompt}


class ParseTests(unittest.TestCase):
    def test_complete_object_is_read(self):
        reply = "json\nCopy\n```json\n" + json.dumps({"style": "flat", "beats": [
            {"starts_with": "You can sit in a", "scene": 1, "type": "scn", "motion": "zi", "prompt": "A man."},
            {"starts_with": "", "prompt": "no start"},
            {"starts_with": "But crocodile meat is", "prompt": "Meat.", "type": "XYZ"},
        ]}) + "\n```"
        got = beats.parse(reply)
        self.assertEqual(got["style"], "flat")
        self.assertEqual(len(got["beats"]), 2)
        self.assertEqual(got["beats"][0]["type"], "SCN")
        self.assertEqual(got["beats"][0]["motion"], "ZI")
        self.assertEqual(got["beats"][1]["type"], "SCN")        # unknown type -> SCN
        self.assertEqual(got["beats"][1]["scene"], 1)           # missing scene -> previous

    def test_cut_off_reply_is_not_a_result(self):
        cut = '```json\n{"style": "s", "beats": [{"starts_with": "a b c", "prompt": "p"}, {"starts_with": "d e'
        self.assertIsNone(beats.parse(cut))


class MappingTests(unittest.TestCase):
    def test_starts_are_found_in_order_across_cue_lines(self):
        cues = make_cues()
        bl = [beat("You can sit in a restaurant"), beat("order alligator for dinner"),
              beat("But crocodile meat is a"), beat("It is the industry behind")]
        starts, f = beats.find_starts(bl, cues)
        self.assertEqual(starts, [1, 2, 3, 5])
        self.assertEqual(f, [])
        ranges, f2 = beats.cue_ranges(starts, len(cues))
        self.assertEqual(ranges, [(1, 1), (2, 2), (3, 4), (5, 6)])   # every cue once
        self.assertEqual(f2, [])

    def test_first_beat_always_starts_at_cue_one(self):
        cues = make_cues()
        ranges, _ = beats.cue_ranges([3, 5], len(cues))
        self.assertEqual(ranges[0][0], 1)

    def test_a_quote_inside_a_cue_snaps_to_that_cue(self):
        cues = make_cues()
        starts, f = beats.find_starts([beat("You can sit in a"), beat("not as survival food but")], cues)
        self.assertEqual(starts, [1, 2])
        self.assertEqual(f, [])

    def test_unknown_quote_is_a_fault_naming_the_beat(self):
        starts, f = beats.find_starts([beat("You can sit in a"), beat("this is never said anywhere")], make_cues())
        self.assertEqual(starts[1], 0)
        self.assertIn("beat 2", f[0])

    def test_two_beats_in_one_cue_are_a_fault(self):
        cues = make_cues()
        starts, _ = beats.find_starts([beat("You can sit in a"), beat("restaurant in Louisiana and"),
                                       beat("order alligator for dinner")], cues)
        _, f = beats.cue_ranges(starts, len(cues))
        self.assertEqual(len(f), 1)
        self.assertIn("beat 1 and beat 2", f[0])

    def test_words_are_compared_without_case_or_punctuation(self):
        starts, f = beats.find_starts([beat("YOU CAN sit, in a"), beat("But crocodile-meat is a")], make_cues())
        self.assertEqual(f, [])
        self.assertEqual(starts, [1, 3])


class HoldAndBuildTests(unittest.TestCase):
    def test_hold_fault_names_beat_and_real_limit(self):
        cues = make_cues(4.0)
        bl = [beat("You can sit in a"), beat("It is the industry behind")]
        starts, ranges, faults = beats.check(bl, cues, ["ZI", "ZO"], 12, 0.2)
        self.assertEqual(ranges, [(1, 4), (5, 6)])
        self.assertEqual(len(faults), 1)                       # 16s > 14.4s
        self.assertIn("beat 1", faults[0])
        self.assertIn("14.4s", faults[0])

    def test_hold_within_tolerance_is_not_a_fault(self):
        cues = make_cues(4.5)       # 3 cues = 13.5s <= 14.4s
        bl = [beat("You can sit in a"), beat("and the reason is not")]
        _, _, faults = beats.check(bl, cues, ["ZI"], 12, 0.2)
        self.assertEqual(faults, [])

    def test_bad_motion_code_is_a_fault(self):
        _, _, faults = beats.check([beat("You can sit in a", motion="ST")], make_cues(1.0), ["ZI", "ZO"], 12, 0.2)
        self.assertTrue(any("motion" in f for f in faults))

    def test_built_shotlist_passes_the_structural_checks(self):
        cues = make_cues()
        bl = [beat("You can sit in a", prompt="A man in a restaurant."),
              beat("But crocodile meat is a", scene=2, motion="ZO", prompt="Crocodile meat on a plate."),
              beat("It is the industry behind", scene=2, motion="PU", prompt="A leather handbag.")]
        starts, ranges, faults = beats.check(bl, cues, ["ZI", "ZO", "PU"], 12, 0.2)
        self.assertEqual(faults, [])
        data, names = beats.build_shotlist(bl, ranges, "flat")
        self.assertEqual(names, ["S01_01_SCN_ZI.png", "S02_01_SCN_ZO.png", "S02_02_SCN_PU.png"])
        self.assertEqual([s["cues"] for s in data["shots"]], ["1-2", "3-4", "5-6"])
        self.assertEqual(studio.shotlist_structural_faults(data, len(cues)), [])


class FixTests(unittest.TestCase):
    def test_parse_and_apply_split(self):
        reply = '```json\n{"replace": [{"beat": 1, "with": [' \
                '{"starts_with": "You can sit in a", "prompt": "A"},' \
                '{"starts_with": "and the reason is not", "prompt": "B"}]}], "style": "short"}\n```'
        fix = beats.parse_fix(reply)
        self.assertEqual(fix["style"], "short")
        old = [beat("You can sit in a", prompt="old"), beat("It is the industry behind")]
        new = beats.apply_fix(old, fix)
        self.assertEqual([b["prompt"] for b in new][:2], ["A", "B"])
        self.assertEqual(len(new), 3)

    def test_several_replacements_use_the_original_numbers(self):
        old = [beat("a b c", prompt="1"), beat("d e f", prompt="2"), beat("g h i", prompt="3")]
        fix = {"replace": [(1, [beat("a b c", prompt="1a"), beat("x y z", prompt="1b")]),
                           (3, [beat("g h i", prompt="3a")])], "style": ""}
        new = beats.apply_fix(old, fix)
        self.assertEqual([b["prompt"] for b in new], ["1a", "1b", "2", "3a"])

    def test_fix_prompt_shows_only_the_beats_named(self):
        cues = make_cues(4.0)
        bl = [beat("You can sit in a"), beat("It is the industry behind")]
        _, ranges, faults = beats.check(bl, cues, ["ZI"], 12, 0.2)
        _, names = beats.build_shotlist(bl, ranges, "s")
        text = beats.fix_prompt(faults, bl, ranges, cues, names, ["ZI"], 12)
        self.assertIn("beat 1 | cues 1-4", text)
        self.assertNotIn("beat 2 |", text)
        self.assertIn("order alligator for dinner", text)       # the narration to split


class RunBeatsTests(unittest.TestCase):
    def _setup(self, replies):
        from whisperradar import webstages as ws
        from whisperradar import briefs
        pdir = Path(tempfile.mkdtemp())
        cues = make_cues(4.0)
        prof = briefs.resolve_profile("custom", custom=json.dumps({"allowed": ["ZI", "ZO", "PU", "PD"]}))
        plan = {"cues": cues, "profile": prof, "max_hold": 12.0, "total_s": 24.0}
        ctx = {"pdir": pdir, "visual_style": "Flat doodles", "bible": ""}
        asked = []

        class T:
            def ask(self, site, text, files, new_chat=True, ready=None):
                asked.append(text)
                return replies.pop(0)

        saved = []
        for p in (mock.patch.object(ws.ep, "_context", return_value=ctx),
                  mock.patch.object(ws.ep, "_plan_inputs", return_value=plan),
                  mock.patch.object(ws.ep, "shotlist_judge_prompt", return_value=("", [])),
                  mock.patch.object(ws, "_dump"),
                  mock.patch.object(ws, "save_shotlist",
                                    side_effect=lambda cfg, pid, data, detail, advance: saved.append(data))):
            p.start()
            self.addCleanup(p.stop)
        return ws, pdir, asked, saved, T()

    def _reply(self, items):
        return "```json\n" + json.dumps({"style": "flat", "beats": items}) + "\n```"

    def test_long_beat_is_split_by_a_small_fix_and_then_saved(self):
        first = self._reply([
            {"starts_with": "You can sit in a", "scene": 1, "type": "SCN", "motion": "ZI", "prompt": "A man."},
            {"starts_with": "It is the industry behind", "scene": 1, "type": "SCN", "motion": "ZO", "prompt": "Handbag."}])
        fix = '```json\n{"replace": [{"beat": 1, "with": [' \
              '{"starts_with": "You can sit in a", "motion": "ZI", "prompt": "A man."},' \
              '{"starts_with": "But crocodile meat is a", "motion": "PU", "prompt": "A mouth."}]}]}\n```'
        ws, pdir, asked, saved, t = self._setup([first, fix])
        data = ws.run_beats(None, 1, t, "zai", log=lambda m: None)
        self.assertEqual(len(asked), 2)                         # plan + one fix
        self.assertEqual([s["cues"] for s in saved[0]["shots"]], ["1-2", "3-4", "5-6"])
        self.assertTrue((pdir / "beats.json").exists())
        self.assertEqual(len(data["shots"]), 3)

    def test_clean_plan_is_saved_in_one_round(self):
        ok = self._reply([
            {"starts_with": "You can sit in a", "scene": 1, "type": "SCN", "motion": "ZI", "prompt": "A man."},
            {"starts_with": "But crocodile meat is a", "scene": 1, "type": "SCN", "motion": "ZO", "prompt": "Meat."},
            {"starts_with": "It is the industry behind", "scene": 2, "type": "SCN", "motion": "PU", "prompt": "Bag."}])
        ws, pdir, asked, saved, t = self._setup([ok])
        ws.run_beats(None, 1, t, "zai", log=lambda m: None)
        self.assertEqual(len(asked), 1)
        self.assertEqual(len(saved), 1)

    def test_a_plan_that_never_passes_keeps_the_old_shotlist(self):
        bad = self._reply([
            {"starts_with": "You can sit in a", "scene": 1, "type": "SCN", "motion": "ZI", "prompt": "A man."},
            {"starts_with": "never said anywhere at all", "scene": 1, "type": "SCN", "motion": "ZO", "prompt": "x"}])
        nofix = "sorry, I cannot"
        ws, pdir, asked, saved, t = self._setup([bad, nofix, nofix])
        (pdir / "shotlist.json").write_text('{"old": true}', encoding="utf-8")
        with self.assertRaises(ws.StageFailed):
            ws.run_beats(None, 1, t, "zai", log=lambda m: None)
        self.assertEqual(saved, [])
        self.assertEqual((pdir / "shotlist.json").read_text(encoding="utf-8"), '{"old": true}')


if __name__ == "__main__":
    unittest.main()
