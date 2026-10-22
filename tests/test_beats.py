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
    def test_unique_two_word_quote_matches(self):
        starts, faults = beats.find_starts(
            [beat("You can"), beat("But crocodile")], make_cues())
        self.assertEqual(starts, [1, 3])
        self.assertEqual(faults, [])

    def test_ambiguous_two_word_quote_is_rejected(self):
        cues = make_cues()
        cues[2]["text"] = "You can also eat something else."
        starts, faults = beats.find_starts([beat("You can")], cues)
        self.assertEqual(starts, [0])
        self.assertTrue(faults)

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
    def test_ai_motion_survives_parse_and_build(self):
        from whisperradar import motion
        raw = [beat("You can sit in a"), beat("But crocodile meat is a", motion="ZO"),
               beat("It is the industry behind")]
        raw[0]["ai_motion"] = "Slow push in; the man turns toward the table."
        parsed = beats.parse(json.dumps({"beats": raw}))
        _, ranges, _ = beats.check(parsed["beats"], make_cues(), ["ZI", "ZO"], 12, .2)
        data, _ = beats.build_shotlist(parsed["beats"], ranges, "flat")
        self.assertEqual(data["shots"][0]["ai_motion"], raw[0]["ai_motion"])
        self.assertNotIn("ai_motion", data["shots"][1])
        faults, _, _ = motion.window_check(data, make_cues(), 8, studio._srt_seconds)
        self.assertEqual(faults, [])

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
    def test_motion_only_fix_keeps_refs_animation_and_overlays(self):
        original = dict(beat("You can sit in a"), refs=["CH_FRIEDA"],
                        ai_motion="Slow push in; she turns.",
                        overlays=[{"text": "$520", "style": "stat"}],
                        transition="CROSSFADE", sfx="pop", land=70)
        fixed = beats.parse_fix(json.dumps({"replace": [{"beat": 1,
            "with": [beat("You can sit in a", motion="ZO")]}]}))
        result = beats.apply_fix([original], fixed)[0]
        for k in ("refs", "ai_motion", "overlays", "transition", "sfx", "land"):
            self.assertEqual(result[k], original[k])
        cleared = beats.parse_fix(json.dumps({"replace": [{"beat": 1,
            "with": [dict(beat("You can sit in a"), refs=[], ai_motion=None)]}]}))
        result = beats.apply_fix([original], cleared)[0]
        self.assertNotIn("refs", result)
        self.assertNotIn("ai_motion", result)

    def test_reference_registry_only_fix_is_usable(self):
        fix = beats.parse_fix(json.dumps({"replace": [], "refs": {"OBJ_CAR": None},
                                         "refPrompts": {"OBJ_CAR": "One blue car."}}))
        self.assertEqual(fix["replace"], [])
        self.assertEqual(fix["refPrompts"]["OBJ_CAR"], "One blue car.")

    def test_shifted_number_does_not_replace_an_unrelated_beat(self):
        old = [beat("You can sit in a"), beat("It is the industry behind")]
        fix = {"replace": [(1, [beat("It is the industry behind")])], "style": ""}
        with self.assertRaises(ValueError):
            beats.apply_fix(old, fix)
        self.assertEqual(old[0]["starts_with"], "You can sit in a")

    def test_explicit_matching_target_allows_correcting_a_bad_quote(self):
        old = [beat("wrong quote here")]
        fix = beats.parse_fix(json.dumps({"replace": [{"beat": 1,
            "original_starts_with": "wrong quote here",
            "with": [beat("You can sit in a")]}]}))
        self.assertEqual(beats.apply_fix(old, fix)[0]["starts_with"], "You can sit in a")

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


