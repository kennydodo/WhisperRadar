"""The explainer scene-plan judge follows the configured pass mark (global, channel, or the script
minimum rating when left at 0), never a fixed number."""
import json
import unittest

from tests.test_explainer_scenes import ScenePlanTests, plan
from whisperradar import db, explainer_scenes as es, settings


def verdict(score):
    return {"score": score, "pass": True, "faults": [], "fixes": []}


class PassMarkResolution(unittest.TestCase):
    setUp = ScenePlanTests.setUp

    def _conn(self):
        return db.connect(self.cfg.db_path)

    def _channel(self, **fields):
        conn = self._conn()
        oc = db.create_own_channel(conn, "Chan", **fields)
        conn.execute("UPDATE productions SET own_channel_id = ? WHERE id = ?", (oc, self.pid))
        conn.commit()
        conn.close()
        return oc

    def _global(self, **values):
        conn = self._conn()
        settings.save(conn, {k: str(v) for k, v in values.items()})
        conn.close()

    def test_zero_follows_the_global_script_rating(self):
        self._global(script_min_rating=9.4)
        self.assertEqual(es.pass_mark(self.cfg, self.pid), 9.4)

    def test_zero_follows_the_channel_script_rating(self):
        self._global(script_min_rating=9.4)
        self._channel(script_min_rating=9.1)
        self.assertEqual(es.pass_mark(self.cfg, self.pid), 9.1)

    def test_own_global_value_wins_over_the_script_rating(self):
        self._global(script_min_rating=9.4, scene_plan_min_rating=9.0)
        self.assertEqual(es.pass_mark(self.cfg, self.pid), 9.0)

    def test_channel_value_wins_over_everything(self):
        self._global(script_min_rating=9.4, scene_plan_min_rating=9.0)
        self._channel(scene_plan_min_rating=9.6)
        self.assertEqual(es.pass_mark(self.cfg, self.pid), 9.6)

    def test_unreadable_settings_fall_back(self):
        class Bad:
            db_path = "/nonexistent/dir/wr.db"
        self.assertEqual(es.pass_mark(Bad(), 1), es.MIN_SCORE)


class VerdictUsesTheBar(unittest.TestCase):
    def test_review_and_problems_follow_the_bar(self):
        self.assertTrue(es.review_passes(verdict(9.0), 8.0))
        self.assertFalse(es.review_passes(verdict(9.0), 9.4))
        self.assertIn("below 9.4", es.verdict_problems(verdict(9.0), 9.4)[0])
        self.assertIn("at least 9.4", es.clarify_prompt(["x"], 9.4))
        self.assertIn("at least 9.4", es.judge_prompt("n", {"scenes": []}, "s", 9.4))


class RunUsesTheSetting(unittest.TestCase):
    setUp = ScenePlanTests.setUp
    _ask = ScenePlanTests._ask

    def test_a_9_score_is_not_approved_under_a_9_4_bar(self):
        conn = db.connect(self.cfg.db_path)
        settings.save(conn, {"script_min_rating": "9.4"})
        conn.close()
        prompts, logs = [], []
        honest = {"score": 9.0, "pass": False, "faults": [{"scene": "b", "severity": "blocking", "problem": "thin"}],
                  "fixes": [{"scene": "b", "severity": "blocking", "change": "add a point"}]}
        base = self._ask([plan()], [verdict(9.0), honest])   # "pass" at 9.0 contradicts 9.4: asked to clarify

        def ask(role, text, new_chat):
            if role == "judge":
                prompts.append(text)
            return base(role, text, new_chat)
        with self.assertRaises(es.NeedsRevision):
            es.run_scenes(self.cfg, self.pid, ask, log=logs.append, rounds=1)
        self.assertTrue(any("at least 9.4" in p for p in prompts))
        self.assertIn("judge pass mark: 9.4 (from the settings)", logs)
        review = json.loads((self.pdir / "plan_candidate_review.json").read_text())
        self.assertEqual(review["min_score"], 9.4)
        self.assertFalse(review["pass"])
        self.assertFalse((self.pdir / "plan.json").exists())

    def test_the_same_score_passes_when_the_bar_allows_it(self):
        conn = db.connect(self.cfg.db_path)
        settings.save(conn, {"script_min_rating": "8.5"})
        conn.close()
        es.run_scenes(self.cfg, self.pid, self._ask([plan()], [verdict(9.0)]), log=lambda _: None, rounds=1)
        self.assertTrue((self.pdir / "plan.json").exists())


if __name__ == "__main__":
    unittest.main()
