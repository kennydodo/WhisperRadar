"""Build the RAG Crash Course demo: narration.srt + plan.json from the real snippets.

Every `lines` and `output.lines` in the plan comes from running snippets/*.py, so the code on screen
is the code that ran and the output panel shows what it printed. Nothing is typed in by hand.

    python docs/explainer/demo/rag-course-demo/build_plan.py          # write narration.srt + plan.json, then compile-check
    python docs/explainer/demo/rag-course-demo/build_plan.py --no-check

Needs scikit-learn and numpy (the snippets import them).
"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
CUE_S = 6.0          # each narration line is 6 s apart in this demo (no audio: timing fixture only)

NARRATION = [
    # 1-4  how RAG works (flow)
    "Retrieval augmented generation, or RAG, lets a model look things up before it answers.",
    "A question comes in, a search step finds the most relevant chunks of your documents,",
    "and the language model writes its answer using only those chunks.",
    "Let's build each step, starting with how documents are split into chunks.",
    # 5-9  chunking (code)
    "First, we split the text. Here is a short handbook as one string.",
    "We cut it into sentences, then group them into chunks of a chosen size.",
    "The chunk function steps through the sentences, size at a time.",
    "Running it prints one chunk per line, each with its own number.",
    "Small chunks keep the search precise, so each answer comes from the right place.",
    # 10-15  keyword search (code)
    "Now the search. The simplest approach is keyword search with TF-IDF.",
    "We build a vectorizer, and it learns how important each word is.",
    "Then we ask a question: how do I send an item back?",
    "Every chunk gets a score. Look at the output.",
    "All zeros. The handbook says returned and refund, but the question says send back.",
    "Keyword search only matches the same words, not the same meaning.",
    # 16-19  embeddings (picture)
    "Embeddings fix this. An embedding turns a text into a point, so that similar meanings sit close together.",
    "Refund policy and returns end up near each other, far from shipping times.",
    "Now the question lands among them, and its nearest neighbours are the returns chunks.",
    "This is a flat picture; real embeddings have hundreds of dimensions.",
    # 20-25  cosine similarity (code + picture beside)
    "Closeness is measured with cosine similarity.",
    "Here we use tiny two dimensional vectors, made up to show the idea.",
    "The function compares the angle between two vectors.",
    "We score the question against each chunk.",
    "Refund policy scores highest, and the other two score much lower.",
    "A real embedding model does the same, with far more numbers.",
    # 26-30  prompt (code)
    "Finally, we give the best chunk to the model inside a prompt.",
    "The prompt holds the question and the retrieved context.",
    "We tell the model to answer using only that context.",
    "Printing it shows exactly what the model will read.",
    "This is the augmented in retrieval augmented generation.",
    # 31-34  next token (picture)
    "The model then writes its answer one token at a time.",
    "At each step it scores every candidate for the next word.",
    "The likeliest one is picked, and the process repeats.",
    "Because the context is in the prompt, the answer is grounded in your documents.",
]


def run(name: str) -> list:
    """(code lines, output lines) of one snippet: the code as written, the output as printed."""
    path = HERE / "snippets" / name
    code = path.read_text(encoding="utf-8").rstrip("\n").split("\n")
    out = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, timeout=120, check=True)
    return code, out.stdout.rstrip("\n").split("\n")


def srt() -> str:
    def stamp(t):
        return f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{int(t % 60):02d},{int(round((t % 1) * 1000)):03d}"
    return "".join(f"{i}\n{stamp((i - 1) * CUE_S)} --> {stamp(i * CUE_S - 0.5)}\n{text}\n\n"
                   for i, text in enumerate(NARRATION, 1))


def plan() -> dict:
    chunk_c, chunk_o = run("01_chunking.py")
    kw_c, kw_o = run("02_keyword_search.py")
    cos_c, cos_o = run("03_cosine_similarity.py")
    pr_c, pr_o = run("04_prompt.py")
    return {"theme": "chalkboard", "scenes": [
        {"id": "how_rag_works", "start": "Retrieval augmented generation", "layout": "flow", "title": "How RAG works",
         "nodes": [{"label": "Question", "at": "A question comes in"}, {"label": "Search", "sub": "find chunks", "at": "a search step finds"},
                   {"label": "LLM", "sub": "writes the answer", "at": "the language model writes"},
                   {"label": "Answer", "at": "using only those chunks"}],
         "sends": [{"label": "chunks", "from": 2, "to": 3, "at": "the most relevant chunks"}]},
        {"id": "chunking", "start": "First, we split the text", "layout": "code_lesson", "title": "Step 1: split into chunks",
         "filename": "chunking.py", "motion": {"at": "Here is a short handbook"}, "lines": chunk_c,
         "highlights": [{"line": 5, "at": "We cut it into sentences", "note": "one per sentence"},
                        {"line": 7, "at": "The chunk function steps", "note": "size at a time"}],
         "output": {"label": "Output", "lines": chunk_o, "at": "Running it prints"},
         "footer": {"text": "Small chunks keep the search precise", "at": "Small chunks keep the search precise"}},
        {"id": "keyword_search", "start": "Now the search", "layout": "code_lesson", "title": "Step 2: keyword search",
         "filename": "keyword_search.py", "motion": {"at": "Now the search"},
         "chips": [{"label": "Keyword search", "color": "accent", "at": "Now the search"},
                   {"label": "TF-IDF", "color": "good", "at": "keyword search with TF-IDF"}],
         "lines": kw_c,
         "highlights": [{"line": 7, "at": "We build a vectorizer", "note": "learns word weights"},
                        {"line": 10, "at": "how do I send an item back", "note": "the question"}],
         "output": {"label": "Output", "lines": kw_o, "at": "Look at the output"},
         "footer": {"text": "Same words match, same meaning does not", "at": "Keyword search only matches"}},
        {"id": "embeddings", "start": "Embeddings fix this", "layout": "embeddings", "title": "Step 3: embeddings",
         "sub": "Similar meanings sit close together", "motion": {"at": "An embedding turns a text into a point"},
         "dots": [{"label": "refund policy", "x": 0.27, "y": 0.46, "group": "returns", "at": "Refund policy and returns"},
                  {"label": "returns", "x": 0.2, "y": 0.3, "group": "returns", "at": "end up near each other"},
                  {"label": "exchange rules", "x": 0.13, "y": 0.52, "group": "returns", "at": "near each other"},
                  {"label": "shipping times", "x": 0.74, "y": 0.24, "group": "delivery", "at": "far from shipping times"},
                  {"label": "delivery cost", "x": 0.84, "y": 0.42, "group": "delivery", "at": "shipping times"},
                  {"label": "opening hours", "x": 0.5, "y": 0.84, "group": "info", "at": "similar meanings sit close"}],
         "query": {"label": "How do I send an item back?", "x": 0.34, "y": 0.64, "at": "Now the question lands"},
         "nearest": {"count": 2, "at": "its nearest neighbours"},
         "footer": {"text": "A flat picture of hundreds of dimensions", "at": "This is a flat picture"}},
        {"id": "cosine", "start": "Closeness is measured", "layout": "code_lesson", "title": "Step 4: measure closeness",
         "filename": "cosine.py", "motion": {"at": "Closeness is measured"}, "lines": cos_c,
         "highlights": [{"line": 3, "at": "The function compares the angle", "note": "angle between vectors"},
                        {"line": 14, "at": "We score the question", "note": "question vs each chunk"}],
         "output": {"label": "Output", "lines": cos_o, "at": "Refund policy scores highest"},
         "diagram": "embeddings", "diagram_place": "before_then_beside",
         "dots": [{"label": "refund policy", "x": 0.3, "y": 0.5, "group": "returns", "at": "Closeness is measured"},
                  {"label": "shipping times", "x": 0.78, "y": 0.28, "group": "delivery", "at": "Closeness is measured"},
                  {"label": "opening hours", "x": 0.58, "y": 0.8, "group": "info", "at": "Closeness is measured"},
                  {"label": "returns", "x": 0.17, "y": 0.33, "group": "returns", "at": "Closeness is measured"}],
         "query": {"label": "the question", "x": 0.36, "y": 0.64, "at": "Closeness is measured"},
         "nearest": {"count": 1, "at": "Here we use tiny two dimensional vectors"}},
        {"id": "prompt", "start": "Finally, we give the best chunk", "layout": "code_lesson", "title": "Step 5: build the prompt",
         "filename": "prompt.py", "motion": {"at": "Finally, we give the best chunk"}, "lines": pr_c,
         "highlights": [{"line": 4, "at": "We tell the model to answer", "note": "only this context"}],
         "output": {"label": "Output", "lines": pr_o, "at": "Printing it shows"},
         "footer": {"text": "Retrieved context + question = the prompt", "at": "This is the augmented"}},
        {"id": "next_token", "start": "The model then writes its answer", "layout": "next_token", "title": "Then the model writes",
         "lead": "Answer: Items can be returned within", "motion": {"at": "At each step it scores"},
         "options": [{"word": "30", "p": 0.88, "at": "At each step it scores"}, {"word": "14", "p": 0.06},
                     {"word": "60", "p": 0.03}, {"word": "90", "p": 0.01}],
         "pick": {"option": 1, "at": "The likeliest one is picked"},
         "footer": {"text": "The answer is grounded in the context", "at": "grounded in your documents"}},
    ]}


def main():
    (HERE / "narration.srt").write_text(srt(), encoding="utf-8")
    p = plan()
    (HERE / "plan.json").write_text(json.dumps(p, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote narration.srt ({len(NARRATION)} cues) and plan.json ({len(p['scenes'])} scenes)")
    if "--no-check" in sys.argv:
        return
    sys.path.insert(0, str(ROOT))
    from whisperradar import explainer_scenes as es, studio
    from whisperradar.video.explainer import recipes
    cues = es._cue_objs(studio.parse_srt_cues((HERE / "narration.srt").read_text(encoding="utf-8")))
    doc = recipes.build(p, cues)
    for s in doc["scenes"]:
        b = s["remotion"]
        print(f"  {s['id']:15s} {b['component']:12s} cues {s['cue_range']}")
    print("compile check OK")


if __name__ == "__main__":
    main()
