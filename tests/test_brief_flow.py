"""The writer never sees the original; the judge sees it for tone/style only."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_external_prompts import Base, SOURCE  # noqa: E402
from tests.test_webstages import Fake, words  # noqa: E402
from whisperradar import (autorun, external_prompts as ep, plan as pp,  # noqa: E402
                          studio, webstages as ws)

BRIEF = ("BRIEF: What hides in an old coin jar and how to check it.\n"
         "KEYWORDS: coin jar, rare coins, mint mark, 1943 penny, coin value")


class PlanSeesTheOriginalScript(Base):
    def setUp(self):
        super().setUp()
        autorun.save_brief(self.pdir, BRIEF, SOURCE)

    def test_plan_prompts_carry_brief_and_the_original_script(self):
        ctx = pp.context(self.cfg, self.pid)
        self.assertIn("rare coins", ctx["brief"])
        plan = pp.parse_plan({"keyword": "coin jar", "title": "t",
                              "titles": [], "promise": "p", "hook": "h",
                              "thumbnail": {"text": "x"}})
        for text in (pp.writer_prompt(ctx), pp.judge_prompt(ctx, plan, [])):
            self.assertIn("rare coins", text)
            # titles replicate the source, so the plan stage reads its script
            self.assertIn(SOURCE[:80], text)

    def test_missing_brief_is_asked_from_the_judge_chat(self):
        (self.pdir / "research_notes.json").unlink()
        (self.pdir / "research_notes.md").unlink()
        t = Fake(deepseek=[BRIEF])
        brief, built = ws.ensure_brief(self.cfg, self.pid, t, "deepseek",
                                       lambda m: None)
        self.assertTrue(built)
        self.assertEqual([c["site"] for c in t.calls], ["deepseek"])
        again, built2 = ws.ensure_brief(self.cfg, self.pid, t, "deepseek",
                                        lambda m: None)
        self.assertFalse(built2)
        self.assertEqual(again, brief)


class PlanNumbers(unittest.TestCase):
    def test_same_list_number_is_allowed_now(self):
        def plan(title):
            return pp.parse_plan({"keyword": "coin jar", "title": title,
                                  "titles": [{"text": title, "why": "x"}] * 6,
                                  "promise": "p", "hook": "h",
                                  "thumbnail": {"text": "WOW"}})
        src = "10 Coin Jar Mistakes That Cost You Money"
        same = pp.originality_faults(plan("Ten coin jar errors to avoid"), src)
        self.assertEqual(same, [])
        self.assertEqual(pp.list_numbers("In 1943 there were 12 reasons"), {12})


class JudgeSeesOriginalWriterDoesNot(Base):
    def test_writer_prompt_has_no_source_text(self):
        text = ep.script_writer_prompt(self.cfg, self.pid)
        self.assertNotIn(SOURCE[:80], text)

    def test_judge_prompt_has_original_for_tone_only(self):
        text = ep.script_judge_prompt(self.cfg, self.pid, "A script.")
        self.assertIn("ORIGINAL (for tone, style, hook, flow and ending ONLY)",
                      text)
        self.assertIn(SOURCE[:80], text)
        self.assertIn("Never tell the writer to match the original's facts",
                      text)

    def test_judge_notes_with_original_details_never_reach_the_writer(self):
        src = "Daniel Hargrove ran Apex Corp and quietly sold the failing plant in March."
        (self.pdir / "source_transcript.txt").write_text(src, "utf-8")
        leak = json.dumps({"score": 5, "criteria": {}, "weak_spans": [],
                           "feedback": ["Use Daniel Hargrove as the lead.",
                                        "The hook is slow - open sharper."]})
        t = Fake(zai=[words(100, "a"), words(100, "b")],
                 deepseek=[leak, json.dumps({"score": 9, "criteria": {},
                                             "feedback": [], "weak_spans": []})])
        with mock.patch.object(ep, "_target_words", return_value=100):
            ws.run_script(self.cfg, self.pid, t, "zai", "deepseek", rounds=3,
                          log=lambda m: None)
        back = [c["prompt"] for c in t.calls if c["site"] == "zai"][1]
        self.assertNotIn("Hargrove", back)
        self.assertIn("open sharper", back)

class WebSearchAlwaysOn(unittest.TestCase):
    def test_search_toggles_are_forced_on(self):
        from whisperradar import webchat
        spec = {"name": "X", "toggles": [
            {"id": "search", "label": "Web search", "text": "Search"},
            {"id": "think", "label": "Think", "text": "Think"}]}
        seen = []

        class Page:
            def evaluate(self, js, arg=None):
                seen.append(arg)
                return "ok"

            def wait_for_timeout(self, ms):
                pass
        webchat._generic_prepare(spec)(Page(), toggles={"search": False,
                                                        "think": False})
        wants = {a["text"]: a["want"] for a in seen if a}
        self.assertTrue(wants["Search"])
        self.assertFalse(wants["Think"])
        self.assertTrue(webchat.is_search_toggle({"label": "Browse the web"}))

    def test_deepseek_search_cannot_be_turned_off(self):
        from whisperradar import webchat
        seen = []

        class Page:
            def evaluate(self, js, arg=None):
                seen.append(arg)
                return "ok"

            def wait_for_timeout(self, ms):
                pass
        webchat._deepseek_prepare(Page(), deepthink=True, search=False)
        self.assertTrue({a["label"]: a["want"] for a in seen}["Search"])


if __name__ == "__main__":
    unittest.main()
