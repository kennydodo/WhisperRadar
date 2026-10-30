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