class ChannelContextTests(unittest.TestCase):
    def test_real_channel_context_contains_brief_bible_refs_density_and_direction(self):
        from types import SimpleNamespace
        from whisperradar import briefs, external_prompts as ep
        with tempfile.TemporaryDirectory() as folder:
            pdir = Path(folder)
            (pdir / "refs").mkdir()
            (pdir / "refs" / "CH_FRIEDA.png").write_bytes(b"reference")
            ctx = {"pdir": pdir, "eff": {}, "density": "minimal",
                   "visual_style": "Only exact narration numerals may appear.",
                   "bible": "CH_FRIEDA is the supplied presenter; preserve her identity.",
                   "prod": {"stage_extras": json.dumps({"shots": "Keep the desk scene."})}}
            plan = {"profile": briefs.resolve_profile(None), "cues": make_cues(),
                    "total_s": 24, "max_hold": 12, "min_align": .8, "allow_refs": True}
            cfg = SimpleNamespace(studio_manifest_brief="", base_dir=pdir)
            context = ep.planning_context_prompt(cfg, ctx, plan, "See narration below.")
            text = beats.prompt("You can sit in a restaurant.", "Elements sentinel", ctx["visual_style"],
                                ctx["bible"], ["ZI", "ZO"], 12, 30, channel_context=context)
            for expected in ("SECTION 6", "refs/CH_FRIEDA.png", "Keep the desk scene.",
                             "VISUAL DENSITY: MINIMAL", "PACING MATH", "Elements sentinel",
                             "Only exact narration numerals", "BEATS ADAPTER"):
                self.assertIn(expected, text)
            plan["allow_refs"] = False
            disabled = ep.planning_context_prompt(cfg, ctx, plan, "See narration below.")
            self.assertIn("REFERENCES ARE DISABLED", disabled)
            self.assertNotIn("SUPPLIED REFERENCE FILES ALREADY ON DISK", disabled)
            custom = pdir / "custom-brief.md"
            custom.write_text("CUSTOM CHANNEL BRIEF SENTINEL")
            cfg.studio_manifest_brief = str(custom)
            self.assertIn("CUSTOM CHANNEL BRIEF SENTINEL",
                          ep.planning_context_prompt(cfg, ctx, plan, "See narration below."))

    def test_optional_shot_fields_and_reference_metadata_survive_build(self):
        raw = dict(beat("You can sit in a"), refs=["CH_FRIEDA"], land=70,
                   transition="CROSSFADE", sfx="pop", ai_motion="Slow push in.",
                   overlays=[{"text": "$520", "style": "stat", "position": "top-center"}])
        parsed = beats.parse(json.dumps({"beats": [raw], "refs": {"CH_FRIEDA": "refs/CH_FRIEDA.png"}}))
        data, _ = beats.build_shotlist(parsed["beats"], [(1, 6)], "style", parsed, make_cues())
        for k in ("land", "transition", "sfx", "ai_motion", "overlays"):
            self.assertEqual(data["shots"][0][k], raw[k])
        self.assertEqual(data["images"][0]["refs"], ["CH_FRIEDA"])
        self.assertEqual(data["refs"], parsed["refs"])

    def test_reveal_anchors_and_build_up_images_become_normal_shotlist(self):
        b = dict(beat("You can sit in a", motion="ST"), reveal={
            "starts_with": ["You can sit in a", "But crocodile meat is a", "It is the industry behind"],
            "layout": "row", "images": [{"prompt": "A restaurant."},
                                          {"prompt": "A crocodile."}, {"prompt": "A handbag."}]}, sfx="pop")
        _, ranges, faults = beats.check([b], make_cues(), ["ZI", "ZO"], 30, 0,
                                         allow_reveal=True)
        self.assertEqual(faults, [])
        data, _ = beats.build_shotlist([b], ranges, "flat", cues=make_cues())
        self.assertEqual(data["shots"][0]["reveal"]["cues"], [1, 3, 5])
        self.assertEqual(len(data["images"]), 3)
        self.assertEqual(studio.shotlist_structural_faults(data, 6), [])


