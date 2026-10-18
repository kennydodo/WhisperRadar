"""Shotlist skeleton: the app drafts the shots, the writer answers with lines,
the app assembles the JSON (no browser, no network)."""
import json
import random
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_external_prompts import Base  # noqa: E402
from whisperradar import briefs, skeleton as sk, studio, webstages as ws  # noqa: E402


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
    def test_channels_with_reveals_sfx_or_presentation_use_the_old_flow(self):
        prof = briefs.get_profile("standard")
        self.assertEqual(sk.unsupported_reason({}, prof), "")
        self.assertIn("presentation",
                      sk.unsupported_reason({"brief_presentation": "x"}, prof))


class RunTests(Base):
    """run_shotlist with the skeleton on, against a writer that follows the
    line format."""

    def setUp(self):
        super().setUp()
        self.log = []
        blocks = []
        for c in make_cues(60):
            blocks.append(f"{c['index']}\n{c['start'].replace('.', ',')} --> "
                          f"{c['end'].replace('.', ',')}\n{c['text']}\n")
        (self.pdir / "subtitles.srt").write_text("\n".join(blocks), "utf-8")

    class Writer:
        def __init__(self):
            self.calls = []

        def adopt(self, site, url):
            pass

        def urls(self):
            return {}

        def ask(self, site, prompt, files=(), new_chat=True, ready=None):
            self.calls.append({"site": site, "prompt": prompt,
                               "new_chat": new_chat, "files": list(files)})
            if site == "deepseek":
                return json.dumps({"faults": [], "shots": [],
                                   "detailed_ratio": 1, "pass": True})
            out = []
            for m in re.finditer(r"^(\d+) \| cues [\d-]+ \| [\d.]+s \| "
                                 r"(\w+)( \| ANIMATE)? \| ", prompt, re.M):
                n = int(m.group(1))
                if m.group(3):
                    out.append(f"{n} | SCN | - | opening picture {n} || "
                               f"slow push in")
                elif n % 2 == 0:
                    out.append(f"{n} | REUSE {n - 1}")
                else:
                    out.append(f"{n} | SCN | - | picture {n} of the story")
            return "\n".join(out)

    def test_plan_is_built_from_lines_and_saved(self):
        t = self.Writer()
        data = ws.run_shotlist(self.cfg, self.pid, t, "zai", "deepseek",
                               rounds=2, log=self.log.append,
                               use_skeleton=True)
        saved = json.loads((self.pdir / "shotlist.json").read_text("utf-8"))
        self.assertEqual(saved["shots"], data["shots"])
        writer_calls = [c for c in t.calls if c["site"] == "zai"]
        self.assertTrue(writer_calls)
        # nothing was attached and the narration never went as a file
        self.assertTrue(all(not c["files"] for c in t.calls))
        self.assertFalse(any("narration.txt" in c["prompt"]
                             for c in writer_calls))
        # after the first part the writer chat is continued, not restarted
        self.assertTrue(all(not c["new_chat"] for c in writer_calls[1:]))
        self.assertTrue(any("drafted" in m for m in self.log))

    def test_unusable_answers_fall_back_to_the_whole_json_flow(self):
        class Bad(self.Writer):
            def ask(self, site, prompt, files=(), new_chat=True, ready=None):
                if site == "zai" and "drafted" not in prompt and \
                        "SHOTS 1-" in prompt or (site == "zai" and
                                                 "Not accepted" in prompt):
                    self.calls.append({"site": site, "prompt": prompt,
                                       "new_chat": new_chat,
                                       "files": list(files)})
                    return "sorry, I cannot do that"
                return super().ask(site, prompt, files, new_chat, ready)

        t = Bad()
        try:
            ws.run_shotlist(self.cfg, self.pid, t, "zai", "deepseek",
                            rounds=1, log=self.log.append, use_skeleton=True)
        except Exception:
            pass
        self.assertTrue(any("not used" in m for m in self.log))
        self.assertTrue(any("whole shotlist" in m for m in self.log))


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
