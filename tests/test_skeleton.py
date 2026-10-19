"""Shotlist skeleton: the app drafts the shots, the writer answers with lines,
the app assembles the JSON (no browser, no network)."""
import collections
import json
import random
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_external_prompts import Base  # noqa: E402
from whisperradar import briefs, db, skeleton as sk, studio, webstages as ws  # noqa: E402


def _ts(x):
    return "%02d:%02d:%06.3f" % (int(x // 3600), int(x % 3600 // 60), x % 60)


def make_cues(n=120, seed=1, lo=1.5, hi=3.5):
    rnd = random.Random(seed)
    texts = ["The market opened lower today.", "Prices fell by 12 percent.",
             "Nobody expected that.", "Analysts blamed rates, and",
             "the cost of borrowing.", "By noon it had recovered.",
             "What happens next matters."]
    cues, t = [], 0.0
    for i in range(1, n + 1):
        d = rnd.uniform(lo, hi)
        cues.append({"index": i, "start": _ts(t), "end": _ts(t + d),
                     "text": rnd.choice(texts)})
        t += d
    return cues


def answer_all(shots, every=3):
    """A stand-in writer: every `every`-th shot reuses the picture before it."""
    ans = sk.Answers()
    for s in shots:
        prev_ok = s.n - 1 in ans.new and not ans.new[s.n - 1]["anim"]
        if s.animate or s.n % every != 0 or not prev_ok:
            ans.new[s.n] = {"type": "SCN", "refs": [],
                            "prompt": f"prompt for shot {s.n}",
                            "anim": "slow push in" if s.animate else ""}
        else:
            ans.reuse[s.n] = s.n - 1
    return ans


class DraftTests(unittest.TestCase):
    def test_every_cue_is_covered_once_in_order(self):
        cues = make_cues()
        shots = sk.draft_shots(cues, 12.0)
        seen = []
        for s in shots:
            seen.extend(range(s.first, s.last + 1))
        self.assertEqual(seen, [c["index"] for c in cues])

    def test_no_shot_holds_longer_than_the_maximum(self):
        for cap in (6.0, 10.0, 12.0, 18.0):
            shots = sk.draft_shots(make_cues(lo=1.0, hi=4.0), cap)
            self.assertTrue(all(s.seconds <= cap + 0.05 for s in shots), cap)

    def test_a_number_starts_a_new_shot(self):
        cues = make_cues(8, lo=2.0, hi=2.0)
        cues[0]["text"] = "He walked in."
        cues[1]["text"] = "He sat down."
        cues[2]["text"] = "47 percent of them agreed."
        shots = sk.draft_shots(cues, 20.0)
        self.assertEqual(shots[0].last, 2)
        self.assertEqual(shots[1].first, 3)

    def test_animated_opening_is_short_and_zoomed(self):
        prof = briefs.get_profile("standard")
        shots = sk.build(make_cues(), prof, 12.0, anim_last_cue=8)
        opening = [s for s in shots if s.animate]
        self.assertTrue(opening)
        self.assertTrue(all(s.seconds <= sk.ANIM_MAX_HOLD + 0.05
                            for s in opening))
        self.assertTrue(all(s.motion in ("ZI", "ZO") for s in opening))
        self.assertTrue(all(not s.animate for s in shots[len(opening):]))


class MotionTests(unittest.TestCase):
    def test_motion_respects_the_channels_rules(self):
        for key in ("standard", "static", "long_holds", "flow_safe",
                    "zooms_only", "documentary"):
            prof = briefs.get_profile(key)
            cap = prof.max_hold or 12.0
            shots = sk.build(make_cues(), prof, cap)
            if prof.allowed:
                self.assertTrue(all(s.motion in prof.allowed for s in shots),
                                key)
            if prof.st_max_hold is not None:
                self.assertTrue(all(
                    s.seconds <= briefs.hold_limit(prof.st_max_hold,
                                                   briefs.ST_HOLD_TOLERANCE)
                    for s in shots if s.motion == "ST"), key)

    def test_st_eligibility_carries_the_hold_tolerance(self):
        prof = briefs.get_profile("standard")
        prof = prof.__class__(**{**prof.__dict__, "st_max_hold": 5.0,
                                 "static_long_hold": None})
        for sec in (3.0, 5.0, 5.9):               # up to 5 s + 20%
            self.assertTrue(sk.st_fits(sec, prof), sec)
        for sec in (6.0, 7.0, 9.0):
            self.assertFalse(sk.st_fits(sec, prof), sec)
        prof = prof.__class__(**{**prof.__dict__, "st_max_hold": None,
                                 "static_long_hold": 10.0})
        self.assertTrue(sk.st_fits(11.0, prof))     # 10 s floor + 20% = 12 s
        self.assertFalse(sk.st_fits(12.0, prof))

    def test_assign_motion_puts_st_on_a_shot_within_tolerance_only(self):
        prof = briefs.get_profile("standard")
        prof = prof.__class__(**{**prof.__dict__, "st_max_hold": 5.0,
                                 "static_long_hold": None})
        cues = make_cues(60, lo=6.0, hi=6.5)       # every shot is ~6 s: over 5, within 7
        shots = sk.build(cues, prof, 12.0)
        for s in shots:
            if s.motion == "ST":
                self.assertLessEqual(s.seconds, 7.0)
        long_cues = make_cues(60, lo=8.0, hi=8.5)  # beyond the tolerance
        shots = sk.build(long_cues, prof, 12.0)
        self.assertFalse([s for s in shots if s.motion == "ST"])


class ParseTests(unittest.TestCase):
    REPLY = """Here you go:
```
BEAT 1
1 | SCN | CH_MAYA, bg_room | A woman at a desk, lit from the left || slow push in
**2** | REUSE 1
3 | inf | - | A diagram of three stacked coins
- 4 | HYB | MAYA | She points at the chart
REF CH_MAYA | A woman in her thirties, grey coat
```
"""

    def test_lines_are_read_through_markdown_noise(self):
        ans = sk.parse_reply(self.REPLY)
        self.assertEqual(sorted(ans.new), [1, 3, 4])
        self.assertEqual(ans.reuse, {2: 1})
        self.assertEqual(ans.new[1]["refs"], ["CH_MAYA", "bg_room"])
        self.assertEqual(ans.new[1]["anim"], "slow push in")
        self.assertEqual(ans.new[3]["type"], "INF")
        self.assertEqual(ans.new[3]["refs"], [])
        self.assertEqual(ans.beats, {1})
        self.assertIn("CH_MAYA", ans.refs)

    def test_covers_needs_every_shot_of_the_batch(self):
        shots = sk.draft_shots(make_cues(20), 12.0)
        part = answer_all(shots)
        text = "\n".join(
            f"{n} | SCN | - | p{n}" for n in sorted(part.new)[:-1])
        self.assertFalse(sk.covers(text, shots))


class ProblemTests(unittest.TestCase):
    def setUp(self):
        self.prof = briefs.get_profile("standard")
        self.shots = sk.build(make_cues(40), self.prof, 12.0)

    def test_complete_answers_have_no_problems(self):
        ans = answer_all(self.shots)
        self.assertEqual(sk.problems(self.shots, ans, self.prof, True, set()),
                         [])

    def test_missing_line_unknown_ref_and_bad_reuse_are_named(self):
        ans = answer_all(self.shots)
        del ans.new[2]
        ans.new[4]["refs"] = ["NOBODY"]
        ans.reuse[5] = 9                     # forward reference
        ans.new.pop(5, None)
        found = sk.problems(self.shots, ans, self.prof, True, set())
        text = " | ".join(found)
        self.assertIn("shot 2: no line", text)
        self.assertIn("shot 4: ref NOBODY", text)
        self.assertIn("shot 5: REUSE 9", text)

    def test_only_limits_the_check_to_one_batch(self):
        ans = sk.Answers()
        found = sk.problems(self.shots, ans, self.prof, True, set(),
                            only={1, 2})
        self.assertEqual(len(found), 2)

    def test_refs_off_rejects_refs(self):
        ans = answer_all(self.shots)
        ans.new[1]["refs"] = ["CH_X"]
        found = sk.problems(self.shots, ans, self.prof, False, set())
        self.assertTrue(any("refs are off" in f for f in found))

    def test_an_animated_shot_needs_its_note_and_its_own_image(self):
        shots = sk.build(make_cues(40), self.prof, 12.0, anim_last_cue=6)
        ans = answer_all(shots)
        first = next(s for s in shots if s.animate)
        ans.new[first.n]["anim"] = ""
        found = sk.problems(shots, ans, self.prof, True, set())
        self.assertTrue(any("no `|| note`" in f for f in found))


class AssembleTests(unittest.TestCase):
    def test_assembled_plan_passes_the_apps_own_checks(self):
        cues = make_cues()
        for key in ("standard", "static", "flow_safe", "zooms_only",
                    "documentary"):
            prof = briefs.get_profile(key)
            cap = prof.max_hold or 12.0
            shots = sk.build(cues, prof, cap, anim_last_cue=6)
            ans = answer_all(shots)
            self.assertEqual(sk.problems(shots, ans, prof, False, set()), [])
            data = sk.assemble(shots, ans, "STYLE", [], False, prof)
            self.assertEqual(
                studio.shotlist_structural_faults(data, len(cues)), [], key)
            self.assertEqual(
                studio.shotlist_pacing(data, cues, cap, prof)[0], [], key)

    def test_names_scenes_and_reused_files(self):
        prof = briefs.get_profile("static")
        shots = sk.build(make_cues(30), prof, 12.0)
        ans = answer_all(shots, every=2)
        ans.beats = {1, shots[len(shots) // 2].n}
        data = sk.assemble(shots, ans, "STYLE", [], False, prof)
        files = [i["file"] for i in data["images"]]
        self.assertEqual(len(files), len(set(files)))
        self.assertTrue(all(re.match(r"^S\d\d_\d\d_[A-Z]+_[A-Z]+\.png$", f)
                            for f in files))
        self.assertEqual({s["scene"] for s in data["shots"]}, {"S01", "S02"})
        assets = {s["asset"] for s in data["shots"]}
        self.assertEqual(assets, set(files))
        self.assertEqual(list(data)[-2:], ["shots", "images"])
        self.assertEqual(data["style"], "STYLE")

    def test_refs_registry_supplied_and_invented(self):
        prof = briefs.get_profile("static")
        shots = sk.build(make_cues(30), prof, 12.0)
        ans = answer_all(shots, every=99)
        ans.new[1]["refs"] = ["MAYA", "ch_cat"]
        ans.refs["CH_CAT"] = "a grey tabby cat alone"
        self.assertEqual(sk.problems(shots, ans, prof, True, {"MAYA"}), [])
        data = sk.assemble(shots, ans, "S", ["MAYA.png", "BOB.png"], True,
                           prof)
        self.assertEqual(data["images"][0]["refs"], ["MAYA", "CH_CAT"])
        self.assertEqual(data["refs"], {"MAYA": "refs/MAYA.png",
                                        "BOB": "refs/BOB.png",
                                        "CH_CAT": None})
        self.assertEqual(data["refPrompts"],
                         {"CH_CAT": "a grey tabby cat alone"})
        self.assertEqual(studio.shotlist_structural_faults(
            data, 30)[:1] and [], [])


class PromptTests(unittest.TestCase):
    def test_rules_are_short_and_carry_the_creative_rules(self):
        prof = briefs.get_profile("standard")
        rules = sk.rules_text(prof, True, ["MAYA.png"])
        self.assertLess(len(rules), 15000)
        self.assertIn("IMAGE CONTENT POLICY", rules)
        self.assertIn("EMOTIONAL REACTION", rules)
        self.assertIn("MAYA", rules)
        self.assertIn("REUSE", rules)
        self.assertNotIn("narration.txt", rules)

    def test_first_prompt_holds_the_lines_and_later_ones_only_lines(self):
        prof = briefs.get_profile("standard")
        shots = sk.build(make_cues(80), prof, 12.0)
        batches = sk.chunks(shots)
        self.assertGreater(len(batches), 1)
        first = sk.first_prompt(sk.rules_text(prof, False, []), "STYLE",
                                "BIBLE", "DIRECTION", batches[0], len(shots))
        self.assertIn(sk.shot_line(batches[0][0]), first)
        self.assertIn("STYLE", first)
        self.assertIn("BIBLE", first)
        self.assertIn("DIRECTION", first)
        nxt = sk.next_prompt(batches[1], len(shots))
        self.assertIn(sk.shot_line(batches[1][0]), nxt)
        self.assertLess(len(nxt), 0.4 * len(first))
        self.assertNotIn("IMAGE CONTENT POLICY", nxt)


class SupportTests(unittest.TestCase):
    def test_reveal_and_sfx_select_legacy_flow(self):
        from dataclasses import replace
        prof = briefs.get_profile("standard")
        self.assertEqual(sk.unsupported_reason({"brief_presentation": "No INF"}, prof), "")
        self.assertIn("reveal", sk.unsupported_reason({}, replace(prof, reveal=True)))
        self.assertIn("sound", sk.unsupported_reason({}, replace(prof, sfx="ding")))


class RunTests(Base):
    def setUp(self):
        super().setUp()
        self.log = []
        cues = make_cues(60)
        (self.pdir / "subtitles.srt").write_text("\n".join(
            f"{c['index']}\n{c['start']} --> {c['end']}\n{c['text']}\n" for c in cues), "utf-8")

    class Writer:
        def __init__(self):
            self.calls = []
            self.groups = ""
            self.reject = 0
        def adopt(self, site, url): pass
        def urls(self): return {}
        def ask(self, site, prompt, files=(), new_chat=True, ready=None):
            self.calls.append((site, prompt, new_chat))
            if site == "deepseek":
                if "grouping judge" in prompt:
                    ranges=[(int(a),int(b)) for a,b in re.findall(r"^GROUP (\d+)-(\d+):",prompt,re.M)]
                    if self.reject:
                        self.reject -= 1
                        a,b=ranges[0]
                        mid=(a+b)//2
                        ranges=[(a,mid),(mid+1,b)]+ranges[1:]
                    return "\n".join(f"{a}-{b}" for a,b in ranges)
                return json.dumps({"faults": [], "shots": [], "detailed_ratio": 1, "pass": True})
            if "you ONLY decide how" in prompt:
                rows = [(int(m[1]), float(m[2])) for m in re.finditer(
                    r"^(\d+) \| ([\d.]+)s \|", prompt, re.M)]
                out, start, total = [], rows[0][0], 0
                for n, sec in rows:
                    if total and total + sec > 20:
                        out.append(f"{start}-{n-1}");start,total=n,0
                    total += sec
                out.append(f"{start}-{rows[-1][0]}")
                self.groups="\n".join(out)
                return self.groups
            if "Not accepted yet:" in prompt: return self.groups
            return "\n".join(f"{m[1]} | SCN | - | A person beside basket number {m[1]} on white." for m in re.finditer(
                r"^(\d+) \| cues [\d-]+ \| [\d.]+s(?: \(.*?\))? \|", prompt, re.M))

    def test_short_brief_is_inline_for_grouping_writer_and_judge(self):
        class Capture(self.Writer):
            def __init__(self):
                super().__init__();self.seen=[]
            def ask(self,site,prompt,files=(),new_chat=True,ready=None):
                if 'you ONLY decide how' in prompt or 'grouping judge' in prompt:
                    self.seen.append((site,prompt,list(files)))
                return super().ask(site,prompt,files,new_chat,ready)
        t=Capture()
        ws.run_shotlist(self.cfg,self.pid,t,'zai','deepseek',rounds=1,
                       log=self.log.append,use_skeleton=True)
        self.assertEqual(len(t.seen),2)
        for site,prompt,files in t.seen:
            self.assertEqual(files,[])
            self.assertIn('HOW TO GROUP',prompt)
            self.assertIn('CUE GROUPING ONLY',prompt)
            self.assertNotIn('SECTION',prompt)
            self.assertNotIn('grouping_brief.md',prompt)

    def test_presentation_reaches_grouping_judge_and_picture_writer(self):
        conn=db.connect(self.cfg.db_path)
        db.update_own_channel(conn,self.chan,brief_presentation="No INF; show the protagonist")
        conn.commit();conn.close()
        t=self.Writer()
        ws.run_shotlist(self.cfg,self.pid,t,'zai','deepseek',rounds=1,
                       log=self.log.append,use_skeleton=True)
        for idx in [0,1,2]:
            self.assertIn('No INF; show the protagonist',t.calls[idx][1])
        self.assertFalse(any('whole shotlist as before' in x for x in self.log))

    def test_skeleton_error_does_not_silently_start_full_planner(self):
        from unittest import mock
        t=self.Writer()
        with mock.patch.object(ws,'_skeleton_plan',side_effect=sk.SkeletonError('bad pictures')):
            with self.assertRaisesRegex(ws.StageFailed,'full-shotlist planner was not started'):
                ws.run_shotlist(self.cfg,self.pid,t,'zai','deepseek',rounds=1,
                               log=self.log.append,use_skeleton=True)
        self.assertEqual(t.calls,[])

    def test_complete_group_review_picture_flow(self):
        t=self.Writer()
        data=ws.run_shotlist(self.cfg,self.pid,t,"zai","deepseek",rounds=1,
                            log=self.log.append,use_skeleton=True)
        self.assertIn("CUES 1-60",t.calls[0][1])
        self.assertIn("grouping judge",t.calls[1][1])
        self.assertIn("The app applied your grouping",t.calls[2][1])
        self.assertFalse(t.calls[2][2])
        self.assertLess(len(data['images']),len(data['shots']))
        self.assertEqual(studio.shotlist_structural_faults(data,60),[])

    def test_judge_list_replaces_the_writers_groups_without_writer_roundtrip(self):
        t=self.Writer();t.reject=1
        data=ws.run_shotlist(self.cfg,self.pid,t,'zai','deepseek',rounds=1,
                            log=self.log.append,use_skeleton=True)
        calls=[c for c in t.calls if c[0]=='deepseek']
        self.assertEqual(sum('grouping judge' in c[1] for c in calls),1)
        self.assertTrue(calls[0][2])
        self.assertTrue(all(not c[2] for c in calls[1:]))
        self.assertTrue(any("the judge's list changed" in x for x in self.log))
        self.assertFalse(any('Not accepted yet:' in c[1] for c in t.calls))
        self.assertEqual(studio.shotlist_structural_faults(data,60),[])

    def test_approved_groups_are_logged_as_approved(self):
        t=self.Writer()
        ws.run_shotlist(self.cfg,self.pid,t,'zai','deepseek',rounds=1,
                       log=self.log.append,use_skeleton=True)
        self.assertTrue(any('the judge approved the groups' in x for x in self.log))

    def test_adopted_judge_chat_is_continued_from_first_group_review(self):
        from unittest import mock
        t=self.Writer()
        with mock.patch.object(ws,'adopt_chats',return_value={'zai','deepseek'}):
            ws.run_shotlist(self.cfg,self.pid,t,'zai','deepseek',rounds=1,
                           log=self.log.append,use_skeleton=True,same_chats=True)
        self.assertTrue(all(not c[2] for c in t.calls if c[0]=='deepseek'))

    def test_unusable_judge_list_keeps_the_writers_groups(self):
        for reply in ('1-999', 'Looks fine to me.', '1-5\n6-10'):
            class BadJudge(self.Writer):
                def ask(inner,site,prompt,files=(),new_chat=True,ready=None):
                    if site=='deepseek' and 'grouping judge' in prompt:
                        inner.calls.append((site,prompt,new_chat))
                        return reply
                    return super().ask(site,prompt,files,new_chat,ready)
            self.log.clear()
            t=BadJudge()
            data=ws.run_shotlist(self.cfg,self.pid,t,'zai','deepseek',rounds=1,
                                log=self.log.append,use_skeleton=True)
            self.assertEqual(sum('grouping judge' in c[1] for c in t.calls),1,reply)
            self.assertTrue(any("keeping the writer's grouping" in x for x in self.log),reply)
            self.assertTrue(any('The app applied your grouping' in c[1] for c in t.calls),reply)
            self.assertEqual(studio.shotlist_structural_faults(data,60),[])

    def test_small_gaps_are_repaired_and_logged(self):
        class Gappy(self.Writer):
            def ask(inner,site,prompt,files=(),new_chat=True,ready=None):
                reply=super().ask(site,prompt,files,new_chat,ready)
                if 'you ONLY decide how' in prompt:
                    lines=reply.split("\n")
                    a,b=lines[0].split("-")
                    lines[0]=f"{a}-{int(b)-1}"        # one cue left out
                    inner.groups="\n".join(lines)
                    return inner.groups
                return reply
        t=Gappy()
        data=ws.run_shotlist(self.cfg,self.pid,t,'zai','deepseek',rounds=1,
                            log=self.log.append,use_skeleton=True)
        self.assertTrue(any("the app repaired the writer's groups" in x for x in self.log))
        self.assertEqual(studio.shotlist_structural_faults(data,60),[])

    def test_a_grouping_that_leaves_out_many_cues_fails_before_the_judge(self):
        class Lazy(self.Writer):
            def ask(inner,site,prompt,files=(),new_chat=True,ready=None):
                if 'you ONLY decide how' in prompt:
                    inner.calls.append((site,prompt,new_chat))
                    return "1-5"
                return super().ask(site,prompt,files,new_chat,ready)
        t=Lazy()
        with self.assertRaisesRegex(ws.StageFailed,'left .* of the cues out'):
            ws.run_shotlist(self.cfg,self.pid,t,'zai','deepseek',rounds=1,
                            log=self.log.append,use_skeleton=True)
        self.assertFalse(any('grouping judge' in c[1] for c in t.calls))

    def test_judge_hold_arithmetic_is_ignored_under_any_label(self):
        faults=["MOTION MIX: shot S01_01_SCN_ZI.png (cues 1-3) is ST and holds 5s, over the 4.8s limit for a static shot.",
                "MAXIMUM ST HOLD: S01_02_SCN_ZO.png, shot cues 4-6, is ST and holds 5s, over the 4.8s limit for static shots."]
        class FalseHold(self.Writer):
            def ask(inner,site,prompt,files=(),new_chat=True,ready=None):
                if site=='deepseek' and 'grouping judge' not in prompt:
                    inner.calls.append((site,prompt,new_chat))
                    return json.dumps({"faults":faults,"shots":[],"detailed_ratio":1.0,"pass":False,
                        "fixes":["Change the motion of cues 1-3 from ST to ZO."]})
                return super().ask(site,prompt,files,new_chat,ready)
        t=FalseHold()
        data=ws.run_shotlist(self.cfg,self.pid,t,'zai','deepseek',rounds=1,
                            log=self.log.append,use_skeleton=True)
        self.assertTrue(any('ignored 2 judge fault(s)' in x for x in self.log))
        self.assertEqual(studio.shotlist_structural_faults(data,60),[])

    def test_stop_after_judge_prevents_picture_generation(self):
        t=self.Writer()
        with self.assertRaisesRegex(ws.StageFailed,'stopped by user'):
            ws.run_shotlist(self.cfg,self.pid,t,'zai','deepseek',rounds=1,
                            log=self.log.append,use_skeleton=True,
                            should_stop=lambda: len(t.calls)>=2)
        self.assertEqual(len(t.calls),2)



class GroupingTests(unittest.TestCase):
    def setUp(self):
        self.cues=make_cues(20,lo=2,hi=2)
        self.prof=briefs.STANDARD

    def test_subsecond_tail_folds_into_the_previous_shot(self):
        # prod 40 cue 298: "demand." = a 0.34s ST flicker after a ZI shot
        cues = make_cues(10, lo=2, hi=2)
        cues[9]['end'] = cues[9]['start'][:7] + '4.340'   # cue 10 = 0.34s
        shots, pictures = sk.regroup(cues, [(1, 5), (6, 10)],
                                     self.prof, 12.0)
        self.assertFalse(any(s.last == 10 and s.first == 10
                             for s in shots), [(s.n, s.cues) for s in shots])
        merged = next(s for s in shots if s.last == 10)
        self.assertGreater(merged.seconds, 1.0)
        # the writer's picture boundary is respected: the fold happens inside
        # the group the tail belongs to
        self.assertEqual([p.last for p in pictures][-1], 10)

    def test_picture_start_tails_are_protected(self):
        # a sub-second shot that STARTS a picture is the writer's boundary
        cues = make_cues(10, lo=2, hi=2)
        cues[9]['end'] = cues[9]['start'][:7] + '4.340'
        shots, pictures = sk.regroup(cues, [(1, 9), (10, 10)],
                                     self.prof, 12.0)
        self.assertTrue(any(p.first == 10 and p.last == 10
                            for p in pictures))

    def test_grouping_brief_prefers_completed_thoughts(self):
        self.assertIn("completes the", sk.grouping_brief())
        self.assertIn("They are", sk.grouping_brief())

    def test_coverage_rejects_gaps_overlap_unknown_and_reverse(self):
        for groups in [[],[(1,19)],[(1,10),(10,20)],[(1,10),(12,20)],[(20,1)],[(1,21)]]:
            self.assertTrue(sk.group_problems(self.cues,groups,100),groups)
        self.assertEqual(sk.group_problems(self.cues,[(1,20)],100),[])
        self.assertTrue(sk.group_problems(self.cues,[(1,20)],12))
    def test_long_group_reuses_picture_across_camera_shots(self):
        shots,pictures=sk.regroup(self.cues,[(1,10),(11,20)],self.prof,12)
        self.assertEqual(len(pictures),2)
        self.assertGreater(len(shots),2)
        ans=sk.expand(pictures,answer_all(pictures,every=99))
        data=sk.assemble(shots,ans,'STYLE',[],False,self.prof)
        self.assertEqual(len(data['images']),2)
        self.assertEqual(studio.shotlist_structural_faults(data,20),[])
        self.assertEqual(pictures[0].text,' '.join(c['text'] for c in self.cues[:10]))
    def test_animated_opening_is_separate(self):
        shots,pictures=sk.regroup(self.cues,[(5,12),(13,20)],self.prof,12,4)
        self.assertEqual(sk.groupable(self.cues,4)[0]['index'],5)
        self.assertTrue(all(p.last<=4 and len(p.members)==1 for p in pictures if p.animate))
        self.assertEqual([i for s in shots for i in range(s.first,s.last+1)],list(range(1,21)))
    def test_reuse_of_an_earlier_picture_maps_to_correct_shots(self):
        shots,pictures=sk.regroup(self.cues,[(1,10),(11,20)],self.prof,12)
        ans=answer_all(pictures,every=99)
        ans.new.pop(2);ans.reuse[2]=1
        data=sk.assemble(shots,sk.expand(pictures,ans),'STYLE',[],False,self.prof)
        self.assertEqual(len(data['images']),1)
        self.assertEqual(len({s['asset'] for s in data['shots']}),1)
        self.assertEqual(studio.shotlist_structural_faults(data,20),[])

    def test_grouping_reply_can_mark_main_beats(self):
        groups, beats = sk.parse_grouping("1-4\nBEAT 5-8\n9-12\nbeat 13-20")
        self.assertEqual(groups, [(1, 4), (5, 8), (9, 12), (13, 20)])
        self.assertEqual(beats, {5, 13})
        self.assertEqual(sk.parse_groups("1-4\nBEAT 5-8"), [(1, 4), (5, 8)])
        # plain parse ignores a BEAT that trails a bare single cue
        self.assertEqual(sk.parse_groups("5"), [(5, 5)])

    def test_group_prompts_ask_for_beat_markers(self):
        text = sk.group_prompt("T", "g", self.cues, 12)
        self.assertIn("MAIN BEAT", text)
        self.assertIn("`BEAT 45-48`", text)
        self.assertIn("MAIN BEAT", sk.group_judge_prompt(self.cues, [(1, 10)], 12))
        self.assertIn("keep those markers", sk.group_judge_prompt(self.cues, [(1, 10)], 12))

    def test_reply_format_drops_beat_lines_when_grouping_decided_scenes(self):
        rules_grouped = sk.rules_text(self.prof, False, [], grouped=True,
                                      beats_from_grouping=True)
        self.assertIn("do not write `BEAT <n>` lines", rules_grouped)
        self.assertNotIn("put a line `BEAT <n>`", rules_grouped)
        # ungrouped keeps the writer's own BEAT-line instruction
        self.assertIn("put a line `BEAT <n>`", sk.rules_text(self.prof, False, []))
        # grouped without markers (fallback) keeps it too
        self.assertIn("put a line `BEAT <n>`",
                      sk.rules_text(self.prof, False, [], grouped=True))

    def test_marker_on_the_first_group_does_not_skip_s01(self):
        # prod 43 replay: a writer sometimes marks the first group; the first
        # chapter starts implicitly and the marker must not count again
        groups, beats = sk.parse_grouping("BEAT 1-8\n9-16\nBEAT 17-25\n26-40")
        self.assertIn(1, beats)
        beats.discard(groups[0][0])
        self.assertEqual(beats, {17})
        shots, pictures = sk.regroup(self.cues, groups, self.prof, 12)
        scenes = {m: 1 + sum(1 for s in sorted(beats) if s <= p.first)
                  for p in pictures for m in p.members}
        self.assertEqual(scenes[pictures[0].members[0]], 1)

    def test_repair_moving_a_marker_snaps_it_forward(self):
        # a repair merged 5-8 into 1-8: the marker on cue 5 must survive at
        # the next group start (9), not disappear without a message
        markers, moved = sk.snap_beat_starts({5, 17},
                                             [(1, 8), (9, 12), (13, 16), (17, 20)])
        self.assertEqual(markers, {9, 17})
        self.assertEqual(moved, 1)
        # a marker that stays on its own group start is not "moved"
        markers, moved = sk.snap_beat_starts({9, 17},
                                             [(1, 8), (9, 12), (13, 16), (17, 20)])
        self.assertEqual(markers, {9, 17})
        self.assertEqual(moved, 0)
        # a marker that snaps onto the first group is chapter 1: dropped,
        # not moved (chapter 1 is implicit, nothing re-anchored)
        markers, moved = sk.snap_beat_starts({1, 9}, [(1, 8), (9, 20)])
        self.assertEqual(markers, {9})
        self.assertEqual(moved, 0)

    def test_scene_cap_splits_a_run_that_swallowed_the_video(self):
        groups = [(1, 4), (5, 8), (9, 12), (13, 16), (17, 20)]
        shots, pictures = sk.regroup(self.cues, groups, self.prof, 12)
        # the writer marked nothing: one scene holds every shot (S13_01...54)
        scene_of = {s.n: 1 for s in shots}
        # every picture is a NEW image, so the cap has boundaries to split at
        ans = sk.expand(pictures, answer_all(pictures, every=99))
        capped = sk._cap_scene_size(shots, scene_of, ans, cap=3)
        runs = collections.Counter(capped.values())
        # scenes relabel 1..K in narration order; every split lands on a
        # NEW-image boundary (a run may end past the cap on a reuse tail -
        # the next boundary is where the split happens). What must never
        # come back is the S13_01...54 shape: one scene holding everything.
        seq = [capped[s.n] for s in shots]
        self.assertEqual(seq, sorted(seq))
        self.assertEqual(len(runs), 3)
        for sc in sorted(set(seq))[:-1]:
            last = max(s.n for s in shots if capped[s.n] == sc)
            nxt = next(s for s in shots if s.n == last + 1)
            self.assertIn(nxt.n, ans.new,
                          f"scene {sc} split away from a reuse shot")
        # a single picture reused across a long run cannot split: no image
        # boundary means one scene keeps its asset under one label
        ans2 = sk.expand(pictures, answer_all(pictures, every=99))
        ans2.new = {k: v for k, v in ans2.new.items() if k == 1}
        ans2.reuse = {s.n: 1 for s in shots if s.n != 1}
        scene_of = {s.n: 1 for s in shots}
        capped2 = sk._cap_scene_size(shots, scene_of, ans2, cap=3)
        self.assertEqual(set(capped2.values()), {1})

    def test_grouping_beat_markers_become_scenes(self):
        shots, pictures = sk.regroup(self.cues, [(1, 6), (7, 12), (13, 20)],
                                     self.prof, 12)
        self.assertEqual(len(pictures), 3)
        ans = sk.expand(pictures, answer_all(pictures, every=99))
        # beat markers on the second and third groups: three scenes
        ordered = [7, 13]
        scenes = {m: 1 + sum(1 for s in ordered if s <= p.first)
                  for p in pictures for m in p.members}
        data = sk.assemble(shots, ans, 'STYLE', [], False, self.prof,
                           scenes=scenes)
        self.assertEqual({s['scene'] for s in data['shots']},
                         {'S01', 'S02', 'S03'})
        self.assertEqual(len({s['asset'] for s in data['shots']}), 3)
        for p, scene in zip(pictures, ('S01', 'S02', 'S03')):
            member_assets = [s['asset'] for s in data['shots']
                             if int(str(s['cues']).split('-')[0]) >= p.first
                             and int(str(s['cues']).split('-')[-1]) <= p.last]
            self.assertTrue(all(a.startswith(scene + '_')
                                for a in member_assets), member_assets)
        self.assertEqual(studio.shotlist_structural_faults(data, 20), [])
        # without scenes the writer's own BEAT lines decide (unchanged)
        ans2 = sk.expand(pictures, answer_all(pictures, every=99))
        ans2.beats = {7}
        data2 = sk.assemble(shots, ans2, 'STYLE', [], False, self.prof)
        self.assertEqual({s['scene'] for s in data2['shots']}, {'S01', 'S02'})

    def test_local_coverage_and_duration_repair(self):
        for ranges in [[],[(4,8),(7,12)],[(20,1)],[(1,20)]]:
            result,errors=sk.repair_groups(self.cues,ranges,4)
            self.assertEqual(errors,[])
            self.assertEqual(sk.group_problems(self.cues,result,4),[])
        result,errors=sk.repair_groups(self.cues,[(1,999)],12)
        self.assertTrue(errors)

    def test_judge_sees_actual_grouped_text_and_returns_a_complete_list(self):
        text=sk.group_judge_prompt(self.cues,[(1,10),(11,20)],12)
        self.assertIn('GROUP 1-10:',text)
        self.assertIn('[1] '+self.cues[0]['text'],text)
        self.assertIn('Cues are indivisible',text)
        self.assertIn('COMPLETE corrected list',text)
        self.assertNotIn('"replace"',text)
        self.assertNotIn('JSON}',text)
        self.assertNotIn('\nCUES:\n',text)

    def test_creator_rules_reach_the_picture_writer_only(self):
        from dataclasses import replace
        prof = replace(briefs.STANDARD,
                       custom_rules="keep the presenter visible throughout the move")
        rules = sk.rules_text(prof, False, [])
        self.assertIn("CREATOR'S OWN MOTION RULES", rules)
        self.assertIn("keep the presenter visible throughout the move", rules)
        self.assertIn("already fixed each shot's code", rules)
        # a grouping prompt stays meaning-only: no rules text there
        gprompt = sk.group_prompt("T", "g", self.cues, 12,
                                  hold_note=sk.hold_range_note(prof, self.cues))
        self.assertNotIn("CREATOR'S OWN MOTION RULES", gprompt)
        self.assertNotIn("keep the presenter visible", gprompt)
        # channels without rules are unchanged
        self.assertNotIn("CREATOR'S OWN MOTION RULES",
                         sk.rules_text(briefs.STANDARD, False, []))

    def test_custom_rules_field_survives_resolve(self):
        spec = briefs.normalize_custom({
            "allowed": ["ST", "ZI", "ZO"],
            "rules": "keep the presenter visible throughout the move"})
        prof = briefs.custom_profile(spec)
        self.assertEqual(prof.custom_rules,
                         "keep the presenter visible throughout the move")
        self.assertIn("keep the presenter visible",
                      prof.slots["MOTION_SECTION"])
        # the standard profile has none
        self.assertEqual(briefs.STANDARD.custom_rules, "")

    def test_slow_paced_range_reaches_writer_and_grouping(self):
        from dataclasses import replace
        slow = replace(briefs.STANDARD, min_hold=10.0, max_hold=14.0)
        cues = make_cues(40, lo=2.0, hi=2.0)     # ~2 s per cue
        rules = sk.rules_text(slow, False, [], cues=cues)
        self.assertIn("PICTURES CARRY SHOTS", rules)
        self.assertIn("slow-paced", rules)
        self.assertIn("10-14s", rules)            # the channel's hold window
        self.assertIn("5-7 shots", rules)          # window / ~2 s per cue
        note = sk.hold_range_note(slow, cues)
        self.assertIn("slow-paced", note)
        self.assertIn("ceiling, not a target", note)
        gprompt = sk.group_prompt("T", "g", cues, 14, hold_note=note)
        self.assertIn("ceiling, not a target", gprompt)
        jprompt = sk.group_judge_prompt(cues, [(1, 10)], 14, hold_note=note)
        self.assertIn("ceiling, not a target", jprompt)
        # a standard (fast) channel keeps the old fixed heuristic and no note
        self.assertIn("two or three shots",
                      sk.rules_text(briefs.STANDARD, False, [], cues=cues))
        self.assertEqual(sk.hold_range_note(briefs.STANDARD, cues), "")

    def test_hold_range_note_needs_both_ends(self):
        from dataclasses import replace
        self.assertEqual(sk.hold_range_note(briefs.STANDARD), "")
        self.assertEqual(sk.hold_range_note(
            replace(briefs.STANDARD, min_hold=8.0)), "")

    def test_short_brief_and_direction_reach_both_roles(self):
        for text in [sk.group_prompt('T','g',self.cues,12,direction='Keep comparisons together'),
                     sk.group_judge_prompt(self.cues,[(1,20)],12,'Keep comparisons together')]:
            self.assertIn('HOW TO GROUP',text)
            self.assertIn('Keep comparisons together',text)
            self.assertIn('number phrase',text)
            self.assertNotIn('THE TWO FAILURES',text)
            self.assertNotIn('NEW vs REUSE',text)
            self.assertNotIn('SECTION',text)
            self.assertNotIn('asset',text.split('CUES')[0].lower().replace('camera shots',''))
        self.assertLess(len(sk.grouping_brief()),1700)

    def test_uncovered_share(self):
        self.assertEqual(sk.uncovered_share(self.cues,[(1,20)]),0.0)
        self.assertAlmostEqual(sk.uncovered_share(self.cues,[(1,10)]),0.5)
        self.assertAlmostEqual(sk.uncovered_share(self.cues,[(1,9),(11,20)]),0.05)
        self.assertEqual(sk.uncovered_share(self.cues,[]),1.0)

    def test_pan_and_tilt_share_counts_shots_not_pictures(self):
        from dataclasses import replace
        cues=make_cues(240,lo=2,hi=2)
        prof=replace(briefs.STANDARD,group_caps=((("PU","PD"),0.30),))
        for size in (3,6,12):
            groups=[(i,min(i+size-1,240)) for i in range(1,241,size)]
            shots,pictures=sk.regroup(cues,groups,prof,12.0)
            ans=sk.expand(pictures,answer_all(pictures,every=99))
            data=sk.assemble(shots,ans,'S',[],False,prof)
            n=len(data['shots'])
            tilt=sum(1 for s in data['shots'] if s['motion'] in ('PU','PD'))
            self.assertLessEqual(tilt/n,0.31,(size,tilt,n))
            self.assertGreaterEqual(tilt/n,0.15,(size,tilt,n))

    def test_refs_off_rule_overrides_attachment_wording_in_the_bible(self):
        prof=briefs.STANDARD
        for grouped in (False,True):
            off=sk.rules_text(prof,False,[],grouped=grouped)
            self.assertIn('never mention an attachment',off)
            self.assertIn('attached reference',off)
            self.assertIn('same as picture 1',off)
            on=sk.rules_text(prof,True,['CH_A.png'],grouped=grouped)
            self.assertNotIn('never mention an attachment',on)

    def test_picture_rules_and_batches_speak_of_pictures(self):
        prof=briefs.STANDARD
        shots,pictures=sk.regroup(self.cues,[(1,10),(11,20)],prof,12)
        grouped=sk.rules_text(prof,False,[],grouped=True)
        plain=sk.rules_text(prof,False,[])
        self.assertIn('numbered PICTURES',grouped)
        self.assertNotIn('Prefer several shots per image',grouped)
        self.assertNotIn('numbered PICTURES',plain)
        self.assertIn('Prefer several shots per image',plain)
        block=sk.batch_block(pictures,len(pictures),first=True)
        self.assertTrue(block.startswith('PICTURES 1-2 of 2'))
        self.assertIn('one line per picture',block)
        self.assertTrue(sk.batch_block(shots[:2],len(shots),first=True).startswith('SHOTS 1-2'))


class MasterIntegrationTests(unittest.TestCase):
    """Things the skeleton must honour that the rest of the app added later:
    the Flow prompt budget (style + prompt) and the frame density."""

    def test_prompt_limit_follows_the_style_length(self):
        self.assertEqual(sk.prompt_limit(""), 2400)
        style = "x" * 1800
        self.assertEqual(sk.prompt_limit(style),
                         studio.FLOWBATCH_MAX_PROMPT_CHARS - 1801)
        self.assertGreaterEqual(sk.prompt_limit("x" * 5000), 200)

    def test_long_prompt_is_a_problem_with_a_long_style(self):
        shots = [sk.Draft(n=1, first=1, last=1, seconds=3.0, text="a",
                          motion="ZI")]
        ans = sk.parse_reply("1 | SCN | - | " + "p" * 900)
        prof = briefs.get_profile("standard")
        short = sk.problems(shots, ans, prof, False, set())
        long_ = sk.problems(shots, ans, prof, False, set(), style="s" * 1800)
        self.assertEqual(short, [])
        self.assertTrue(any("limit" in p for p in long_))

    def test_density_reaches_the_rules(self):
        prof = briefs.get_profile("standard")
        plain = sk.rules_text(prof, False, [])
        minimal = sk.rules_text(prof, False, [], density="minimal")
        self.assertNotIn(studio.density_planner_block("minimal").strip(),
                         plain)
        self.assertIn(studio.density_planner_block("minimal").strip(),
                      minimal)


class WebChatDensityTests(Base):
    """The web-chat planner and judge honour the frame density too (they used
    to ignore it - only the API path passed it on)."""
    channel_fields = {"visual_density": "minimal"}

    def test_planner_and_judge_prompts_carry_the_density(self):
        from whisperradar import external_prompts as ep
        want = studio.density_planner_block("minimal").strip()
        self.assertIn(want, ep.shotlist_planner_prompt(self.cfg, self.pid))
        judge, _ = ep.shotlist_judge_prompt(self.cfg, self.pid, "")
        self.assertIn("FRAME DENSITY", judge)


if __name__ == "__main__":
    unittest.main()
