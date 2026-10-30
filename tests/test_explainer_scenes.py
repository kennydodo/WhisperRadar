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
