"""Key terms are shown where spoken; hero/timeline text never overlaps the footer; underline and
highlight match the written text."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import explainer_scenes as es, explainer_terms as et, studio  # noqa: E402
from whisperradar.video.explainer import layout, recipes, schema as sc, themes, validate  # noqa: E402

SRT = """1
00:00:00,000 --> 00:00:20,000
Today we look at NCM, a new way of generating text.

2
00:00:20,000 --> 00:00:44,000
NCM stands for Next Concept Modeling. It builds a broad plan first.

3
00:00:44,000 --> 00:01:04,000
The paper reports results on a dataset known as LM -1B. NCM is not an LLM.

4
00:01:04,000 --> 00:01:28,000
So check the claims before you trust the headline about NCM.
"""


def cues():
    return studio.parse_srt_cues(SRT)


def plan(first_title="Meet it", footer=None):
    s1 = {"id": "intro", "start": "today we look at", "layout": "hero", "title": first_title,
          "items": [{"label": "A new way to write text", "icon": "pen-line", "at": "today we look at"}]}
    if footer:
        s1["footer"] = footer
    return {"theme": "chalkboard", "scenes": [
        s1,
        {"id": "plain", "start": "NCM stands for", "layout": "row", "title": "The idea",
         "items": [{"label": "Broad plan", "icon": "map", "at": "builds a broad plan"},
                   {"label": "Then detail", "icon": "layers", "at": "builds a broad plan"}]},
        {"id": "wrap", "start": "the paper reports", "layout": "hero", "title": "Check it",
         "items": [{"label": "Read the claims", "icon": "search", "at": "the paper reports"}]}]}


class KeyTermTests(unittest.TestCase):
    def test_terms_are_found_with_what_they_stand_for(self):
        terms = {t["term"]: t for t in et.key_terms(cues(), "NCM vs LLMs: what changes")}
        self.assertEqual(terms["NCM"]["first"], 1)
        self.assertEqual(terms["NCM"]["expansion"], "Next Concept Modeling")
        self.assertEqual(terms["NCM"]["expansion_cue"], 2)
        self.assertIn("LLM", terms)                       # in the title
        self.assertNotIn("LM", terms)                     # "LM -1B" is a dataset name, not a term
        self.assertNotIn("AI", et.STOP - {"AI"} | {"x"})  # sanity: stoplist is a set

    def test_everyday_acronyms_are_left_out(self):
        c = studio.parse_srt_cues("1\n00:00:00,000 --> 00:00:12,000\nAI and AI and AI again, in the US.\n")
        self.assertEqual(et.key_terms(c, ""), [])

    def test_a_term_that_is_said_but_not_shown_is_a_fault(self):
        terms = et.key_terms(cues(), "NCM vs LLMs")
        faults = es.check(plan(), cues(), 88.0, {}, terms)
        self.assertTrue(any("'NCM'" in f and "never shown" in f for f in faults), faults)
        self.assertTrue(any("Next Concept Modeling" in f for f in faults), faults)

    def test_a_term_on_screen_where_it_is_said_passes(self):
        terms = et.key_terms(cues(), "NCM vs LLMs")
        p = plan(first_title="NCM, in one line")
        p["scenes"][1]["title"] = "NCM = Next Concept Modeling"
        self.assertEqual([f for f in es.check(p, cues(), 88.0, {}, terms) if "never shown" in f and "LLM" not in f], [])

    def test_missing_terms_are_written_in_by_the_code(self):
        terms = et.key_terms(cues(), "NCM")
        p, notes = es._fix_terms(plan(), cues(), terms)
        self.assertTrue(notes)
        self.assertEqual([f for f in es.check(p, cues(), 88.0, {}, terms) if "never shown" in f], [])
        intro = p["scenes"][0]
        self.assertIn("NCM", (intro.get("footer") or {}).get("text", "") + intro.get("title", ""))

    def test_the_judge_still_runs_when_a_term_needed_adding(self):
        import tempfile
        from whisperradar import db
        from whisperradar.config import load_config
        cfg = load_config(ROOT / "config.yaml")
        tmp = Path(tempfile.mkdtemp())
        cfg.db_path = tmp / "wr.db"
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        pid = db.create_production(conn, "NCM vs LLMs", "AI", None, str(tmp / "p"))
        conn.close()
        pdir = studio.prod_dir(cfg, pid)
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "subtitles.srt").write_text(SRT, encoding="utf-8")
        seen = []

        def ask(role, text, new_chat):
            seen.append(role)
            if role == "writer":
                return "```json\n" + json.dumps(plan()) + "\n```"
            return json.dumps({"score": 9, "pass": True, "faults": [], "fixes": []})
        out = es.run_scenes(cfg, pid, ask, log=lambda m: None, rounds=3)
        self.assertIn("judge", seen)
        shown = " ".join(et.scene_text(s) for s in out["scenes"])
        self.assertIn("NCM", shown)
        self.assertIn("KEY TERMS", es.writer_prompt("n", 60, "", es._terms_hint(et.key_terms(cues(), "NCM"))))


class LayoutTests(unittest.TestCase):
    def build(self, p, c):
        return recipes.build(p, es._cue_objs(c))

    def boxes(self, doc, sid):
        import tempfile
        f = Path(tempfile.mkdtemp()) / "explainer.json"
        f.write_text(json.dumps(doc), encoding="utf-8")
        d = validate.load(str(f), project_folder=str(f.parent))
        scene = [x for x in d.scenes if x.id == sid][0]
        laid = layout.layout_scene(scene, d.theme, (1920, 1080), project_folder="")
        return {lo.spec.id: tuple(lo.box_px) for lo in laid.objects}

    def test_hero_label_clears_the_footer(self):
        p = plan(footer={"text": "Words alone are not enough", "at": "today we look at"})
        p["scenes"][0]["items"][0]["label"] = "What changed, and what stayed the same?"
        b = self.boxes(self.build(p, cues()), "intro")
        label, foot = b["intro_lb0"], b["intro_ft"]
        self.assertLessEqual(label[1] + label[3], foot[1] + 1)

    def test_timeline_labels_clear_the_footer(self):
        p = {"theme": "chalkboard", "scenes": [{"id": "tl", "start": "today we look at", "layout": "timeline", "title": "Steps",
             "footer": {"text": "Order matters", "at": "so check the claims"},
             "items": [{"label": "Broad plan first", "icon": "map", "at": "today we look at"},
                       {"label": "Then detail", "icon": "layers", "at": "NCM stands for"},
                       {"label": "Decode to text", "icon": "file-text", "at": "the paper reports"}]}]}
        b = self.boxes(self.build(p, cues()), "tl")
        foot = b["tl_ft"]
        for k, box in b.items():
            if k.startswith("tl_lb"):
                self.assertLessEqual(box[1] + box[3], foot[1] + 1, k)

    def test_underline_and_highlight_match_the_written_text(self):
        p = plan(first_title="NCM", footer={"text": "Short", "at": "today we look at"})
        doc = validate.load_dict(self.build(p, cues())) if hasattr(validate, "load_dict") else None
        if doc is None:
            import tempfile
            f = Path(tempfile.mkdtemp()) / "explainer.json"
            f.write_text(json.dumps(self.build(p, cues())), encoding="utf-8")
            doc = validate.load(str(f), project_folder=str(f.parent))
        scene = [s for s in doc.scenes if s.id == "intro"][0]
        laid = layout.layout_scene(scene, doc.theme, (1920, 1080), project_folder="")
        by = {lo.spec.id: lo for lo in laid.objects}
        self.assertLess(by["intro_tline"].box_px[2], by["intro_title"].box_px[2])   # the underline follows the text, not the title box
        self.assertGreater(by["intro_tline"].box_px[2], 40)
        self.assertLess(by["intro_fhl"].box_px[2], int((len("Short") * 0.028 + 0.06) * 1920))


if __name__ == "__main__":
    unittest.main()
