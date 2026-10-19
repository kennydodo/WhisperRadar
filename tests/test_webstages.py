"""Web-chat stage runner: writer -> judge -> feedback loops, with a scripted
fake transport (no browser, no network)."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_external_prompts import Base  # noqa: E402
from whisperradar import briefs, db, external_prompts as ep, studio, webstages as ws  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class Fake:
    """Replies are consumed per site, in order; every call is recorded."""

    def __init__(self, **replies):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.calls = []
        self.adopted = {}

    def adopt(self, site, url):
        self.adopted[site] = url

    def urls(self):
        return {"zai": "https://chat.z.ai/c/aaaa1111", "deepseek":
                "https://chat.deepseek.com/a/chat/s/bbbb2222"}

    def ask(self, site, prompt, files=(), new_chat=True, ready=None):
        self.calls.append({"site": site, "prompt": prompt,
                           "files": list(files), "new_chat": new_chat})
        return self.replies[site].pop(0)


def words(n, tag="alpha"):
    return " ".join(f"{tag}{i}" for i in range(n)) + "."


def verdict(score, feedback=()):
    return json.dumps({"score": score, "criteria": {},
                       "feedback": list(feedback), "weak_spans": ["w1"]})


class ScriptLoopTests(Base):
    def run_loop(self, t, rounds=3):
        with mock.patch.object(ep, "_target_words", return_value=100):
            return ws.run_script(self.cfg, self.pid, t, "zai", "deepseek",
                                 rounds=rounds, log=lambda m: None)

    def stage(self):
        conn = db.connect(self.cfg.db_path)
        try:
            return db.get_production(conn, self.pid)["stage"]
        finally:
            conn.close()

    def test_passes_first_round_and_saves(self):
        t = Fake(zai=[words(100)], deepseek=[verdict(9)])
        before = self.stage()
        out = self.run_loop(t)
        self.assertEqual(len(out.split()), 100)
        self.assertEqual((self.pdir / "script.md").read_text("utf-8").strip(),
                         out)
        self.assertNotEqual(self.stage(), before)
        self.assertEqual([c["site"] for c in t.calls], ["zai", "deepseek"])
        self.assertTrue(all(c["new_chat"] for c in t.calls))

    def test_feedback_goes_back_to_the_writer_in_the_same_chat(self):
        t = Fake(zai=[words(100, "a"), words(100, "b")],
                 deepseek=[verdict(4, ["tighten the hook"]), verdict(9)])
        out = self.run_loop(t)
        self.assertTrue(out.startswith("b0"))
        sites = [(c["site"], c["new_chat"]) for c in t.calls]
        self.assertEqual(sites, [("zai", True), ("deepseek", True),
                                 ("zai", False), ("deepseek", False)])
        self.assertIn("SAME", t.calls[3]["prompt"])
        fb = t.calls[2]["prompt"]
        self.assertIn("tighten the hook", fb)
        self.assertIn("w1", fb)
        self.assertIn("COMPLETE script", fb)

    def test_never_passing_saves_best_but_does_not_advance(self):
        t = Fake(zai=[words(100, "a"), words(100, "b")],
                 deepseek=[verdict(3), verdict(5)])
        before = self.stage()
        with self.assertRaises(ws.StageFailed):
            self.run_loop(t, rounds=2)
        self.assertEqual(self.stage(), before)
        self.assertTrue((self.pdir / "script.md").read_text().startswith("b0"))

    def test_a_judge_reply_without_a_score_is_asked_again(self):
        t = Fake(zai=[words(100)], deepseek=["I think it is fine.", verdict(9)])
        self.run_loop(t)
        self.assertEqual([c["site"] for c in t.calls],
                         ["zai", "deepseek", "deepseek"])

    def test_a_cut_off_or_short_draft_is_not_accepted(self):
        t = Fake(zai=[words(30)], deepseek=[verdict(9)])
        with self.assertRaises(ws.StageFailed):
            self.run_loop(t, rounds=1)


PLAN = {"style": "flat", "images": [{"file": "a_ST.png", "prompt": "a cat"}],
        "shots": [{"asset": "a_ST.png", "cues": "1-3"}]}


def plan_json(**over):
    d = json.loads(json.dumps(PLAN))
    d.update(over)
    return json.dumps(d)


class ShotlistLoopTests(Base):
    def run_loop(self, t, rounds=3, local=()):
        with mock.patch.object(ep, "shotlist_planner_prompt",
                               return_value="PLAN PROMPT"), \
             mock.patch.object(ep, "shotlist_judge_prompt",
                               return_value=("JUDGE PROMPT", list(local))):
            return ws.run_shotlist(self.cfg, self.pid, t, "zai", "deepseek",
                                   rounds=rounds, log=lambda m: None)

    def test_passes_and_saves(self):
        t = Fake(zai=["```json\n" + plan_json() + "\n```"],
                 deepseek=[json.dumps({"faults": [], "shots": [],
                                       "detailed_ratio": 1, "pass": True})])
        self.run_loop(t)
        saved = json.loads((self.pdir / "shotlist.json").read_text("utf-8"))
        self.assertEqual(saved["images"][0]["file"], "a_ST.png")

    def test_cut_off_plan_is_continued_until_the_json_closes(self):
        full = plan_json()
        cut = full.index('"shots"')
        t = Fake(zai=["json\nCopy\n```json\n" + full[:cut] + "\n```",
                      "```json\n" + full[cut:] + "\n```"],
                 deepseek=[json.dumps({"faults": [], "shots": [],
                                       "detailed_ratio": 1, "pass": True})])
        self.run_loop(t)
        self.assertEqual([(c["site"], c["prompt"], c["new_chat"])
                          for c in t.calls[:2]],
                         [("zai", "PLAN PROMPT", True),
                          ("zai", "continue", False)])

    def test_weak_prompts_go_back_as_a_patch_and_are_applied(self):
        weak = {"faults": [], "pass": False, "detailed_ratio": 0.5,
                "shots": [{"asset": "a_ST.png", "verdict": "weak",
                           "missing": ["the cheetah"], "reason": "vague"}]}
        ok = {"faults": [], "shots": [], "detailed_ratio": 1, "pass": True}
        patch = json.dumps({"patches": {"a_ST.png": "a cheetah on a rock"}})
        t = Fake(zai=[plan_json(), patch],
                 deepseek=[json.dumps(weak), json.dumps(ok)])
        self.run_loop(t)
        sent = t.calls[2]
        self.assertEqual((sent["site"], sent["new_chat"]), ("zai", False))
        self.assertIn("CURRENT PROMPT: a cat", sent["prompt"])
        self.assertIn("the cheetah", sent["prompt"])
        saved = json.loads((self.pdir / "shotlist.json").read_text("utf-8"))
        self.assertEqual(saved["images"][0]["prompt"], "a cheetah on a rock")

    def test_hard_faults_ask_the_writer_for_a_corrected_plan(self):
        bad = {"faults": ["shots 2-3 overlap"], "shots": [], "pass": False}
        ok = {"faults": [], "shots": [], "detailed_ratio": 1, "pass": True}
        t = Fake(zai=[plan_json(), plan_json(style="fixed")],
                 deepseek=[json.dumps(bad), json.dumps(ok)])
        self.run_loop(t)
        self.assertIn("shots 2-3 overlap", t.calls[2]["prompt"])
        self.assertFalse(t.calls[2]["new_chat"])
        # the judge stays in its first chat and only gets the revised plan
        self.assertTrue(t.calls[1]["new_chat"])
        self.assertFalse(t.calls[3]["new_chat"])
        self.assertEqual(t.calls[3]["site"], "deepseek")
        self.assertIn("SAME rules", t.calls[3]["prompt"])
        saved = json.loads((self.pdir / "shotlist.json").read_text("utf-8"))
        self.assertEqual(saved["style"], "fixed")

    def test_code_faults_go_back_without_a_judge_round(self):
        ok = {"faults": [], "shots": [], "detailed_ratio": 1, "pass": True}
        t = Fake(zai=[plan_json()], deepseek=[json.dumps(ok)])
        with self.assertRaises(ws.StageFailed):
            self.run_loop(t, rounds=1, local=["hold too long at cue 2"])
        # the code faults are decided locally; the ~2.5 min judge round is
        # never spent on a plan code already knows is broken
        self.assertEqual([c["site"] for c in t.calls], ["zai"])

    def test_a_judge_that_never_answers_stops_with_the_plan_saved(self):
        t = Fake(zai=[plan_json()], deepseek=["nope", "still nope"])
        with self.assertRaises(ws.StageFailed):
            self.run_loop(t)
        self.assertTrue((self.pdir / "shotlist.json").exists())



class NoLimitTests(Base):
    def test_the_script_loop_keeps_going_past_three_rounds_until_it_passes(self):
        zai = [words(100, c) for c in "abcde"]
        t = Fake(zai=zai, deepseek=[verdict(4)] * 4 + [verdict(9.9)])
        with mock.patch.object(ep, "_target_words", return_value=100):
            out = ws.run_script(self.cfg, self.pid, t, "zai", "deepseek",
                                log=lambda m: None)
        self.assertTrue(out.startswith("e0"))
        self.assertEqual(sum(c["site"] == "deepseek" for c in t.calls), 5)

    def test_stop_saves_the_best_script_without_advancing(self):
        t = Fake(zai=[words(100, "a"), words(100, "b")],
                 deepseek=[verdict(6), verdict(5)])
        n = [0]

        def stop():
            n[0] += 1
            return n[0] >= 2          # allow one feedback round, then stop
        with mock.patch.object(ep, "_target_words", return_value=100):
            with self.assertRaises(ws.StageFailed):
                ws.run_script(self.cfg, self.pid, t, "zai", "deepseek",
                              log=lambda m: None, should_stop=stop)
        self.assertTrue((self.pdir / "script.md").read_text().startswith("a0"))

    def test_shotlist_stop_keeps_the_best_plan_not_the_last(self):
        good = plan_json(style="good")
        worse = plan_json(style="worse")
        r1 = {"faults": ["a"], "shots": [], "pass": False}
        r2 = {"faults": ["a", "b", "c"], "shots": [], "pass": False}
        t = Fake(zai=[good, worse],
                 deepseek=[json.dumps(r1), json.dumps(r2)])
        n = [0]

        def stop():
            n[0] += 1
            return n[0] >= 2
        with mock.patch.object(ep, "shotlist_planner_prompt",
                               return_value="P"), \
             mock.patch.object(ep, "shotlist_judge_prompt",
                               return_value=("J", [])):
            with self.assertRaises(ws.StageFailed):
                ws.run_shotlist(self.cfg, self.pid, t, "zai", "deepseek",
                                log=lambda m: None, should_stop=stop)
        saved = json.loads((self.pdir / "shotlist.json").read_text("utf-8"))
        self.assertEqual(saved["style"], "good")

    def test_stop_route_needs_a_running_web_chat_job(self):
        c = create_app(self.cfg).test_client()
        r = c.post(f"/studio/{self.pid}/webchat-stop")
        self.assertIn("No", r.headers["Location"])


class OptionsTests(unittest.TestCase):
    def test_defaults_and_overrides_per_site(self):
        seen = []

        class Chat:
            def ask(self, key, prompt, files, new_chat=True, options=None,
                    ready=None, log=None):
                seen.append((key, options))
                return "ok"

        t = ws.WebTransport(Chat(), lambda m: None,
                            {"zai": {"thinking": "High"},
                             "deepseek": {"search": True}})
        t.ask("zai", "p")
        t.ask("deepseek", "p")
        self.assertEqual(seen[0], ("zai", {"thinking": "High",
                                           "model": "flash"}))
        self.assertEqual(seen[1], ("deepseek",
                                   {"deepthink": True, "search": True}))
        d = ws.WebTransport(Chat(), lambda m: None)
        d.ask("deepseek", "p")
        self.assertEqual(seen[2][1], {"deepthink": True, "search": True})


class RouteOptionTests(Base):
    def test_the_form_choices_reach_the_job(self):
        c = create_app(self.cfg).test_client()
        with mock.patch.object(ws, "shotlist_job") as job:
            c.post(f"/studio/{self.pid}/webchat/shots",
                   data={"writer": "zai", "judge": "deepseek",
                         "zai_thinking": "Max", "zai_model": "5.3",
                         "deepseek_deepthink": "off",
                         "deepseek_search": "on"})
            import time
            for _ in range(50):
                if job.called:
                    break
                time.sleep(0.1)
            opts = job.call_args.args[6]
        self.assertEqual(opts, {"zai": {"thinking": "Max", "model": "5.3"},
                                "deepseek": {"deepthink": False,
                                             "search": True},
                                "deepseek_writer": {"deepthink": True,
                                                    "search": True},
                                "deepseek_judge": {"deepthink": True,
                                                   "search": True}})

    def test_bad_values_fall_back_to_the_defaults(self):
        c = create_app(self.cfg).test_client()
        with mock.patch.object(ws, "script_job") as job:
            c.post(f"/studio/{self.pid}/webchat/script",
                   data={"zai_thinking": "Turbo"})
            import time
            for _ in range(50):
                if job.called:
                    break
                time.sleep(0.1)
            opts = job.call_args.args[6]
        self.assertEqual(opts["zai"], {"thinking": "Low", "model": "flash"})
        self.assertEqual(opts["deepseek"], {"deepthink": True,
                                            "search": True})

class SendTests(unittest.TestCase):
    def test_big_prompts_switch_to_attached_files(self):
        t = Fake(zai=["ok"])
        seen = {}

        def build(files):
            if files is None:
                return "x" * (ws.INLINE_MAX_CHARS + 1)
            files.append({"name": "narration.txt", "text": "hi", "about": ""})
            return "see file"

        class T(Fake):
            def ask(self, site, prompt, files=(), new_chat=True, ready=None):
                seen["files"] = [Path(f).read_text() for f in files]
                return "ok"

        ws._send(T(), "zai", build, lambda m: None)
        self.assertEqual(seen["files"], ["hi"])


class RouteTests(Base):
    def test_buttons_are_on_the_script_and_shotlist_pages(self):
        c = create_app(self.cfg).test_client()
        for stage in ("script", "shots"):
            page = c.get(f"/studio/{self.pid}?stage={stage}").get_data(
                as_text=True)
            self.assertTrue(f"/studio/{self.pid}/webchat/{stage}" in page)

    def test_the_route_starts_the_job_and_rejects_bad_input(self):
        c = create_app(self.cfg).test_client()
        with mock.patch.object(ws, "script_job") as job:
            r = c.post(f"/studio/{self.pid}/webchat/script",
                       data={"writer": "zai", "judge": "deepseek"})
            self.assertEqual(r.status_code, 302)
            for _ in range(50):
                if job.called:
                    break
                import time
                time.sleep(0.1)
            self.assertEqual(job.call_args.args[2:4], ("zai", "deepseek"))
        self.assertIn("Unknown", c.post(
            f"/studio/{self.pid}/webchat/script",
            data={"writer": "x"}).headers["Location"])
        self.assertIn("Unknown", c.post(
            f"/studio/{self.pid}/webchat/bogus").headers["Location"])


class InlineOnlySiteTests(unittest.TestCase):
    class T:
        def __init__(self):
            self.calls = []

        def ask(self, site, prompt, files=(), new_chat=True, ready=None):
            self.calls.append((site, prompt, list(files)))
            return "ok"

    @staticmethod
    def _build(files):
        big = "N" * (ws.INLINE_MAX_CHARS + 500)
        if files is None:
            return "INLINE " + big
        files.append({"name": "narration.txt", "text": big, "about": "a"})
        return "see narration.txt"

    def test_qwen_always_gets_the_text_in_the_message(self):
        t = self.T()
        ws._send(t, "qwen", self._build, lambda m: None)
        site, prompt, files = t.calls[0]
        self.assertTrue(prompt.startswith("INLINE"))
        self.assertEqual(files, [])

    def test_other_sites_still_switch_to_files_when_big(self):
        t = self.T()
        ws._send(t, "zai", self._build, lambda m: None)
        site, prompt, files = t.calls[0]
        self.assertEqual(prompt, "see narration.txt")
        self.assertEqual(len(files), 1)


if __name__ == "__main__":
    unittest.main()


class JudgeSameChatTests(unittest.TestCase):
    def test_followup_texts(self):
        files = []
        t = ws._plan_followup('{"shots": []}', files)
        self.assertIn("shotlist.json", t)
        self.assertEqual(files[0]["name"], "shotlist.json")
        self.assertIn('{"shots": []}', ws._plan_followup('{"shots": []}', None))
        self.assertIn("SAME", ws._script_followup("hello"))

    def test_typed_instructions_are_repeated_every_round(self):
        # whatever the creator types must reach the judge and the writer in
        # EVERY message, not only the first prompt
        jd = "List the points in REVERSE order of the original."
        wd = "Invent your own company names."
        self.assertIn(jd, ws._script_followup("x", direction=jd))
        self.assertIn("JUDGE", ws._script_followup("x", direction=jd))
        fb = ws._script_feedback(900, 1000, ["too short"],
                                 {"feedback": ["a"]}, False, wd)
        self.assertIn(wd, fb)
        self.assertIn("WRITER", fb)
        self.assertNotIn("CREATOR'S INSTRUCTIONS",
                         ws._script_feedback(900, 1000, [], {}, False))
        self.assertNotIn("CREATOR'S INSTRUCTIONS", ws._script_followup("x"))

    def test_followup_states_measured_length(self):
        t = ws._script_followup("x", 1500, 1000, 0.02, False)
        self.assertIn("1500 words", t)
        self.assertIn("OUTSIDE", t)
        self.assertIn("INSIDE", ws._script_followup("x", 1000, 1000, 0.0,
                                                    False))

    def test_word_count_matches_manual_prompts_and_feedback_is_exact(self):
        self.assertEqual(ws._wc("It's a well-known fact."), 6)
        over = ws._length_delta(1500, 1000)
        self.assertIn("over", over)
        self.assertIn("Cut at least 500", over)
        self.assertIn("Add about", ws._length_delta(500, 1000))


class JoinAndResumeTests(Base):
    PLAN = {"style": "s", "shots": [
        {"asset": "a_ST.png", "cues": "1-2"}, {"asset": "b_ST.png", "cues": "3-4"}],
        "images": [{"file": "a_ST.png", "prompt": "one"},
                   {"file": "b_ST.png", "prompt": "two two two"}]}

    def test_a_restarted_half_entry_is_dropped_when_joining(self):
        full = json.dumps(self.PLAN, indent=1)
        cut = full.index('"two two')
        first = full[:cut + 8]                       # b_ST cut mid-prompt
        # the continue reply restarts the b_ST entry and finishes the plan
        tail = '    { "file": "b_ST.png", "prompt": "two two two" }\n  ]\n}'
        joined = ws._join_chunks(first, tail)
        data = ws._plan_from_text(joined)
        self.assertEqual(len(data["images"]), 2)
        self.assertEqual(data["images"][1]["prompt"], "two two two")

    def test_a_plain_continuation_is_just_appended(self):
        self.assertEqual(ws._join_chunks('{"a": [1,', '2]}'),
                         '{"a": [1,\n2]}')

    def test_resume_reads_the_saved_replies_and_skips_the_writer(self):
        full = "```json\n" + plan_json() + "\n```"
        d = self.pdir / "webchat_debug"
        d.mkdir(exist_ok=True)
        (d / "plan_reply_1.txt").write_text(full, encoding="utf-8")
        ok = {"faults": [], "shots": [], "detailed_ratio": 1, "pass": True}
        t = Fake(zai=[], deepseek=[json.dumps(ok)])
        with mock.patch.object(ep, "shotlist_judge_prompt",
                               return_value=("JUDGE PROMPT", [])):
            ws.run_shotlist(self.cfg, self.pid, t, "zai", "deepseek",
                            log=lambda m: None, resume=True)
        self.assertEqual([c["site"] for c in t.calls], ["deepseek"])


class ChatLostTests(Base):
    def test_a_lost_writer_chat_is_restarted_with_the_plan_and_the_faults(self):
        from whisperradar import webchat as wcm

        class T(Fake):
            def ask(self, site, prompt, files=(), new_chat=True, ready=None):
                if site == "zai" and not new_chat:
                    self.calls.append({"site": site, "prompt": prompt,
                                       "files": [], "new_chat": new_chat})
                    raise wcm.ChatLost("gone")
                return super().ask(site, prompt, files, new_chat, ready)

        bad = {"faults": ["shots 2-3 overlap"], "shots": [], "pass": False}
        ok = {"faults": [], "shots": [], "detailed_ratio": 1, "pass": True}
        t = T(zai=[plan_json(), plan_json(style="fixed")],
              deepseek=[json.dumps(bad), json.dumps(ok)])
        with mock.patch.object(ep, "shotlist_planner_prompt",
                               return_value="PLAN PROMPT"), \
             mock.patch.object(ep, "shotlist_judge_prompt",
                               return_value=("JUDGE PROMPT", [])):
            ws.run_shotlist(self.cfg, self.pid, t, "zai", "deepseek",
                            rounds=3, log=lambda m: None)
        fresh = [c for c in t.calls if c["site"] == "zai" and c["new_chat"]]
        self.assertEqual(len(fresh), 2)            # first plan + recovery
        self.assertIn("shots 2-3 overlap", fresh[1]["prompt"])
        self.assertIn("YOUR PREVIOUS SHOTLIST", fresh[1]["prompt"])


class SameChatsTests(Base):
    def _run(self, t, same=True):
        with mock.patch.object(ep, "shotlist_planner_prompt",
                               return_value="PLAN PROMPT"), \
             mock.patch.object(ep, "shotlist_judge_prompt",
                               return_value=("JUDGE PROMPT", [])):
            return ws.run_shotlist(self.cfg, self.pid, t, "zai", "deepseek",
                                   log=lambda m: None, same_chats=same)

    def test_chats_are_saved_and_continued_by_the_next_stage(self):
        ws.save_chats(self.cfg, self.pid, Fake())
        self.assertEqual(ws.load_chats(self.cfg, self.pid)["zai"],
                         "https://chat.z.ai/c/aaaa1111")
        ok = {"faults": [], "shots": [], "detailed_ratio": 1, "pass": True}
        t = Fake(zai=["```json\n" + plan_json() + "\n```"],
                 deepseek=[json.dumps(ok)])
        self._run(t)
        self.assertEqual(t.adopted["zai"], "https://chat.z.ai/c/aaaa1111")
        self.assertEqual([(c["site"], c["new_chat"]) for c in t.calls],
                         [("zai", False), ("deepseek", False)])
        self.assertIn("PLAN PROMPT", t.calls[0]["prompt"])
        self.assertIn("JUDGE PROMPT", t.calls[1]["prompt"])   # full rules

    def test_without_the_option_everything_starts_fresh(self):
        ws.save_chats(self.cfg, self.pid, Fake())
        ok = {"faults": [], "shots": [], "detailed_ratio": 1, "pass": True}
        t = Fake(zai=["```json\n" + plan_json() + "\n```"],
                 deepseek=[json.dumps(ok)])
        self._run(t, same=False)
        self.assertEqual(t.adopted, {})
        self.assertTrue(all(c["new_chat"] for c in t.calls))

    def test_a_chat_that_cannot_be_reopened_falls_back_to_a_new_one(self):
        from whisperradar import webchat as wcm
        ws.save_chats(self.cfg, self.pid, Fake())

        class T(Fake):
            def ask(self, site, prompt, files=(), new_chat=True, ready=None):
                if not new_chat:
                    self.calls.append({"site": site, "prompt": prompt,
                                       "files": [], "new_chat": new_chat})
                    raise wcm.ChatLost("gone")
                return super().ask(site, prompt, files, new_chat, ready)

        ok = {"faults": [], "shots": [], "detailed_ratio": 1, "pass": True}
        t = T(zai=["```json\n" + plan_json() + "\n```"],
              deepseek=[json.dumps(ok)])
        self._run(t)
        fresh = [(c["site"], c["new_chat"]) for c in t.calls]
        self.assertEqual(fresh, [("zai", False), ("zai", True),
                                 ("deepseek", False), ("deepseek", True)])


class RobustnessTests(Base):
    def test_feedback_carries_the_judges_json_verbatim(self):
        verdict = {"faults": ["shots 2-3 overlap"], "pass": False,
                   "shots": [{"asset": "a_ST.png", "verdict": "weak"}]}
        text = ws._plan_feedback(["shots 2-3 overlap"], [], [], verdict,
                                 ["cue 9 uncovered"])
        self.assertIn('"shots 2-3 overlap"', text)
        self.assertIn("cue 9 uncovered", text)
        self.assertIn("verbatim", text)
        self.assertIn("Change ONLY", text)

    def test_changed_prompts_counts_edits_and_new_files(self):
        old = {"images": [{"file": "a", "prompt": "1"}, {"file": "b", "prompt": "2"}]}
        new = {"images": [{"file": "a", "prompt": "1"},
                          {"file": "b", "prompt": "two"},
                          {"file": "c", "prompt": "3"}]}
        self.assertEqual(ws._changed_prompts(old, new), (2, 3))

    def test_every_script_round_is_kept_as_a_version_with_its_review(self):
        t = Fake(zai=[words(100, "a"), words(100, "b")],
                 deepseek=[verdict(4, ["tighten the hook"]), verdict(9)])
        with mock.patch.object(ep, "_target_words", return_value=100):
            ws.run_script(self.cfg, self.pid, t, "zai", "deepseek",
                          rounds=3, log=lambda m: None)
        d = self.pdir / "versions" / "script"
        self.assertTrue((d / "attempt-1.md").read_text("utf-8").startswith("a0"))
        self.assertTrue((d / "attempt-2.md").read_text("utf-8").startswith("b0"))
        rev = json.loads((d / "review.json").read_text("utf-8"))
        self.assertEqual([r["score"] for r in rev], [4.0, 9.0])
        self.assertIn("tighten the hook", rev[0]["feedback"])
        self.assertTrue(rev[0]["reasons"])
        self.assertFalse(rev[0]["passed"])

    def test_review_json_keeps_zero_score_too_long_and_reasons(self):
        t = Fake(zai=[words(200, "a"), words(100, "b")],
                 deepseek=[verdict(0, ["too long"]), verdict(9)])
        with mock.patch.object(ep, "_target_words", return_value=100):
            ws.run_script(self.cfg, self.pid, t, "zai", "deepseek",
                          rounds=2, log=lambda m: None)
        rev = json.loads((self.pdir / "versions" / "script"
                          / "review.json").read_text("utf-8"))
        self.assertEqual(rev[0]["score"], 0.0)      # not null
        self.assertIs(rev[0]["too_long"], True)     # 200 > 115% of 100
        self.assertIs(rev[1]["too_long"], False)
        self.assertTrue(any("rating" in r for r in rev[0]["reasons"]))
        self.assertTrue(rev[1]["passed"])


class AutoRefreshTests(unittest.TestCase):
    def test_the_poll_marks_a_job_that_started_after_the_page_loaded(self):
        html = (ROOT / "whisperradar" / "templates"
                / "studio_detail.html").read_text("utf-8")
        run_block = html[html.index("if (s.running) {"):]
        self.assertIn("wasRunning = true;", run_block[:200])
        # only the user's own typing counts as an unsaved edit
        self.assertIn("e.isTrusted", html)


class VerdictParseShapeTests(unittest.TestCase):
    """A thinking judge that drafts a verdict in its reasoning and only emits
    the real one at the end must be read from the LAST shaped object."""

    def test_last_object_with_the_expected_shape_wins(self):
        draft = '{"pass": false, "faults": ["oops"], "shots": [], "detailed_ratio": 0.5}'
        real = '{"pass": true, "faults": [], "shots": [], "detailed_ratio": 1}'
        reply = ("Thinking about it: I could write " + draft +
                 " but after reviewing the plan, here is my verdict:\n\n" + real)
        got = studio._parse_json_object(reply, want=("pass", "faults"))
        self.assertTrue(got["pass"])
        self.assertEqual(got["detailed_ratio"], 1)

    def test_no_shape_hints_is_the_first_object_only(self):
        reply = '{"score": 3} then {"score": 9} trailing prose with a } brace'
        got = studio._parse_json_object(reply)
        self.assertEqual(got, {"score": 3})

    def test_stray_braces_in_prose_before_the_verdict_do_not_hide_it(self):
        # the real bug shape: prose mentions JSON, verdict sits at the end
        reply = ('Reviewing {the shots and cues} for {hard} faults.\n'
                 '{"pass": true, "faults": [], "shots": [], "detailed_ratio": 1}')
        got = studio._parse_json_object(reply, want=("pass", "faults"))
        self.assertTrue(got.get("pass"))

    def test_curly_quoted_final_verdict_beats_a_plain_draft(self):
        # Claude's caught case: the want-scan must repair curly quotes too,
        # or the shaped search matches nothing and the fallback hands back the
        # DRAFT object as the verdict
        draft = '{"pass": false, "faults": ["seen too early"], "shots": [], "detailed_ratio": 0.4}'
        final = ('{“pass”: true, “faults”: [], “shots”: [], '
                 '“detailed_ratio”: 1}')
        got = studio._parse_json_object(draft + " on reflection:\n" + final,
                                        want=("pass", "faults"))
        self.assertTrue(got["pass"])
        self.assertEqual(got["faults"], [])


class CodeFirstRejectionTests(Base):
    """Code faults return to the writer WITHOUT a judge round; a plan that
    passes the code checks still reaches the judge."""

    def test_faults_bring_the_writer_then_a_clean_plan_reaches_the_judge(self):
        calls = {"n": 0}

        def judge_prompt(cfg, pid, text, files):
            # round 1 plan has a code fault, round 2 (corrected) does not
            calls["n"] += 1
            faults = [] if "fixed" in text else ["hold too long at cue 2"]
            return ("JUDGE PROMPT", faults)

        ok = json.dumps({"faults": [], "shots": [], "detailed_ratio": 1,
                         "pass": True})
        t = Fake(zai=[plan_json(), plan_json(style="fixed")], deepseek=[ok])
        with mock.patch.object(ep, "shotlist_planner_prompt",
                               return_value="PLAN PROMPT"), \
             mock.patch.object(ep, "shotlist_judge_prompt",
                               side_effect=judge_prompt):
            out = ws.run_shotlist(self.cfg, self.pid, t, "zai", "deepseek",
                                  log=lambda m: None)
        self.assertEqual(out["style"], "fixed")
        sites = [c["site"] for c in t.calls]
        # the first exchange is code-only: no deepseek call before the fix
        self.assertEqual(sites[:2], ["zai", "zai"])
        self.assertEqual(sites[2], "deepseek")
        self.assertIn("hold too long at cue 2", t.calls[1]["prompt"])


class AskTimingTests(unittest.TestCase):
    def test_ask_logs_the_round_trip_time(self):
        seen = []

        class Chat:
            def ask(self, *a, **k):
                return "a reply"

        t = ws.WebTransport(Chat(), lambda m: seen.append(m))
        out = t.ask("zai", "prompt")
        self.assertEqual(out, "a reply")
        self.assertTrue(any("reply of 7 chars after" in m and "zai" in m
                            for m in seen), seen)

    def test_a_failed_ask_logs_and_reraises(self):
        class Chat:
            def ask(self, *a, **k):
                raise RuntimeError("boom")

        seen = []
        t = ws.WebTransport(Chat(), lambda m: seen.append(m))
        with self.assertRaises(RuntimeError):
            t.ask("zai", "p")
        self.assertTrue(any("round trip failed" in m for m in seen), seen)


class AutoSplitAndDecidedFaultTests(unittest.TestCase):
    def _cues(self, n, each=3.0):
        def ts(x):
            return f"00:{int(x // 60):02d}:{x % 60:06.3f}".replace(".", ",")
        return [{"index": i, "start": ts((i - 1) * each), "end": ts(i * each),
                 "text": f"cue {i}"} for i in range(1, n + 1)]

    def test_long_shot_is_split_near_the_middle_and_keeps_picture(self):
        data = {"shots": [{"cues": "1-6", "asset": "A.png", "scene": "S01", "motion": "ZI"},
                          {"cues": "7-8", "asset": "B.png", "scene": "S01", "motion": "ST"}]}
        n = ws._auto_split_long(data, self._cues(8), 14.4)
        self.assertEqual(n, 1)                      # 1-6 holds 18s
        self.assertEqual([s["cues"] for s in data["shots"]], ["1-3", "4-6", "7-8"])
        self.assertEqual(data["shots"][1]["asset"], "A.png")

    def test_shot_within_limit_is_left_alone(self):
        data = {"shots": [{"cues": "1-4", "asset": "A.png", "scene": "S01", "motion": "ZI"}]}
        self.assertEqual(ws._auto_split_long(data, self._cues(4), 14.4), 0)

    def test_hold_faults_are_recognised_as_code_decided(self):
        m = ws._CODE_DECIDED.match
        self.assertTrue(m("MAXIMUM HOLD: S16_01 cues 75-82 hold 14s (>12s)"))
        self.assertTrue(m("STATIC HOLD: S16_01 cues 83-86 is ST and holds 6s (>5s)"))
        self.assertTrue(m("MAXIMUM ST HOLD: S08_01_CMP_ZO.png, shot cues 61-63, is ST and holds 5s, over the 4.8s limit"))
        self.assertTrue(m("MAXIMUM STATIC HOLD: S01_02 holds 6s"))
        # any label in front, as the judge actually wrote them
        self.assertTrue(m("MOTION MIX: shot S10_02_CMP_ZI.png (cues 61-63) is ST and holds 5s, over the 4.8s limit for a static shot."))
        self.assertTrue(m("MOTION MIX: shot S10_16_CU_ST.png (cues 145-147) is ST and holds 5s, over the 4.8s limit for a static shot."))
        self.assertTrue(m("PACING: S03_01 cues 9-12 holds 13s which exceeds the 12s maximum limit"))
        # the exact prod-40 phrasing: "holding", "cap"
        self.assertTrue(m("S27_06_CMP_ST.png cues 126-128 is a static ST shot holding 6.0s, exceeding the 5.0s ST hold cap."))
        # not hold arithmetic: these stay faults
        self.assertFalse(m("MOTION MIX: ST is 20% of shots (cap ~10%) - vary the motion codes"))
        self.assertFalse(m("MOTION MIX: PU/PD together are 37% of shots (cap ~30%)"))
        self.assertFalse(m("COVERAGE: cues 10-12 missing"))
        self.assertFalse(m("REFERENCES OFF: 55 of the 60 image prompts say attached reference"))

    def test_false_hold_fault_is_measured_not_matched(self):
        from dataclasses import replace
        cues = self._cues(3, each=3.0)           # cues 126-style: sums lie
        cues[0]["end"] = cues[0]["start"][:7] + "1.680"
        cues[1]["end"] = cues[1]["start"][:7] + "4.420"
        cues[1]["start"] = cues[0]["end"]
        cues[2]["start"] = cues[1]["end"]
        cues[2]["end"] = cues[2]["start"][:7] + "4.680"
        prof = replace(briefs.STANDARD, st_max_hold=5.0, st_hold_tol=0.0)
        data = {"shots": [{"cues": "1-3", "asset": "S27_06_CMP_ST.png",
                           "scene": "S27", "motion": "ST"}]}
        # the exact prod-40 fault: judge summed rounded durations -> 6.0s
        fault = ("S27_06_CMP_ST.png cues 1-3 is a static ST shot holding "
                 "6.0s, exceeding the 5.0s ST hold cap.")
        self.assertTrue(ws._CODE_DECIDED.match(fault))
        self.assertTrue(ws._false_hold_fault(fault, data, cues, prof))
        # a REAL over-limit hold stays a fault even in the same phrasing
        cues[2]["end"] = cues[2]["start"][:7] + "9.000"
        self.assertFalse(ws._false_hold_fault(fault, data, cues, prof))
        # an unnamed / ambiguous fault is never dropped on a guess
        self.assertFalse(ws._false_hold_fault(
            "a static shot holds 6.0s, exceeding the 5.0s ST hold cap",
            data, cues, prof))
        # non-hold faults are untouched
        self.assertFalse(ws._false_hold_fault(
            "COVERAGE: cues 10-12 missing", data, cues, prof))

    def test_mechanical_labels_drop_when_the_app_check_is_clean(self):
        m = ws._MECHANICAL_LABELS.match
        # the exact prod-40 rambling: each one answers its own question
        self.assertTrue(m("COVERAGE: shot with cues 488-491 is followed by "
                          "492-496, but cue 487-491's range overlaps with the "
                          "preceding 484-486? No—checking order: 484-486, "
                          "487-491, 492-496 is contiguous.; ASSETS: ..."))
        self.assertTrue(m("ASSETS: shot with cues 586-588 references asset "
                          "S01_01_SCN_ZI.png with scene S25, but there is "
                          "only one image prompt entry for that file; that is "
                          "allowed (same asset reused)."))
        self.assertTrue(m("MOTION MIX: ST is 20% of shots (cap ~10%) - vary "
                          "the motion codes"))
        self.assertTrue(m("MOTION CODES: PL is not allowed on this channel"))
        self.assertTrue(m("MAXIMUM HOLD: S16_01 cues 75-82 hold 14s (>12s)"))
        # categories the app does NOT fully check stay with the judge
        self.assertFalse(m("MINIMUM HOLD: 40% of shots hold under 8s"))
        self.assertFalse(m("REFERENCES OFF: 55 of the 60 image prompts say "
                           "attached reference"))
        self.assertFalse(m("PROMPT QUALITY: the infographic never shows the "
                           "mechanism"))
        # weak-prompt and other prose without a mechanical label is untouched
        self.assertFalse(m("The comparison shot reads as wallpaper"))


class ForceFilesTests(unittest.TestCase):
    class _T:
        def __init__(self):
            self.calls = []

        def ask(self, site, text, paths, new_chat=True, ready=None):
            self.calls.append((site, text, list(paths)))
            return "ok"

    def _build(self, files):
        if files is None:
            return "inline prompt"
        files.append({"name": "writing_style.md", "text": "STYLE", "about": "x"})
        return "prompt with attachment"

    def test_force_files_attaches_even_when_the_prompt_is_short(self):
        t = self._T()
        ws._send(t, "zai", self._build, lambda m: None, force_files=True)
        site, text, paths = t.calls[0]
        self.assertEqual(text, "prompt with attachment")
        self.assertEqual(len(paths), 1)

    def test_short_prompt_stays_inline_by_default(self):
        t = self._T()
        ws._send(t, "zai", self._build, lambda m: None)
        self.assertEqual(t.calls[0][1:], ("inline prompt", []))

    def test_inline_only_site_is_never_given_files(self):
        t = self._T()
        ws._send(t, "qwen", self._build, lambda m: None, force_files=True)
        self.assertEqual(t.calls[0][1:], ("inline prompt", []))


class FullStyleInlineTests(unittest.TestCase):
    def test_style_limit_cuts_by_default_and_not_when_lifted(self):
        long_style = "Hook pattern: " + ("x" * 7000) + " END-OF-GUIDE"
        cut = studio.script_prompt("T", "g", "brief", style_guide=long_style,
                                   target_words=1000)
        full = studio.script_prompt("T", "g", "brief", style_guide=long_style,
                                    target_words=1000, style_limit=10 ** 6)
        self.assertNotIn("END-OF-GUIDE", cut)
        self.assertIn("END-OF-GUIDE", full)
