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

    def test_only_the_analyst_and_judge_read_the_script(self):
        ctx = pp.context(self.cfg, self.pid)
        ctx["package"] = {"overview": "What hides in an old coin jar.",
                          "premise": "p", "keyword": "coin jar",
                          "values": ["curiosity"]}
        self.assertIn("rare coins", ctx["brief"])
        plan = pp.parse_plan({"keyword": "coin jar", "title": "t",
                              "titles": [], "promise": "p",
                              "thumbnail": {"text": "x"}})
        self.assertNotIn(SOURCE[:80], pp.writer_prompt(ctx))   # blind titler
        self.assertIn(SOURCE[:80], pp.analyst_prompt(ctx))
        self.assertIn(SOURCE[:80], pp.judge_prompt(ctx, plan, []))

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
    def test_numbered_titles_are_never_allowed(self):
        def plan(title):
            return pp.parse_plan({"keyword": "coin jar", "title": title,
                                  "titles": [{"text": title, "why": "x"}] * 6,
                                  "promise": "p",
                                  "thumbnail": {"text": "WOW"}})
        src = "10 Coin Jar Mistakes That Cost You Money"
        for t in ("Ten coin jar errors to avoid", "12 coin jar errors to avoid"):
            self.assertTrue(pp.scope_faults(plan(t), src), t)
        self.assertEqual(pp.scope_faults(plan("What your coin jar hides"), src),
                         [])
        self.assertIn("never", pp._numbers_text({"source": {"title": src}}))
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

class FactualBlockersTests(Base):
    def test_blockers_from_must_fix_and_wrong_claims(self):
        must, unver = studio.verdict_blockers({
            "must_fix": ["'saves $500' -> the math gives $300 -> fix it"],
            "claims": [{"claim": "APY is 8%", "status": "wrong",
                        "correction": "typical is 4%"},
                       {"claim": "x", "status": "unverified"},
                       {"claim": "y", "status": "verified"}]})
        self.assertEqual(len(must), 2)
        self.assertEqual(unver, 1)
        self.assertEqual(studio.verdict_blockers({}), ([], 0))

    def test_judge_prompt_demands_audit_and_blocks_on_errors(self):
        text = ep.script_judge_prompt(self.cfg, self.pid, "A script.")
        for needle in ("AUDIT FIRST, SCORE SECOND", "unverified",
                       "SEVERITY", "must_fix", "Maximum 3 items",
                       "Reasoning checks that matter in this niche"):
            self.assertIn(needle, text)

    def test_script_with_must_fix_cannot_pass_even_at_9_6(self):
        v1 = json.dumps({"score": 9.6, "criteria": {}, "feedback": [],
                         "weak_spans": [],
                         "must_fix": ["The savings add up to $300, not $500"]})
        v2 = json.dumps({"score": 9.2, "criteria": {}, "feedback": [],
                         "weak_spans": [], "must_fix": []})
        t = Fake(zai=[words(100, "a"), words(100, "b")], deepseek=[v1, v2])
        with mock.patch.object(ep, "_target_words", return_value=100):
            out = ws.run_script(self.cfg, self.pid, t, "zai", "deepseek",
                                rounds=3, log=lambda m: None)
        self.assertTrue(out.startswith("b0"))        # round 1 was not accepted
        back = [c["prompt"] for c in t.calls if c["site"] == "zai"][1]
        self.assertIn("MANDATORY corrections", back)
        self.assertIn("$300, not $500", back)
        follow = [c["prompt"] for c in t.calls if c["site"] == "deepseek"][1]
        self.assertIn("same severity rules", follow)
        self.assertIn("at most 3", follow)


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
