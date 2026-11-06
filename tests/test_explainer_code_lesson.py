"""code_lesson: typed code in small type, chips, an output panel and a diagram that comes first / sits beside."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import explainer_scenes as es, studio  # noqa: E402
from whisperradar.video.explainer import recipes, remotion_backend as rb  # noqa: E402

# six cues, 5 s apart, 30 s in all
SRT = "".join(f"{i}\n00:00:{(i - 1) * 5:02d},000 --> 00:00:{i * 5 - 1:02d},500\nWords number {i} are said here.\n\n"
              for i in range(1, 7))

CODE = ["from sklearn.feature_extraction.text import TfidfVectorizer", "",
        "docs = [\"Office equipment policy\", \"Office travel policy\"]",
        "analyzer = TfidfVectorizer()", "doc_vectors = analyzer.fit_transform(docs)"]


def cues():
    return es._cue_objs(studio.parse_srt_cues(SRT))


def scene(**kw):
    s = {"id": "tfidf", "start": "words number 1", "layout": "code_lesson", "title": "Keyword search",
         "filename": "main.py", "motion": {"at": "words number 2"}, "lines": list(CODE)}
    s.update(kw)
    return s


def build(**kw):
    return recipes.build({"scenes": [scene(**kw)]}, cues())["scenes"][0]["remotion"]


def params(**kw):
    return build(**kw)["params"]


def block(**kw):
    p = {"filename": "a.py", "lines": ["x = 1", "y = 2"], "start_ms": 0, "type_ms": 1000}
    p.update(kw)
    return {"component": "code_lesson", "params": p}


class CodeLessonCompileTests(unittest.TestCase):
    def test_plain_code_scene(self):
        b = build()
        self.assertEqual(b["component"], "code_lesson")
        p = b["params"]
        self.assertEqual(p["start_ms"], 5000)                 # "words number 2" is said at 5 s
        self.assertGreaterEqual(p["type_ms"], 2500)
        self.assertNotIn("diagram", p)

    def test_highlight_chip_and_output_follow_the_words(self):
        p = params(highlights=[{"line": 4, "at": "words number 4", "note": "weights"}],
                   chips=[{"label": "TF-IDF", "color": "good", "at": "words number 2"}, {"label": "BM25"}],
                   output={"label": "Output", "lines": ["0.00  a", "0.65  b"], "at": "words number 5"})
        self.assertEqual(p["highlights"][0]["start_ms"], 15000 - p["start_ms"])   # relative to the typing start
        self.assertEqual(p["chips"][0]["start_ms"], 5000)                          # chips and output: scene time
        self.assertEqual(p["chips"][1]["start_ms"], 550)
        self.assertEqual(p["output"]["start_ms"], 20000)

    def test_output_defaults_to_after_the_typing(self):
        p = params(output={"lines": ["ok"]})
        self.assertEqual(p["output"]["label"], "Output")
        self.assertGreaterEqual(p["output"]["start_ms"], p["start_ms"] + p["type_ms"])

    def test_diagram_first_pushes_the_code_back(self):
        p = params(motion={"at": "words number 1"}, diagram="flow",
                   nodes=[{"label": "Question"}, {"label": "Search"}, {"label": "LLM"}])
        self.assertEqual(p["diagram"]["place"], "before_then_beside")
        self.assertEqual(p["diagram"]["component"], "flow")
        self.assertEqual(len(p["diagram"]["params"]["nodes"]), 3)
        self.assertGreaterEqual(p["start_ms"], recipes.CODE_LESSON_DIAGRAM_MIN_MS)

    def test_diagram_steps_anchor_to_words(self):
        p = params(diagram="flow", nodes=[{"label": "A", "at": "words number 1"}, {"label": "B", "at": "words number 2"}])
        self.assertEqual(p["diagram"]["params"]["nodes"][1]["at_ms"], 5000)

    def test_beside_diagram_does_not_delay_the_code(self):
        p = params(motion={"at": "words number 1"}, diagram="flow", diagram_place="beside",
                   nodes=[{"label": "A"}, {"label": "B"}])
        self.assertLess(p["start_ms"], recipes.CODE_LESSON_DIAGRAM_MIN_MS)

    def test_a_diagram_that_vanishes_cannot_still_be_animating(self):
        with self.assertRaises(Exception):
            build(diagram="flow", diagram_place="before", motion={"at": "words number 1"},
                  nodes=[{"label": "A"}, {"label": "B", "at": "words number 2"}])

    def test_unknown_diagram_is_rejected(self):
        with self.assertRaises(Exception):
            build(diagram="pipeline", nodes=[{"label": "A"}, {"label": "B"}])

    def test_code_that_starts_too_late_is_rejected(self):
        with self.assertRaises(Exception):
            build(motion={"at": "words number 6"})


class CodeLessonCheckTests(unittest.TestCase):
    def test_good_block_passes(self):
        self.assertEqual(rb.check_block(block(chips=[{"label": "TF-IDF", "start_ms": 0}],
                                              output={"label": "Output", "lines": ["ok"], "start_ms": 1200}), "S"), [])

    def test_line_length_depends_on_the_diagram(self):
        long = "x" * 70
        self.assertEqual(rb.check_block(block(lines=["a", long]), "S"), [])
        diagram = {"place": "beside", "component": "flow",
                   "params": {"nodes": [{"label": "A"}, {"label": "B"}]}}
        self.assertTrue(rb.check_block(block(lines=["a", long], diagram=diagram), "S"))

    def test_too_tall_is_rejected(self):
        lines = [f"x{i} = {i}" for i in range(15)]
        out = {"label": "Output", "lines": ["a", "b", "c"], "start_ms": 1000}
        self.assertTrue(any("too tall" in e for e in rb.check_block(block(lines=lines, output=out), "S")))
        self.assertEqual(rb.check_block(block(lines=lines), "S"), [])

    def test_bad_values_are_rejected(self):
        for bad in (block(lines=["only one"]), block(lines=[f"l{i}" for i in range(20)]), block(filename=""),
                    block(type_ms=100), block(chips=[]), block(chips=[{"label": "x" * 20, "start_ms": 0}]),
                    block(output={"label": "Output", "lines": [], "start_ms": 0}),
                    block(highlights=[{"line": 9, "start_ms": 0}]),
                    block(diagram={"place": "sideways", "component": "flow", "params": {}})):
            self.assertTrue(rb.check_block(bad, "S"), bad)


def llm(**kw):
    """Compile one LLM-picture scene; return its remotion params."""
    s = {"id": "pic", "start": "words number 1", "motion": {"at": "words number 1"}}
    s.update(kw)
    return recipes.build({"scenes": [s]}, cues())["scenes"][0]["remotion"]["params"]


class LlmPictureTests(unittest.TestCase):
    def test_tokens_split_on_the_words(self):
        p = llm(layout="tokens", text="RAG lets a model look things up",
                tokens=[{"text": "RAG", "id": 49, "at": "words number 1"}, {"text": " lets", "id": 8128, "at": "words number 3"}])
        self.assertEqual([t["at_ms"] for t in p["tokens"]], [0, 10000])
        self.assertTrue(p["illustrative"])                       # schematic unless the scene says otherwise
        self.assertEqual(rb.check_block({"component": "tokens", "params": p}, "S"), [])

    def test_embeddings_query_and_nearest(self):
        dots = [{"label": f"d{i}", "x": 0.1 * i, "y": 0.5} for i in range(1, 6)]
        p = llm(layout="embeddings", dots=dots, query={"label": "q", "x": 0.3, "y": 0.6, "at": "words number 2"},
                nearest={"count": 2, "at": "words number 3"})
        self.assertEqual(p["query"]["at_ms"], 5000)
        self.assertEqual(p["nearest"], {"count": 2, "at_ms": 10000})
        self.assertEqual(rb.check_block({"component": "embeddings", "params": p}, "S"), [])

    def test_attention_is_one_based_in_the_plan(self):
        p = llm(layout="attention", words=["The", "animal", "was", "tired", "because", "it"], focus=6,
                links=[{"to": 2, "weight": 0.6, "at": "words number 2"}])
        self.assertEqual(p["focus"], 5)
        self.assertEqual(p["links"][0]["to"], 1)
        self.assertEqual(rb.check_block({"component": "attention", "params": p}, "S"), [])

    def test_next_token_pick(self):
        p = llm(layout="next_token", lead="The capital of France is",
                options=[{"word": "Paris", "p": 0.86, "at": "words number 1"}, {"word": "Lyon", "p": 0.06}],
                pick={"option": 1, "at": "words number 3"})
        self.assertEqual(p["pick"], {"option": 1, "at_ms": 10000})
        self.assertEqual(rb.check_block({"component": "next_token", "params": p}, "S"), [])

    def test_bad_pictures_are_rejected(self):
        bad = [
            {"component": "tokens", "params": {"text": "x", "tokens": [{"text": "a", "id": 1, "at_ms": 0}]}},
            {"component": "embeddings", "params": {"dots": [{"label": "a", "x": 2, "y": 0, "at_ms": 0}] * 4}},
            {"component": "attention", "params": {"words": list("abcd"), "focus": 9, "links": [{"to": 1, "weight": 0.5, "at_ms": 0}]}},
            {"component": "attention", "params": {"words": list("abcd"), "focus": 1, "links": [{"to": 1, "weight": 0.5, "at_ms": 0}]}},
            {"component": "next_token", "params": {"lead": "x", "options": [
                {"word": "a", "p": 0.7, "at_ms": 0}, {"word": "b", "p": 0.6, "at_ms": 0}]}},
        ]
        for b in bad:
            self.assertTrue(rb.check_block(b, "S"), b)

    def test_a_picture_can_be_the_diagram_of_a_code_scene(self):
        p = params(motion={"at": "words number 1"}, diagram="next_token", lead="The capital of France is",
                   options=[{"word": "Paris", "p": 0.86, "at": "words number 1"}, {"word": "Lyon", "p": 0.06}])
        self.assertEqual(p["diagram"]["component"], "next_token")
        self.assertGreaterEqual(p["start_ms"], recipes.CODE_LESSON_DIAGRAM_MIN_MS)


if __name__ == "__main__":
    unittest.main()
