"""Auto-run's packaging plan: runs before the script, through the API
providers, applies the title, never blocks the run."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_external_prompts import Base  # noqa: E402
from tests.test_plan import good  # noqa: E402
from whisperradar import autorun, db, plan as pp, settings, studio  # noqa: E402


def verdict(score):
    return json.dumps({"score": score, "pass": score >= 8, "faults": [],
                       "fixes": []})


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, cfg, prompt, timeout=1800, provider=None,
                 max_tokens=None, temperature=1.0):
        self.calls.append((provider, prompt))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class ApiPlanTests(Base):
    def run_api(self, replies, judge="judge"):
        llm = FakeLLM(replies)
        with mock.patch.object(studio, "llm_generate", llm):
            plan = pp.run_plan_api(self.cfg, self.pid, "writer", judge,
                                   log=lambda m: None)
        return plan, llm

    def test_passes_in_one_round(self):
        plan, llm = self.run_api([json.dumps(good()), verdict(9)])
        self.assertEqual(plan["status"], "ready")
        self.assertEqual([c[0] for c in llm.calls], ["writer", "judge"])
        self.assertEqual(pp.load_plan(self.pdir)["title"], plan["title"])

    def test_a_revision_carries_the_previous_attempt_and_verdict(self):
        plan, llm = self.run_api([json.dumps(good()), verdict(5),
                                  json.dumps(good(title="The coin jar secret 1")),
                                  verdict(9)])
        self.assertEqual(plan["title"], "The coin jar secret 1")
        revision = llm.calls[2][1]
        self.assertIn("YOUR PREVIOUS ATTEMPT", revision)
        self.assertIn("Change ONLY", revision)

    def test_gives_up_after_three_rounds_with_the_best_as_a_draft(self):
        plan, _ = self.run_api([json.dumps(good()), verdict(5)] * 3)
        self.assertEqual(plan["status"], "draft")

    def test_no_judge_accepts_a_rule_clean_plan(self):
        plan, llm = self.run_api([json.dumps(good())], judge=None)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(len(llm.calls), 1)

    def test_a_judge_outage_falls_back_to_the_rules(self):
        plan, _ = self.run_api([json.dumps(good()), RuntimeError("down")])
        self.assertEqual(plan["status"], "ready")

    def test_rule_faults_block_even_a_high_score(self):
        plan, _ = self.run_api([json.dumps(good(promise="")), verdict(9)] * 3)
        self.assertEqual(plan["status"], "draft")

    def test_all_garbage_raises(self):
        with self.assertRaises(RuntimeError):
            self.run_api(["nope", "nope", "nope"])

    def test_apply_remembers_the_source_title(self):
        pp.save_plan(self.pdir, pp.parse_plan(good()))
        before = self._title()
        pp.apply_plan(self.cfg, self.pid)
        self.assertEqual(pp.load_plan(self.pdir)["source_title"], before)
        self.assertEqual(self._title(), "The coin jar secret 0")

    def _title(self):
        conn = db.connect(self.cfg.db_path)
        try:
            return db.get_production(conn, self.pid)["title"]
        finally:
            conn.close()


class AutoPlanStepTests(ApiPlanTests):
    def auto(self, replies, **kw):
        logs = []
        llm = FakeLLM(replies)
        with mock.patch.object(studio, "llm_generate", llm), \
             mock.patch.object(autorun, "_stage_provider",
                               return_value="writer"), \
             mock.patch.object(studio, "judge_provider",
                               return_value="judge"):
            autorun._auto_plan(self.cfg, self.pid, None, logs.append)
        return logs, llm

    def test_runs_and_applies_the_title(self):
        # auto-run plans through the SETTING bar (default 9.5), so a passing
        # verdict must clear it - 9 would no longer be enough
        logs, _ = self.auto([json.dumps(good()), verdict(9.6)])
        self.assertEqual(self._title(), "The coin jar secret 0")
        self.assertTrue(pp.load_plan(self.pdir)["applied"])
        self.assertTrue(any("applied" in m for m in logs))

    def test_a_draft_keeps_the_original_title(self):
        before = self._title()
        self.auto([json.dumps(good()), verdict(4)] * 3)
        self.assertEqual(self._title(), before)

    def test_an_existing_ready_plan_is_kept_and_applied_without_llm_calls(self):
        pp.save_plan(self.pdir, dict(pp.parse_plan(good()), status="ready"))
        logs, llm = self.auto([])
        self.assertEqual(llm.calls, [])
        self.assertEqual(self._title(), "The coin jar secret 0")

    def test_switched_off_does_nothing(self):
        conn = db.connect(self.cfg.db_path)
        db.set_setting(conn, "autorun_plan", "0")
        conn.close()
        before = self._title()
        logs, llm = self.auto([])
        self.assertEqual(llm.calls, [])
        self.assertEqual(self._title(), before)

    def test_a_failure_is_logged_and_never_raised(self):
        before = self._title()
        logs, _ = self.auto([RuntimeError("boom")] * 3)
        self.assertTrue(any("skipped" in m for m in logs))
        self.assertEqual(self._title(), before)

    def test_setting_exists_and_defaults_on(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.assertTrue(settings.load(conn)["autorun_plan"])
        conn.close()
        self.assertIn("autorun_plan",
                      [k for _g, keys in settings.GROUPS for k in keys])


if __name__ == "__main__":
    unittest.main()
