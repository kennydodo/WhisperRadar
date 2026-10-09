"""API thumbnail generator + Auto Run's end-of-pipeline thumbnail step."""
import json
import sys
import unittest
from unittest import mock

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_external_prompts import Base  # noqa: E402
from tests.test_thumbnails import three, reply, concept  # noqa: E402
from whisperradar import autorun, db, studio, thumbnails as th  # noqa: E402


def verdict(score, **kw):
    return json.dumps({"score": score, "pass": score >= 8,
                       "faults": kw.get("faults", []), "fixes": []})


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


class ApiLoopTests(Base):
    def setUp(self):
        super().setUp()
        (self.pdir / "script.md").write_text("A script about coins. " * 30,
                                             "utf-8")

    def run_api(self, replies, judge="judge"):
        llm = FakeLLM(replies)
        with mock.patch.object(studio, "llm_generate", llm):
            data = th.run_concepts_api(self.cfg, self.pid, "writer", judge,
                                       log=lambda m: None)
        return data, llm

    def test_passes_and_saves(self):
        data, llm = self.run_api([reply(three()), verdict(9.6)])
        self.assertEqual(data["status"], "ready")
        self.assertEqual(len(th.load_thumbs(self.pdir)["concepts"]), 3)
        self.assertEqual([c[0] for c in llm.calls], ["writer", "judge"])

    def test_no_judge_accepts_a_rule_clean_set(self):
        data, llm = self.run_api([reply(three())], judge=None)
        self.assertEqual(data["status"], "ready")
        self.assertEqual([c[0] for c in llm.calls], ["writer"])

    def test_a_revision_carries_the_previous_concepts(self):
        better = three()
        better[1]["text"] = "NOBODY SAW THIS"
        data, llm = self.run_api([reply(three()),
                                  verdict(5, faults=["weak words"]),
                                  reply(better), verdict(9.6)])
        self.assertEqual(data["concepts"][1]["text"], "NOBODY SAW THIS")
        self.assertIn("NOBODY KNEW", llm.calls[2][1])   # previous attempt echoed

    def test_garbage_raises(self):
        with self.assertRaises(RuntimeError):
            self.run_api(["nope", "nope", "nope"])


class AutoThumbnailTests(Base):
    def _auto(self, replies, setting="1", with_script=True, existing=None):
        if with_script:
            (self.pdir / "script.md").write_text("coins " * 30, "utf-8")
        if existing is not None:
            data = th.empty_thumbs()
            data["concepts"] = th.parse_concepts(existing)
            th.save_thumbs(self.pdir, data)
        conn = db.connect(self.cfg.db_path)
        db.set_setting(conn, "autorun_thumbnails", setting)
        conn.close()
        logs, llm = [], FakeLLM(replies)
        with mock.patch.object(studio, "llm_generate", llm), \
             mock.patch.object(autorun, "_stage_provider",
                               return_value="writer"), \
             mock.patch.object(studio, "judge_provider", return_value="judge"):
            autorun._auto_thumbnails(self.cfg, self.pid, None, logs.append)
        return logs, llm

    def test_generates_and_saves_at_the_end(self):
        _logs, llm = self._auto([reply(three()), verdict(9.6)])
        self.assertEqual([c[0] for c in llm.calls], ["writer", "judge"])
        self.assertEqual(len(th.load_thumbs(self.pdir)["concepts"]), 3)

    def test_off_makes_no_calls(self):
        _logs, llm = self._auto([reply(three()), verdict(9.6)], setting="0")
        self.assertEqual(llm.calls, [])

    def test_existing_concepts_are_kept(self):
        _logs, llm = self._auto([], existing=three())
        self.assertEqual(llm.calls, [])

    def test_no_script_skips(self):
        _logs, llm = self._auto([], with_script=False)
        self.assertEqual(llm.calls, [])

    def test_failure_never_raises(self):
        logs, _llm = self._auto([RuntimeError("boom")] * 3)
        self.assertTrue(any("skipped" in m for m in logs))


if __name__ == "__main__":
    unittest.main()
