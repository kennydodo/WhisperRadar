"""Prompts show the action their cues narrate and nothing unrelated: the writer
brief (both templates, and the skeleton picture writer) says so, the judge
checks it, and the free local check catches filler gestures. No network."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import briefs, external_prompts, fidelity, skeleton, studio  # noqa: E402

COMPACT = ROOT / "whisperradar" / "brief_template_compact.md"


class WriterBriefTests(unittest.TestCase):
    def templates(self):
        return {"standard": briefs.read_template(), "compact": briefs.read_template(COMPACT)}

    def test_both_templates_carry_section_11d_before_the_checklist(self):
        for name, text in self.templates().items():
            with self.subTest(template=name):
                self.assertIn("## SECTION 11D", text)
                self.assertLess(text.index("## SECTION 11C"), text.index("## SECTION 11D"))
                self.assertLess(text.index("## SECTION 11D"), text.index("## SECTION 12"))
                body = skeleton._section("11D", text)
                for phrase in ("Core rule:", "Figurative language", "Charts support the scene", "invent activity",
                               "planned", "hypothetical", "recurring"):
                    self.assertIn(phrase, body)
                self.assertNotIn("\n---\n", text)            # a '---' line would cut the brief

    def test_the_checklist_names_it(self):
        for name, text in self.templates().items():
            with self.subTest(template=name):
                self.assertIn("(Section 11D)", text[text.index("## SECTION 12"):])

    def test_every_profile_renders_it(self):
        profiles = [v for v in vars(briefs).values() if isinstance(v, briefs.MotionProfile)]
        for name, text in self.templates().items():
            for p in profiles:
                with self.subTest(template=name, profile=p.key):
                    out = briefs.render_brief(text.strip(), p, "A host opens the video.")
                    self.assertIn("SECTION 11D", out)


class JudgeTests(unittest.TestCase):
    def test_built_in_judge_checks_action_and_relevance(self):
        text = studio.alignment_prompt(
            [{"asset": "S01_01_SCN_ST.png", "cues": "1-1", "prompt": "A woman stands by a table."}],
            {1: "She pays the electricity bill."})
        for phrase in ("MEANING, ACTION AND RELEVANCE", "UNRELATED:", "WRONG ACTION:", "METAPHOR:",
                       "GRAPHIC DOMINATES:", "complete thought" if False else "whole thought",
                       "planned or hypothetical", "Do NOT require somebody", "is NOT \"ok\""):
            self.assertIn(phrase, text)
        self.assertIn("nothing in the prompt is unrelated or invented", text)

    def test_external_judge_inherits_the_rule(self):
        rules = external_prompts._alignment_rules("Flat cartoon style.")
        self.assertIn("MEANING, ACTION AND RELEVANCE", rules)
        self.assertIn("WRONG ACTION:", rules)

    def test_unrelated_items_and_wrong_actions_reach_the_fix_request(self):
        # judge reply -> weak list -> the rewrite request the writer gets
        reply = ('{"shots": [{"asset": "A.png", "verdict": "weak", "missing": '
                 '["UNRELATED: a jar of coins on the table", "WRONG ACTION: she looks at the bill, '
                 'the narration says she pays it"], "reason": "extra prop, wrong action"}]}')
        shots = [{"asset": "A.png", "cues": "1", "prompt": "A woman looks at a bill beside a jar of coins."}]
        with mock.patch.object(studio, "llm_generate", return_value=reply):
            res = studio._judge_shot_chunks(cfg=None, shots=shots, cue_text={1: "She pays the bill."},
                                            provider="p", chunk_size=20, style_guide="", temperature=1.0)
        self.assertEqual(res["matched"], 0)
        weak = res["weak"]
        self.assertEqual(weak[0]["verdict"], "weak")
        self.assertIn("UNRELATED: a jar of coins", weak[0]["missing"][0])
        request = studio.shotlist_patch_prompt(weak, style_guide="Flat cartoon")
        self.assertIn("UNRELATED: a jar of coins on the table", request)
        self.assertIn("WRONG ACTION: she looks at the bill", request)


class FillerCheckTests(unittest.TestCase):
    CUES = [{"index": 1, "text": "She pays the electricity bill on her phone."},
            {"index": 2, "text": "Then she waves goodbye to the neighbour."}]

    def plan(self, prompt, cue="1"):
        return {"shots": [{"cues": cue, "asset": "S01_01_SCN_ST.png"}],
                "images": [{"file": "S01_01_SCN_ST.png", "prompt": prompt,
                            "shows": ["electricity bill", "her phone"]}]}

    def test_a_filler_gesture_the_cues_never_mention_is_a_fault(self):
        got = fidelity.faults(self.plan("A woman waves and points at the electricity bill on her phone."), self.CUES)
        self.assertEqual(len(got), 1, got)
        self.assertIn("waving and pointing", got[0])
        self.assertIn("S01_01_SCN_ST.png", got[0])
        self.assertIn("cue 1", got[0])

    def test_the_gesture_is_fine_when_the_cues_narrate_it(self):
        plan = self.plan("A woman waves goodbye to her neighbour at the door.", cue="2")
        plan["images"][0]["shows"] = ["woman waves goodbye", "neighbour"]
        self.assertEqual(fidelity.faults(plan, self.CUES), [])

    def test_nouns_and_idioms_are_not_flagged(self):
        self.assertEqual(fidelity.filler_gestures("A data point on a chart; a starting point", "x"), [])
        self.assertEqual(fidelity.faults(
            self.plan("A woman taps pay on the electricity bill on her phone, a data point on a graph beside her."),
            self.CUES), [])

    def test_the_brief_block_says_it_too(self):
        self.assertIn("filler gesture", fidelity.brief_block())


if __name__ == "__main__":
    unittest.main()


class FillerPrecisionTests(unittest.TestCase):
    def test_not_character_gestures(self):
        from whisperradar import fidelity
        for p in ("Ocean waves breaking against rocks",
                  "An arrow pointing to the growing balance",
                  "No waving, no pointing, no thumbs-up.",
                  "Avoid gesturing at the viewer"):
            self.assertEqual(fidelity.filler_gestures(p, "a plain cue"), [], p)

    def test_character_gestures_still_flagged(self):
        from whisperradar import fidelity
        self.assertEqual(fidelity.filler_gestures("Joe waves at the camera", "x"), ["waving"])
        self.assertEqual(fidelity.filler_gestures("She points to the chart", "x"), ["pointing"])

    def test_required_character_exception_in_prompts(self):
        from whisperradar import studio
        t = open("whisperradar/brief_template.md", encoding="utf-8").read()
        self.assertIn("presenter or story protagonist", t)
        self.assertIn("presenter or lead", open("whisperradar/brief_template_compact.md", encoding="utf-8").read())
        self.assertIn("recurring story protagonist", studio.alignment_prompt([{"asset": "S01_01_SCN_ZI.png", "cues": "1-1", "prompt": "x"}], {1: "y"}))


class NoMetaphorTests(unittest.TestCase):
    def test_both_briefs_ban_metaphor_and_do_not_ask_for_it(self):
        for f in ("whisperradar/brief_template.md", "whisperradar/brief_template_compact.md"):
            t = open(f, encoding="utf-8").read()
            self.assertIn("Figurative language", t, f)
            self.assertNotIn("focal object, metaphor", t, f)
            self.assertNotIn("and visual metaphor", t, f)
            self.assertNotIn("grouping and metaphor", t, f)
        self.assertNotIn("focal object, metaphor", open("whisperradar/skeleton.py", encoding="utf-8").read())

    def test_judge_flags_metaphor(self):
        t = open("whisperradar/studio.py", encoding="utf-8").read()
        self.assertIn("METAPHOR:", t)
        self.assertIn("giant bars", t)

    def test_fidelity_asks_for_literal_items(self):
        t = open("whisperradar/fidelity.py", encoding="utf-8").read()
        self.assertNotIn("a metaphor may fill", t)
        self.assertNotIn("may name what stands for the idea", t)


class OlderGuidanceAndCustomTests(unittest.TestCase):
    def test_no_stand_in_mechanism_or_metaphor_guidance_remains(self):
        for f in ("whisperradar/brief_template.md", "whisperradar/brief_template_compact.md",
                  "whisperradar/skeleton.py", "whisperradar/briefs.py"):
            t = open(f, encoding="utf-8").read()
            for bad in ("growing stacks", "stacks growing", "self-feeding", "and metaphor.",
                        "visual metaphor"):
                self.assertNotIn(bad, t, (f, bad))

    def test_literal_objects_are_kept_when_narrated(self):
        for f in ("whisperradar/brief_template.md", "whisperradar/brief_template_compact.md"):
            t = open(f, encoding="utf-8").read()
            self.assertIn("as substitutes for financial concepts", t, f)
            self.assertIn("when the narration genuinely describes them", t, f)
            self.assertNotIn("No pipes", t, f)
            self.assertNotIn("No plumbing", t, f)

    def test_planner_keeps_a_custom_11d_and_shortens_only_the_bundled_ones(self):
        std = open("whisperradar/brief_template.md", encoding="utf-8").read()
        short = skeleton._section("11D", open("whisperradar/brief_template_compact.md", encoding="utf-8").read())
        self.assertEqual(skeleton._planner_11d(std), short)          # bundled standard -> short
        custom = std.replace("## SECTION 11D", "## SECTION 11D").replace(
            "Core rule:", "House rule: our own wording.")
        out = skeleton._planner_11d(custom)
        self.assertIn("House rule: our own wording.", out)
        self.assertEqual(out, skeleton._section("11D", custom))
        self.assertEqual(skeleton._planner_11d("## SECTION 12 — X\n\nno 11D here"), "")
        rules = skeleton.rules_text(briefs.get_profile("standard"), True, ["MAYA.png"], template_text=custom)
        self.assertIn("House rule: our own wording.", rules)


class PromptHygieneTests(unittest.TestCase):
    """The seven prompt-hygiene rules: written into both briefs, checked by the judge."""

    def test_both_templates_state_the_hygiene_rules(self):
        import re
        for name, text in {"standard": briefs.read_template(),
                           "compact": briefs.read_template(COMPACT)}.items():
            body = skeleton._section("11D", text)
            with self.subTest(template=name):
                for phrase in ("communicates", "phone timer", "no readable text", "never drop a later"
                               if name == "compact" else "Never drop a later", "CU shot", "background that helps explain" if name == "standard" else "helpful background",
                               "look-alike" if name == "standard" else "third party", "engine's character budget"):
                    self.assertIn(phrase, body)
                self.assertIsNone(re.search(r"\b\d+\s*(?:-|to)\s*\d+\s+words\b", body))
                self.assertNotIn("first beat is shown", body)

    def test_judge_has_a_prefix_for_each_rule(self):
        text = studio.alignment_prompt(
            [{"asset": "S01_01_CU_ZI.png", "cues": "1-1", "prompt": "A woman holds a mug."}],
            {1: "She carries a mug."})
        for prefix in ("COMMENTARY:", "STAND-IN PROP:", "MULTI-ACTION:", "FRAMING:",
                       "VIEWER-FACING:", "WRONG SUBJECT:"):
            self.assertIn(prefix, text)
        self.assertNotIn("presentation instructions require a presenter", text)
        self.assertIn("WHO IS ON SCREEN:", text)
        for phrase in ("a phone timer or a kitchen timer is a legitimate timer", "no readable text",
                       "drops a later narrated action", "background that helps explain the action"):
            self.assertIn(phrase, text)

    def test_external_judge_inherits_them(self):
        rules = external_prompts._alignment_rules("Flat cartoon style.")
        self.assertIn("PROMPT HYGIENE", rules)

    def test_planner_rules_carry_them_and_stay_small(self):
        text = skeleton._planner_11d(briefs.read_template(COMPACT))
        self.assertIn("phone timer", text)


class JudgePassTests(unittest.TestCase):
    def test_a_weak_shot_is_never_accepted_and_goes_back_to_be_fixed(self):
        import inspect
        from whisperradar import webstages
        text = studio.alignment_prompt(
            [{"asset": "S01_01_SCN_ZI.png", "cues": "1-1", "prompt": "A woman stands by a table."}],
            {1: "She pays the electricity bill."})
        self.assertIn('the verdict is then "missing"', text)
        self.assertIn('"shots" is empty', inspect.getsource(external_prompts.shotlist_judge_prompt))
        self.assertIn("not hard and not weak", inspect.getsource(webstages))


class PriorityTests(unittest.TestCase):
    def test_priorities_are_in_both_briefs_and_the_judge(self):
        for name, text in {"standard": briefs.read_template(),
                           "compact": briefs.read_template(COMPACT)}.items():
            with self.subTest(template=name):
                body = skeleton._section("11D", text)
                self.assertIn("Priorities, in order", body)
                self.assertIn("one coherent visible moment", body)
                self.assertIn("never dominate" if name == "compact" else "without dominating", body)
        judge = studio.alignment_prompt(
            [{"asset": "S01_01_SCN_ZI.png", "cues": "1-1", "prompt": "A woman stands by a table."}],
            {1: "She pays the electricity bill."})
        self.assertIn("ORDER OF CHECKS", judge)
        self.assertIn("do not treat brevity or length as a fault", judge)


class FollowupWordingTests(unittest.TestCase):
    def test_followup_does_not_name_a_file_that_may_not_exist(self):
        from whisperradar import webstages
        inline = webstages._plan_followup('{"images": []}', None)
        files = []
        attached = webstages._plan_followup('{"images": []}', files)
        for text in (inline, attached):
            self.assertIn("original narration supplied earlier in this chat", text)
            self.assertNotIn("narration.txt", text)
        self.assertEqual([f["name"] for f in files], ["shotlist.json"])


class ImagineCueTests(unittest.TestCase):
    def test_imagine_cues_show_the_imagined_moment(self):
        for name, text in {"standard": briefs.read_template(),
                           "compact": briefs.read_template(COMPACT)}.items():
            with self.subTest(template=name):
                body = skeleton._section("11D", text)
                self.assertIn("imagine", body.lower())
                self.assertIn('"you"', body)
                self.assertIn("presenter", body)
        judge = studio.alignment_prompt(
            [{"asset": "S01_01_CU_ZI.png", "cues": "1-1", "prompt": "A woman holds a phone."}],
            {1: "Picture checking your balance."})
        self.assertIn("IMAGINE CUES", judge)
        self.assertIn("imagined moment itself", judge)


class PresenterDefaultTests(unittest.TestCase):
    def test_briefs_and_judge_make_the_presenter_the_default(self):
        for name, text in {"standard": briefs.read_template(),
                           "compact": briefs.read_template(COMPACT)}.items():
            with self.subTest(template=name):
                body = skeleton._section("11D", text)
                self.assertIn("Who is on screen", body)
                self.assertIn("presenter", body)
                self.assertNotIn("never drawn", body)
                self.assertNotIn("narrator who only speaks", body)
                for word in ("advice", "conclusions"):
                    self.assertIn(word, body)
        judge = studio.alignment_prompt(
            [{"asset": "S01_01_SCN_ZI.png", "cues": "1-1", "prompt": "A woman holds a phone."}],
            {1: "You open your banking app."})
        self.assertIn("WHO IS ON SCREEN:", judge)
        self.assertIn("WRONG SUBJECT:", judge)
        self.assertNotIn("NARRATOR ON SCREEN", judge)
        self.assertNotIn("never drawn in the scenes", judge)
        self.assertIn("Not every opening", judge)
        self.assertIn("share a scene only when the narration supports it", judge)
        for t in (briefs.read_template(), briefs.read_template(COMPACT)):
            self.assertIn("only when the narration supports it", skeleton._section("11D", t))


class PresenterReferenceTests(unittest.TestCase):
    def test_reference_rules_cast_the_presenter_and_keep_refs_exclusive(self):
        std = briefs.read_template()
        com = briefs.read_template(COMPACT)
        for name, t in {"standard": std, "compact": com}.items():
            with self.subTest(template=name):
                self.assertNotIn("attached to no image", t)
                self.assertNotIn("never cast", t)
                self.assertIn("default person on screen", t)
                self.assertIn("never", t.split("default person on screen")[0][-400:] + t)
        self.assertIn("never on anyone", open("whisperradar/skeleton.py", encoding="utf-8").read())
        src = open("whisperradar/external_prompts.py", encoding="utf-8").read()
        self.assertIn("attached only to images that", src)
        self.assertNotIn("except a narrator/presenter reference", src)


class ActionPlanTests(unittest.TestCase):
    def test_brief_requires_an_action_and_a_judge_only_note(self):
        std = briefs.read_template()
        self.assertIn("`action`", std)
        self.assertIn("for the judge only", std)
        self.assertIn("Do not invent activity to satisfy an action requirement", std)
        self.assertIn("directly depicts the narrated task", std)
        self.assertNotIn("not an everyday action by itself", std)
        com = briefs.read_template(COMPACT)
        self.assertIn("only when it is the narrated task", com)
        self.assertIn("Never invent activity", com)
        self.assertIn('"action"', com)
        self.assertIn('"note"', com)
        self.assertIn("for the judge only", com)

    def test_judge_gets_action_and_note_and_has_a_no_action_fault(self):
        shot = {"asset": "S01_01_SCN_ZI.png", "cues": "1-1",
                "prompt": "A woman reads a statement.",
                "action": "walks to the door and taps Pay on her phone",
                "note": "Maya, actual event, her flat entrance"}
        text = studio.alignment_prompt([shot], {1: "She pays the bill."})
        self.assertIn('planned action: "walks to the door', text)
        self.assertIn("note for you only", text)
        self.assertIn("NO ACTION:", text)
        self.assertIn("first check them against the narration", text)
        self.assertIn("Do not demand activity", text)
        plain = studio.alignment_prompt(
            [{"asset": "a_ZI.png", "cues": "1-1", "prompt": "x"}], {1: "y"})
        self.assertNotIn("planned action:", plain)

    def test_cue_table_has_the_five_columns(self):
        data = {"shots": [{"cues": "1-2", "asset": "S01_01_SCN_ZI.png",
                           "action": "cooks | dinner", "note": "Maya, actual"}]}
        table = studio.cue_table_text(data, {1: "She cooks.", 2: "Then eats."})
        self.assertIn("| Shot | Cues | Narration | Action | Note |", table)
        self.assertIn("| S01_01_SCN_ZI.png | 1-2 | She cooks. Then eats. | "
                      "cooks / dinner | Maya, actual |", table)