class RunBeatsTests(unittest.TestCase):
    def test_quality_judge_cannot_replace_its_own_writer_chat(self):
        ws, pdir, asked, saved, t = self._setup([])
        with self.assertRaisesRegex(ws.StageFailed, "different chat"):
            ws.run_beats(None, 1, t, "zai", quality_judge="zai")
        self.assertEqual(asked, [])

    def test_optional_quality_judge_runs_after_code_checks(self):
        raw = [beat("You can sit in a"), beat("But crocodile meat is a"),
               beat("It is the industry behind")]
        verdict = json.dumps({"pass": True, "faults": [], "shots": [], "detailed_ratio": 1.0})
        ws, pdir, asked, saved, t = self._setup([self._reply(raw), verdict])
        ws.run_beats(None, 1, t, "zai", log=lambda m: None, quality_judge="deepseek")
        self.assertEqual(len(asked), 2)
        self.assertEqual(len(saved), 1)
        report = next((pdir / "webchat_debug" / "beats_runs").glob("*/beats_checks_round1.txt"))
        self.assertTrue(json.loads(report.read_text())["quality_verdict"]["pass"])

    def test_quality_rejection_gets_targeted_correction_then_reviewed_again(self):
        raw = [beat("You can sit in a"), beat("But crocodile meat is a"),
               beat("It is the industry behind")]
        bad = json.dumps({"pass": False, "faults": ["S01_01_SCN_ZI.png: missing the restaurant."],
                          "shots": [], "detailed_ratio": .5})
        fix = json.dumps({"replace": [{"beat": 1, "with": [beat("You can sit in a", prompt="A man at a restaurant table.")]}]})
        good = json.dumps({"pass": True, "faults": [], "shots": [], "detailed_ratio": 1.0})
        ws, pdir, asked, saved, t = self._setup([self._reply(raw), bad, fix, good])
        ws.run_beats(None, 1, t, "zai", log=lambda m: None, quality_judge="deepseek")
        self.assertEqual(len(asked), 4)
        self.assertEqual(saved[0]["images"][0]["prompt"], "A man at a restaurant table.")

    def test_full_fault_text_and_all_faults_are_saved_per_run(self):
        raw = [beat("You can sit in a"), beat("But crocodile meat is a"),
               beat("It is the industry behind")]
        ws, pdir, asked, saved, t = self._setup([self._reply(raw), "no JSON", "no JSON"])
        faults = [f"fault {n}: " + "detail " * 50 for n in range(6)]
        with mock.patch.object(ws.ep, "shotlist_judge_prompt", return_value=("", faults)):
            with self.assertRaises(ws.StageFailed):
                ws.run_beats(None, 1, t, "zai", log=lambda m: None)
        report = next((pdir / "webchat_debug" / "beats_runs").glob("*/beats_checks_round1.txt"))
        self.assertEqual(json.loads(report.read_text())["faults"], faults)

    def test_unusable_judge_verdict_preserves_old_shotlist(self):
        raw = [beat("You can sit in a"), beat("But crocodile meat is a"),
               beat("It is the industry behind")]
        ws, pdir, asked, saved, t = self._setup([self._reply(raw), "not JSON", "not JSON"])
        (pdir / "shotlist.json").write_text("old shotlist")
        with self.assertRaises(ws.StageFailed):
            ws.run_beats(None, 1, t, "zai", log=lambda m: None, quality_judge="deepseek")
        self.assertEqual(saved, [])
        self.assertEqual((pdir / "shotlist.json").read_text(), "old shotlist")

    def test_animation_instructions_follow_effective_channel_toggle(self):
        raw = [beat("You can sit in a"), beat("But crocodile meat is a"),
               beat("It is the industry behind")]
        ws, pdir, asked, saved, t = self._setup([self._reply(raw), self._reply(raw)])
        ctx = ws.ep._context.return_value
        ctx["eff"] = {"animate_enabled": False, "animate_seconds": 8}
        ws.run_beats(None, 1, t, "zai", log=lambda m: None)
        self.assertNotIn("ANIMATED OPENING (strict rules", asked[0])
        ctx["eff"]["animate_enabled"] = True
        ws.run_beats(None, 1, t, "zai", log=lambda m: None)
        self.assertIn("ANIMATED OPENING (strict rules", asked[1])
        self.assertIn(TEXTS[1], asked[1])
        self.assertNotIn("cues 1-2 ONLY", asked[1])

    def test_supplied_reference_must_be_attached_before_the_plan_passes(self):
        raw = [beat("You can sit in a", prompt="Frieda at a restaurant."),
               beat("But crocodile meat is a", prompt="Crocodile meat on a plate."),
               beat("It is the industry behind", prompt="A leather handbag.")]
        correction = json.dumps({"replace": [{"beat": 1, "with": [
            dict(raw[0], refs=["CH_FRIEDA"])]}]})
        ws, pdir, asked, saved, t = self._setup([self._reply(raw), correction])
        def checker(cfg, pid, text, files):
            return "", studio.shotlist_structural_faults(json.loads(text), 6)
        with mock.patch.object(ws.studio, "find_supplied_refs", return_value=["CH_FRIEDA.png"]), \
             mock.patch.object(ws.ep, "shotlist_judge_prompt", side_effect=checker):
            ws.run_beats(None, 1, t, "zai", log=lambda m: None)
        self.assertEqual(len(asked), 2)
        self.assertEqual(saved[0]["images"][0]["refs"], ["CH_FRIEDA"])
        self.assertIn("CHANNEL TEST RULES", asked[1])

    def test_references_disabled_does_not_seed_or_attach_supplied_files(self):
        raw = [beat("You can sit in a"), beat("But crocodile meat is a"),
               beat("It is the industry behind")]
        ws, pdir, asked, saved, t = self._setup([self._reply(raw)])
        ws.ep._plan_inputs.return_value["allow_refs"] = False
        with mock.patch.object(ws.studio, "find_supplied_refs", return_value=["CH_FRIEDA.png"]):
            ws.run_beats(None, 1, t, "zai", log=lambda m: None)
        self.assertNotIn("refs", saved[0])
        self.assertTrue(all("refs" not in image for image in saved[0]["images"]))

    def test_references_disabled_rejects_then_removes_writer_reference_fields(self):
        raw = [dict(beat("You can sit in a"), refs=["CH_FRIEDA"]),
               beat("But crocodile meat is a"), beat("It is the industry behind")]
        first = json.dumps({"beats": raw, "refs": {"CH_FRIEDA": "refs/CH_FRIEDA.png"}})
        fix = json.dumps({"replace": [{"beat": 1, "with": [dict(raw[0], refs=[])]}], "refs": {}})
        ws, pdir, asked, saved, t = self._setup([first, fix])
        ws.ep._plan_inputs.return_value["allow_refs"] = False
        ws.run_beats(None, 1, t, "zai", log=lambda m: None)
        self.assertFalse(saved[0].get("refs"))
        self.assertTrue(all(not image.get("refs") for image in saved[0]["images"]))
        self.assertEqual(len(asked), 2)

    def test_supplied_and_generated_references_reach_shotlist_and_saved_beats(self):
        raw = [dict(beat("You can sit in a"), refs=["CH_FRIEDA", "OBJ_CAR"]),
               beat("But crocodile meat is a"), beat("It is the industry behind")]
        reply = json.dumps({"style": "flat", "beats": raw,
                            "refs": {"CH_FRIEDA": None, "OBJ_CAR": None},
                            "refPrompts": {"CH_FRIEDA": "Do not regenerate me.",
                                           "OBJ_CAR": "A blue car."}})
        ws, pdir, asked, saved, t = self._setup([reply])
        with mock.patch.object(ws.studio, "find_supplied_refs", return_value=["CH_FRIEDA.png"]):
            ws.run_beats(None, 1, t, "zai", log=lambda m: None)
        data = saved[0]
        self.assertEqual(data["refs"]["CH_FRIEDA"], "refs/CH_FRIEDA.png")
        self.assertNotIn("CH_FRIEDA", data["refPrompts"])
        self.assertEqual(data["images"][0]["refs"], ["CH_FRIEDA", "OBJ_CAR"])
        stored = json.loads((pdir / "beats.json").read_text())
        self.assertEqual(stored["refs"], data["refs"])
        self.assertEqual(stored["refPrompts"], data["refPrompts"])
        self.assertIn("CHANNEL TEST RULES", asked[0])

    def test_failed_code_checks_never_advance_or_overwrite_old_shotlist(self):
        raw = [beat("You can sit in a"), beat("But crocodile meat is a"),
               beat("It is the industry behind")]
        ws, pdir, asked, saved, t = self._setup([self._reply(raw), "no JSON", "no JSON"])
        (pdir / "shotlist.json").write_text('{"original":true}')
        with mock.patch.object(ws.ep, "shotlist_judge_prompt", side_effect=RuntimeError("checker unavailable")):
            with self.assertRaises(ws.StageFailed):
                ws.run_beats(None, 1, t, "zai", log=lambda m: None)
        self.assertEqual(saved, [])
        self.assertEqual((pdir / "shotlist.json").read_text(), '{"original":true}')

    def test_missing_animation_is_fixed_and_reaches_checker(self):
        from whisperradar import motion
        raw = [beat("You can sit in a"), beat("But crocodile meat is a", motion="ZO"),
               beat("It is the industry behind")]
        repaired = dict(raw[0], ai_motion="Slow push in; the man turns toward the table.")
        fix = json.dumps({"replace": [{"beat": 1, "with": [repaired]}]})
        ws, pdir, asked, saved, t = self._setup([self._reply(raw), fix])
        def checker(cfg, pid, text, files):
            faults, _, _ = motion.window_check(json.loads(text), make_cues(), 8,
                                               studio._srt_seconds)
            return "", faults
        with mock.patch.object(ws.ep, "shotlist_judge_prompt", side_effect=checker):
            ws.run_beats(None, 1, t, "zai", log=lambda m: None)
        self.assertEqual(saved[0]["shots"][0]["ai_motion"], repaired["ai_motion"])
        self.assertIn('"ai_motion"', asked[1])

    def test_last_allowed_fix_is_checked_and_saved(self):
        first = self._reply([beat("You can sit in a"), beat("It is the industry behind")])
        fix = json.dumps({"replace": [{"beat": 1, "with": [
            beat("You can sit in a"), beat("But crocodile meat is a")]}]})
        ws, pdir, asked, saved, t = self._setup([first, fix])
        with mock.patch.object(ws, "BEATS_MAX_ROUNDS", 1):
            ws.run_beats(None, 1, t, "zai", log=lambda m: None)
        self.assertEqual(len(saved), 1)
        self.assertEqual(len(saved[0]["shots"]), 3)

    def _setup(self, replies):
        from whisperradar import webstages as ws
        from whisperradar import briefs
        pdir = Path(tempfile.mkdtemp())
        cues = make_cues(4.0)
        prof = briefs.resolve_profile("custom", custom=json.dumps({"allowed": ["ZI", "ZO", "PU", "PD"]}))
        plan = {"cues": cues, "profile": prof, "max_hold": 12.0, "total_s": 24.0,
                "allow_refs": True, "min_align": .8}
        ctx = {"pdir": pdir, "visual_style": "Flat doodles", "bible": ""}
        asked = []

        class T:
            def ask(self, site, text, files, new_chat=True, ready=None):
                asked.append(text)
                return replies.pop(0)

        saved = []
        for p in (mock.patch.object(ws.ep, "_context", return_value=ctx),
                  mock.patch.object(ws.ep, "_plan_inputs", return_value=plan),
                  mock.patch.object(ws.ep, "planning_context_prompt", return_value="CHANNEL TEST RULES"),
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
