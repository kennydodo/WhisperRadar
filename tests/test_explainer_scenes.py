"""Scene plan loop: writer drafts, code checks, judge scores."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db, explainer_scenes as es, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402

SRT = """1
00:00:00,000 --> 00:00:04,000
Two computers can run the same model.

2
00:00:04,000 --> 00:00:09,000
One answers quickly, the other struggles.

3
00:00:09,000 --> 00:00:14,000
The reason is whether the model fits in memory.

4
00:00:14,000 --> 00:00:20,000
So check the file size before you buy anything at all.
"""


def plan(start2="the reason is whether"):
    return {"theme": "chalkboard", "scenes": [
        {"id": "a", "start": "two computers can run", "layout": "compare", "sides": [
            {"header": "Fast", "icon": "rocket", "at": "one answers quickly", "points": []},
            {"header": "Slow", "icon": "snail", "at": "the other struggles", "points": []}]},
        {"id": "b", "start": start2, "layout": "row", "title": "Fit",
         "items": [{"label": "Model", "icon": "box", "at": "the reason is whether"},
                   {"label": "Memory", "icon": "memory-stick", "at": "fits in memory"}]}]}


class ScenePlanTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        tmp = Path(tempfile.mkdtemp())
        self.cfg.db_path = tmp / "wr.db"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.pid = db.create_production(conn, "T", "general", None, str(tmp / "p"))
        conn.close()
        self.pdir = studio.prod_dir(self.cfg, self.pid)
        self.pdir.mkdir(parents=True, exist_ok=True)
        (self.pdir / "subtitles.srt").write_text(SRT, encoding="utf-8")
        self.log = []

    def _ask(self, writer, judge):
        calls = {"writer": iter(writer), "judge": iter(judge)}

        def ask(role, text, new_chat):
            self.log.append((role, text))
            return "```json\n" + json.dumps(next(calls[role])) + "\n```" if role == "writer" \
                else json.dumps(next(calls[role]))
        return ask

    def test_checker_fault_is_sent_back_then_judge_passes(self):
        bad = plan("words that are never said")
        ask = self._ask([bad, plan()], [{"score": 9, "pass": True, "faults": [], "fixes": []}])
        out = es.run_scenes(self.cfg, self.pid, ask, log=lambda m: None, rounds=4)
        self.assertEqual(out["scenes"][1]["start"], "the reason is whether")
        fix = [t for r, t in self.log if r == "writer"][1]
        self.assertIn("were not found in the subtitles", fix)
        self.assertTrue((self.pdir / "plan.json").is_file())
        review = json.loads((self.pdir / "plan_review.json").read_text())
        self.assertEqual(review["score"], 9)

    def test_judge_feedback_goes_to_the_writer(self):
        ask = self._ask([plan(), plan()],
                        [{"score": 5, "pass": False, "faults": ["scene b: too thin"], "fixes": ["add a point"]},
                         {"score": 8.5, "pass": True, "faults": [], "fixes": []}])
        es.run_scenes(self.cfg, self.pid, ask, log=lambda m: None, rounds=4)
        writer_msgs = [t for r, t in self.log if r == "writer"]
        self.assertIn("scene b: too thin", writer_msgs[1])
        self.assertEqual(len([1 for r, _ in self.log if r == "judge"]), 2)

    def test_the_judge_runs_even_when_the_writer_gives_a_long_label_and_a_crowded_scene(self):
        crowded = plan()
        crowded["scenes"][1]["items"][1]["label"] = "Memory " + "x" * 30         # a note, not a block
        crowded["scenes"][1]["footer"] = "A footer given as plain text"            # tolerated
        crowded["scenes"][0]["sides"][0]["points"] = [{"text": "a"}, {"text": "b"}]
        ask = self._ask([crowded], [{"score": 9, "pass": True, "faults": [], "fixes": []}])
        logs = []
        out = es.run_scenes(self.cfg, self.pid, ask, log=logs.append, rounds=3)
        self.assertEqual([r for r, _ in self.log], ["writer", "judge"])            # the judge was called
        self.assertTrue(out["scenes"])

    def test_writer_prompt_has_rules_and_narration(self):
        p = es.writer_prompt("[0:00] hello there", 600, "keep it light")
        for word in ("compare", "hero", "copied word for word", "[0:00] hello there", "keep it light"):
            self.assertIn(word, p)


if __name__ == "__main__":
    unittest.main()


class NumberSceneTests(unittest.TestCase):
    CUES = studio.parse_srt_cues(SRT)

    def _plan(self, mem):
        return {"theme": "chalkboard", "scenes": [
            {"id": "m", "start": "two computers can run", "layout": "memory", "capacity": 24, "unit": "GB",
             "items": mem},
            {"id": "n", "start": "the reason is whether", "layout": "counter",
             "counter": {"from": 0, "to": 14, "unit": "GB", "label": "a 7B model", "at": "fits in memory"}}]}

    def test_writer_prompt_teaches_number_scenes(self):
        p = es.writer_prompt("narration", 120)
        for word in ("memory:", "precision:", "offload:", "fit_chart", "counter", "illustrative"):
            self.assertIn(word, p)
        self.assertIn("arithmetic", es.judge_prompt("n", {"scenes": []}, "s"))

    def test_checker_passes_good_numbers_and_rejects_overflow(self):
        good = self._plan([{"label": "Weights", "gb": 14, "at": "one answers quickly"},
                           {"label": "Cache", "gb": 6, "at": "the other struggles"}])
        self.assertEqual(es.check(good, self.CUES, 20), [])
        bad = self._plan([{"label": "Weights", "gb": 20, "at": "one answers quickly"},
                          {"label": "Cache", "gb": 8, "at": "the other struggles"}])
        self.assertTrue(any("add up to 28" in f for f in es.check(bad, self.CUES, 20)))

    def test_judge_summary_shows_the_numbers(self):
        good = self._plan([{"label": "Weights", "gb": 14, "at": "one answers quickly"}])
        text = es.summary_text(good, self.CUES)
        self.assertIn("capacity=24GB", text)
        self.assertIn("gb=14", text)
        self.assertIn("counter 0 -> 14 GB", text)


class ChannelPlanningSettingsTests(unittest.TestCase):
    def cues(self):
        return es.studio.parse_srt_cues(SRT)

    def test_scene_density_moves_the_target_range(self):
        few, normal, many = (es.counts(300, d) for d in ("few", "normal", "many"))
        self.assertLess(few[1], normal[1])
        self.assertLess(normal[1], many[1])
        self.assertEqual(es.counts(300), normal)

    def test_diagram_style_adds_a_hint_to_the_writer(self):
        self.assertIn("minimal", es.look_hint({"diagram_style": "minimal"}))
        self.assertEqual(es.look_hint({}), "")
        self.assertIn("DIAGRAM STYLE", es.writer_prompt("n", 60, "", es.look_hint({"diagram_style": "icons"})))

    def test_pen_speed_changes_how_long_the_pen_takes(self):
        from whisperradar.video.explainer import recipes
        cues = es._cue_objs(self.cues())
        def span(look):
            doc = recipes.build(plan(), cues, look)
            ev = [e for sc_ in doc["scenes"] for e in sc_["events"] if "duration_ms" in e or "dur" in e]
            return json.dumps(doc["scenes"])
        self.assertNotEqual(span({"pen_speed": "slow"}), span({"pen_speed": "fast"}))


class JudgeIsReachedTests(unittest.TestCase):
    """An over-full scene or a slightly long label must not keep the judge from ever running."""

    def cues(self):
        return es.studio.parse_srt_cues(SRT)

    def crowded(self):
        p = plan()
        p["scenes"][1]["footer"] = "A footer that needs pen time too"
        p["scenes"][1]["items"][1]["label"] = "Memory " + "x" * 30       # long for a row, a note only
        return p

    def test_overfull_scenes_are_simplified_in_code(self):
        from whisperradar.video.explainer import recipes
        p = plan()
        p["scenes"][0]["sides"][0]["points"] = [{"text": "one"}, {"text": "two"}, {"text": "three"}]
        out, notes = recipes.simplify_overfull(p, es._cue_objs(self.cues()))
        recipes.build(out, es._cue_objs(self.cues()))                  # builds now, whatever was removed
        self.assertIsInstance(notes, list)

    def test_a_long_label_is_advice_not_a_block(self):
        faults, advice = es._split_faults(es.check(self.crowded(), self.cues(), 20.0))
        self.assertTrue(any("is longer than" in a for a in advice))
        self.assertFalse(any("is longer than" in f for f in faults))

    def test_stack_rows_may_be_longer(self):
        self.assertGreater(es.LABEL_MAX["stack"], 32)
        self.assertEqual(es.LABEL_MAX.get("row", 32), 32)

    def test_the_overfull_message_says_how_late(self):
        from whisperradar.video.explainer import recipes
        tight = {"theme": "chalkboard", "scenes": [{"id": "a", "start": "two computers can run", "layout": "row", "title": "T",
                 "items": [{"label": f"Item number {i}", "icon": "box", "at": "two computers can run"} for i in range(5)]}]}
        try:
            recipes.build(tight, es._cue_objs(self.cues()))
        except es.ExplainerError as exc:
            self.assertIn("too late", exc.errors[0])
            self.assertNotIn("after the scene", exc.errors[0])


class TooShortSceneTests(unittest.TestCase):
    def test_a_scene_nothing_can_be_taken_off_is_removed_not_failed(self):
        from whisperradar.video.explainer import recipes
        srt = "".join(f"{i}\n00:00:{(i - 1) * 4:02d},000 --> 00:00:{i * 4 - 1:02d},800\nWords number {i} are said here.\n\n"
                      for i in range(1, 7))
        cues = es._cue_objs(studio.parse_srt_cues(srt))
        plan = {"scenes": [
            {"id": "a", "start": "words number 1", "layout": "hero", "title": "A", "items": [{"label": "First point", "at": "words number 1"}]},
            {"id": "b", "start": "words number 2", "layout": "row", "title": "B", "items": [
                {"label": "One", "at": "words number 2"}, {"label": "Two", "at": "words number 2"},
                {"label": "Three", "at": "words number 2"}, {"label": "Four", "at": "words number 2"}]},
            {"id": "c", "start": "words number 3", "layout": "hero", "title": "C", "items": [{"label": "Third point", "at": "words number 3"}]}]}
        fixed, notes = recipes.simplify_overfull(plan, cues)
        recipes.build(fixed, cues)                      # builds: nothing is left over-full
        self.assertTrue(len(fixed["scenes"]) <= 3)


class JudgeSeesMotionScenesTests(unittest.TestCase):
    def test_summary_lists_the_compiled_animation_and_complete_narration(self):
        cues = studio.parse_srt_cues("1\n00:00:00,000 --> 00:00:30,000\n" + "Explain this clearly. " * 30 + "Keep the last important condition.\n\n")
        p = {"scenes": [{"id": "g", "cue_range": [1, 1], "layout": "generation", "title": "Example",
                        "rows": [{"label": "Left to right", "mode": "sequential", "words": ["Hi", "team", "meeting", "Thursday"]}],
                        "footer": {"text": "Illustrative only", "cue": 1}}]}
        text = es.summary_text(p, cues)
        for needle in ("COMPILED animation", "start_ms", "run_ms", "Illustrative only", "Keep the last important condition."):
            self.assertIn(needle, text)


class LastResortTests(unittest.TestCase):
    def test_a_scene_the_checker_keeps_rejecting_is_dropped_not_the_whole_run(self):
        plan = {"scenes": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
        out, dropped = es.drop_faulty_scenes(plan, ["Scene b: the footer starts too late"])
        self.assertEqual(dropped, ["b"])
        self.assertEqual([s["id"] for s in out["scenes"]], ["a", "c"])
        self.assertEqual(es.drop_faulty_scenes(plan, ["Scene a: x"])[1], [])               # never the first scene
        self.assertEqual(es.drop_faulty_scenes(plan, ["the reply has no scenes"])[1], [])   # not about one scene

    def test_a_footer_said_too_late_is_moved_earlier_not_rejected(self):
        from whisperradar.video.explainer import recipes
        srt = "".join(f"{i}\n00:00:{(i - 1) * 5:02d},000 --> 00:00:{i * 5 - 1:02d},500\nWords number {i} are said here.\n\n"
                      for i in range(1, 5))
        cs = es._cue_objs(studio.parse_srt_cues(srt))
        plan = {"scenes": [{"id": "g", "cue_range": [1, 4], "layout": "generation", "motion": {"cue": 1},
                            "footer": {"text": "Late words", "cue": 4},
                            "rows": [{"label": "A", "mode": "sequential", "words": list("abcd")}]}]}
        p = recipes.build(plan, cs)["scenes"][0]["remotion"]["params"]
        self.assertLess(p["footer"]["start_ms"], 19000)

    def test_two_needs_stage_is_not_a_stage_count(self):
        from whisperradar.video.explainer import recipes
        cs = studio.parse_srt_cues("1\n00:00:00,000 --> 00:00:08,000\nIt has eight stages. If stage two needs stage one it waits.\n\n")
        res = recipes.resolve_anchors({"scenes": [{"id": "p", "cue_range": [1, 1], "layout": "pipeline",
                                                   "stages": [{"label": str(i), "workers": 1} for i in range(8)]}]}, es._cue_objs(cs))
        self.assertEqual(es.count_faults(res, cs), [])
