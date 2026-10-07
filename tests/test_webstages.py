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
from whisperradar import db, external_prompts as ep, studio, webstages as ws  # noqa: E402
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

    def test_local_code_faults_block_a_judge_pass(self):
        ok = {"faults": [], "shots": [], "detailed_ratio": 1, "pass": True}
        t = Fake(zai=[plan_json()], deepseek=[json.dumps(ok)])
        with self.assertRaises(ws.StageFailed):
            self.run_loop(t, rounds=1, local=["hold too long at cue 2"])

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
        self.assertEqual(seen[2][1], {"deepthink": True, "search": False})


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
                                            "search": False})

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


class AutoRefreshTests(unittest.TestCase):
    def test_the_poll_marks_a_job_that_started_after_the_page_loaded(self):
        html = (ROOT / "whisperradar" / "templates"
                / "studio_detail.html").read_text("utf-8")
        run_block = html[html.index("if (s.running) {"):]
        self.assertIn("wasRunning = true;", run_block[:200])
        # only the user's own typing counts as an unsaved edit
        self.assertIn("e.isTrusted", html)
