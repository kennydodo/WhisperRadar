"""The shots-stage fault loop: the checker's faults go to the writer, which
returns only the entries it changed (a JSON delta); the checker looks again.
Same loop as an external LLM chat, instead of a full re-plan.

Run: python -m unittest tests.test_shotlist_fix_loop
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import autorun, db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402


def _plan(n_cues=6):
    return {"style": "s",
            "images": [{"file": "S01_01_SCN_ZI.png", "prompt": "a long idea"},
                       {"file": "S01_02_SCN_ZO.png", "prompt": "next"}],
            "shots": [{"cues": "1-4", "asset": "S01_01_SCN_ZI.png",
                       "scene": "S01", "motion": "ZI"},
                      {"cues": "5-6", "asset": "S01_02_SCN_ZO.png",
                       "scene": "S01", "motion": "ZO"}]}


CUE_TEXT = {i: f"cue {i} text" for i in range(1, 7)}


class ScopeAndPromptTests(unittest.TestCase):
    faults = ["shot S01_01_SCN_ZI.png holds 20.0s, over the 12s maximum"]

    def test_scope_picks_named_shots_and_the_cues_a_fault_mentions(self):
        picked, show = studio.shotlist_fix_scope(
            _plan(), ["1 narration cue(s) have no shot: 6"], [], 6)
        self.assertEqual([x["asset"] for x in picked], ["S01_02_SCN_ZO.png"])
        self.assertIn(6, show)
        picked, _ = studio.shotlist_fix_scope(_plan(), self.faults, [], 6)
        self.assertEqual([x["asset"] for x in picked], ["S01_01_SCN_ZI.png"])

    def test_scope_includes_weak_assets(self):
        picked, _ = studio.shotlist_fix_scope(
            _plan(), [], [{"asset": "S01_02_SCN_ZO.png", "missing": ["x"]}], 6)
        self.assertEqual([x["asset"] for x in picked], ["S01_02_SCN_ZO.png"])

    def test_prompt_carries_faults_plan_overview_entries_and_reply_format(self):
        text = studio.shotlist_fix_prompt(
            _plan(), self.faults, [], CUE_TEXT, 6, 12.0,
            style_guide="flat colours", bible="MAYA is tall")
        self.assertIn("Fix ONLY those problems", text)
        self.assertIn(self.faults[0], text)
        self.assertIn("1-4 | S01_01_SCN_ZI.png | ZI | S01", text)   # overview
        self.assertIn("a long idea", text)                           # full entry
        self.assertNotIn('"prompt": "next"', text)                   # not concerned
        self.assertIn("1: cue 1 text", text)
        self.assertIn("flat colours", text)
        self.assertIn("MAYA is tall", text)
        self.assertIn("REPLACES every existing shot whose cues overlap", text)
        self.assertIn("no shot may hold longer than 12s", text)

    def test_references_off_is_stated(self):
        text = studio.shotlist_fix_prompt(
            _plan(), self.faults, [], CUE_TEXT, 6, 12.0, allow_refs=False)
        self.assertIn("References are DISABLED", text)


class ParseTests(unittest.TestCase):
    def test_fenced_and_plain_replies_parse(self):
        body = '{"shots": [{"cues": "1-2", "asset": "a.png"}], "images": []}'
        for text in (body, "```json\n" + body + "\n```"):
            fix = studio.parse_shotlist_fix(text)
            self.assertEqual(fix["shots"][0]["asset"], "a.png")

    def test_a_cut_off_reply_is_reported_as_incomplete(self):
        with self.assertRaises(RuntimeError) as ctx:
            studio.parse_shotlist_fix('{"shots": [{"cues": "1-2", "asset": "a')
        self.assertIn("incomplete", str(ctx.exception))

    def test_wrong_shape_is_an_error(self):
        with self.assertRaises(RuntimeError):
            studio.parse_shotlist_fix('{"shots": "nope", "images": []}')


class ApplyTests(unittest.TestCase):
    def test_a_long_shot_is_split_in_place_and_the_rest_is_untouched(self):
        fix = {"shots": [{"cues": "1-2", "asset": "S01_01a_SCN_ZI.png",
                          "scene": "S01", "motion": "ZI"},
                         {"cues": "3-4", "asset": "S01_01b_SCN_ZO.png",
                          "scene": "S01", "motion": "ZO"}],
               "images": [{"file": "S01_01a_SCN_ZI.png", "prompt": "first half"},
                          {"file": "S01_01b_SCN_ZO.png", "prompt": "second half"}]}
        new, changed, summary = studio.apply_shotlist_fix(_plan(), fix)
        self.assertEqual([x["cues"] for x in new["shots"]], ["1-2", "3-4", "5-6"])
        self.assertEqual(changed, {"S01_01a_SCN_ZI.png", "S01_01b_SCN_ZO.png"})
        files = [i["file"] for i in new["images"]]
        self.assertNotIn("S01_01_SCN_ZI.png", files)          # unused -> dropped
        self.assertIn("S01_02_SCN_ZO.png", files)             # untouched
        self.assertEqual(studio.shotlist_cue_faults(new["shots"], 6), [])
        self.assertIn("dropped 1 unused", summary)

    def test_a_gap_is_filled(self):
        plan = _plan()
        plan["shots"].pop()                                    # cues 5-6 missing
        fix = {"shots": [{"cues": "5-6", "asset": "S01_02_SCN_ZO.png",
                          "scene": "S01", "motion": "ZO"}],
               "images": [{"file": "S01_02_SCN_ZO.png", "prompt": "next"}]}
        new, _changed, _s = studio.apply_shotlist_fix(plan, fix)
        self.assertEqual(studio.shotlist_cue_faults(new["shots"], 6), [])

    def test_a_prompt_rewrite_replaces_by_file_and_keeps_other_fields(self):
        plan = _plan()
        plan["images"][0]["refs"] = ["CH_MAYA"]
        fix = {"shots": [], "images": [{"file": "S01_01_SCN_ZI.png",
                                        "prompt": "much more detail"}]}
        new, changed, _s = studio.apply_shotlist_fix(plan, fix)
        self.assertEqual(new["images"][0]["prompt"], "much more detail")
        self.assertEqual(new["images"][0]["refs"], ["CH_MAYA"])
        self.assertEqual(changed, {"S01_01_SCN_ZI.png"})
        self.assertEqual(new["shots"], plan["shots"])

    def test_an_image_still_used_elsewhere_is_kept(self):
        plan = _plan()
        plan["shots"].append({"cues": "7", "asset": "S01_01_SCN_ZI.png",
                              "scene": "S01", "motion": "ZI"})
        fix = {"shots": [{"cues": "1-4", "asset": "S01_03_SCN_ZI.png",
                          "scene": "S01", "motion": "ZI"}],
               "images": [{"file": "S01_03_SCN_ZI.png", "prompt": "new"}]}
        new, _c, _s = studio.apply_shotlist_fix(plan, fix)
        self.assertIn("S01_01_SCN_ZI.png", [i["file"] for i in new["images"]])

    def test_an_empty_or_unusable_fix_changes_nothing(self):
        plan = _plan()
        for fix in ({"shots": [], "images": []},
                    {"shots": [{"cues": "x", "asset": "a.png"}],
                     "images": [{"file": "a.png", "prompt": " "}]}):
            new, changed, _s = studio.apply_shotlist_fix(plan, fix)
            self.assertIs(new, plan)
            self.assertEqual(changed, set())

    def test_the_original_plan_is_not_modified(self):
        plan = _plan()
        before = json.dumps(plan, sort_keys=True)
        studio.apply_shotlist_fix(plan, {"shots": [
            {"cues": "1-4", "asset": "z.png"}], "images": [
            {"file": "z.png", "prompt": "p"}]})
        self.assertEqual(json.dumps(plan, sort_keys=True), before)


class AttemptTests(unittest.TestCase):
    def _prior(self):
        return {"data": _plan(), "faults": ["a fault on S01_01_SCN_ZI.png"],
                "weak": [], "sheet": ""}

    def _call(self, reply=None, side_effect=None):
        cues = [{"index": i, "text": t} for i, t in CUE_TEXT.items()]
        with mock.patch.object(studio, "llm_generate", return_value=reply,
                               side_effect=side_effect):
            return autorun._attempt_shotlist_fix(
                None, self._prior(), cues, "p", "style", "bible", 12.0, True,
                24.0, 2)

    def test_a_usable_reply_returns_the_fixed_plan_and_changed_assets(self):
        reply = json.dumps({"shots": [], "images": [
            {"file": "S01_01_SCN_ZI.png", "prompt": "fixed"}]})
        data, changed = self._call(reply)
        self.assertEqual(data["images"][0]["prompt"], "fixed")
        self.assertEqual(changed, {"S01_01_SCN_ZI.png"})

    def test_unusable_replies_and_errors_fall_back_to_a_replan(self):
        self.assertIsNone(self._call("sorry, I cannot"))
        self.assertIsNone(self._call('{"shots": [], "images": []}'))
        self.assertIsNone(self._call(side_effect=RuntimeError("boom")))

    def test_a_cut_off_reply_is_continued(self):
        full = json.dumps({"shots": [], "images": [
            {"file": "S01_01_SCN_ZI.png", "prompt": "fixed"}]})
        cut = full[:30]
        data, _ = self._call(side_effect=[cut, full[30:]])
        self.assertEqual(data["images"][0]["prompt"], "fixed")


class LoopTests(unittest.TestCase):
    """_run_shots: a plan with a fault is FIXED in place, not re-planned."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.pid = db.create_production(conn, "A test production")
        conn.commit()
        conn.close()
        pdir = studio.prod_dir(self.cfg, self.pid)
        self.pdir = pdir
        blocks = "".join(
            f"{i}\n00:00:{(i - 1) * 2:02d},000 --> 00:00:{i * 2:02d},000\n"
            f"cue {i} text\n\n" for i in range(1, 7))
        (pdir / "subtitles.srt").write_text(blocks, encoding="utf-8")

    def _review(self, faults, ratio=1.0):
        return {"faults": faults, "ratio": ratio, "matched": 2, "total": 2,
                "weak": [], "warnings": [], "unreviewed": 0, "error": None,
                "verdicts": {}}

    def test_a_faulty_plan_is_fixed_not_replanned(self):
        plan = _plan()
        fix = {"shots": [], "images": [
            {"file": "S01_01_SCN_ZI.png", "prompt": "fixed prompt"}]}
        replies = [json.dumps(plan), json.dumps(fix)]
        calls = []

        def fake_llm(cfg, prompt, **kw):
            calls.append(prompt)
            return replies[len(calls) - 1]

        first = self._review(["a fault on S01_01_SCN_ZI.png"])
        second = self._review([])
        with mock.patch.object(autorun, "style_bible", return_value=(
                "style", "channel", "a bible", "channel")), \
             mock.patch.object(studio, "seed_production", return_value={}), \
             mock.patch.object(studio, "load_manifest_brief", return_value="BRIEF"), \
             mock.patch.object(db, "stage_provider", return_value="judge"), \
             mock.patch.object(studio, "llm_generate", side_effect=fake_llm), \
             mock.patch.object(studio, "review_shotlist", return_value=first), \
             mock.patch.object(studio, "review_shotlist_patch",
                               return_value=second) as patch_review:
            autorun._run_shots(self.cfg, self.pid, provider="p")

        self.assertEqual(len(calls), 2)                      # plan + ONE fix
        self.assertIn("Fix ONLY those problems", calls[1])
        self.assertIn("a fault on S01_01_SCN_ZI.png", calls[1])
        self.assertEqual(patch_review.call_count, 1)         # re-checked
        self.assertEqual(patch_review.call_args.kwargs["patched_assets"],
                         {"S01_01_SCN_ZI.png"})
        saved = json.loads((self.pdir / "shotlist.json").read_text("utf-8"))
        self.assertEqual(saved["images"][0]["prompt"], "fixed prompt")
        self.assertEqual(len(saved["shots"]), 2)             # rest untouched

    def test_an_unusable_fix_falls_back_to_the_old_replan(self):
        plan = json.dumps(_plan())
        calls = []

        def fake_llm(cfg, prompt, **kw):
            calls.append(prompt)
            return plan if len(calls) != 2 else "no json here"

        reviews = [self._review(["a fault"]), self._review([])]
        with mock.patch.object(autorun, "style_bible", return_value=(
                "style", "channel", "a bible", "channel")), \
             mock.patch.object(studio, "seed_production", return_value={}), \
             mock.patch.object(studio, "load_manifest_brief", return_value="BRIEF"), \
             mock.patch.object(db, "stage_provider", return_value="judge"), \
             mock.patch.object(studio, "llm_generate", side_effect=fake_llm), \
             mock.patch.object(studio, "review_shotlist", side_effect=reviews):
            autorun._run_shots(self.cfg, self.pid, provider="p")

        self.assertEqual(len(calls), 3)          # plan, failed fix, full re-plan
        self.assertIn("INPUT 5 - FIXES REQUIRED", calls[2])


if __name__ == "__main__":
    unittest.main()
