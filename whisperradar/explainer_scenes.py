"""Scene plan for an explainer video: a writer chat drafts plan.json, the code
validates it, a judge chat reviews it - the same writer/judge loop as the shot list.

The writer never sees cue numbers or timings: it quotes the first words of the
narration where a scene starts ("start") and where each item appears ("at"). The
app finds those words in the subtitles (video/explainer/recipes.py), so every
scene is timed from the real narration and the plan survives a new voice.
Faults found by the code (words not in the narration, a scene too crowded for its
time, an unknown icon) go back to the writer as a short list; the judge then
grades the visual teaching, not the format.

Prompts are self-contained (the full plan travels with every message), so the same
loop works over the web-chat transport and over a plain API provider.
"""
from __future__ import annotations

import json
import copy
import hashlib
import math
import os
import time
import re
from typing import Callable

from . import studio
from . import explainer_terms
from . import scene_plan_state as sps
from .video.explainer import recipes
from .video.explainer.validate import ExplainerError

MAX_ROUNDS = 5
class NeedsRevision(RuntimeError):
    """The run finished and the draft was saved, but it is not approved. NOT a transport failure."""


class AcceptError(RuntimeError):
    """A manual acceptance that was refused (stale, invalid, repeated, not acknowledged)."""


CONTINUE_MESSAGE = ("Continue the same scene-plan JSON as text, not a file. Output only the remaining "
                    "scenes after the last complete one and close every bracket.")
# longest label each layout can show; stack rows and hero lines are wide, the others share a row
LABEL_MAX = {"stack": 48, "hero": 60, "compare": 40}
MIN_SCORE = 8.0          # fallback only, when no setting is reachable (tests, direct calls)


def pass_mark(cfg, pid: int) -> float:
    """The scene-plan pass mark from the settings (Settings > Explainer scene plan: minimum score,
    or the production's channel; 0 there = the script minimum rating). MIN_SCORE only when the
    settings cannot be read."""
    try:
        from . import db, settings
        conn = db.connect(cfg.db_path)
        try:
            v = float(settings.for_production(conn, db.get_production(conn, pid))["scene_plan_min_rating"])
        finally:
            conn.close()
        return v if 1.0 <= v <= 10.0 else MIN_SCORE
    except Exception:  # noqa: BLE001 - never fail a run over reading the bar
        return MIN_SCORE

ICONS = (
    "box blocks layers puzzle file folder database server hard-drive cpu gpu memory-stick monitor laptop "
    "smartphone cloud globe wifi lock shield key user users message-square mail bell bookmark book-open "
    "image video file-music mic camera calculator ruler scale gauge hourglass clock calendar zap flame battery "
    "sun moon thermometer wrench hammer settings sliders-horizontal search eye lightbulb brain target flag "
    "rocket trophy star heart thumbs-up thumbs-down triangle-alert circle-check circle-x message-circle-question-mark info "
    "wallet credit-card dollar-sign coins piggy-bank trending-up trending-down chart-bar chart-line "
    "chart-pie shopping-cart store house building car plane train-front bike truck map-pin route compass "
    "list-checks clipboard-list pencil trash download upload refresh-cw repeat split merge arrow-right "
    "turtle snail rabbit leaf sprout tree-pine droplet shield-check badge-check scissors package gift "
    "graduation-cap briefcase handshake megaphone newspaper code terminal bug bot sparkles wand"
).split()

# ---- the scene vocabulary, per genre ------------------------------------------------------
# Every genre gets the text layouts and the GENERAL motion layouts (any topic). A genre adds its
# specialised motion layouts. The writer only ever sees the layouts of its genre.
TEXT_SET = ("row", "steps", "timeline", "stack", "hero", "compare")
GENERAL_SET = ("flow", "bars", "line", "split", "grid", "counter")
GENRE_LAYOUTS = {
    "tech": TEXT_SET + GENERAL_SET + ("pipeline", "generation", "levels", "code",
                                      "memory", "precision", "offload", "fit_chart"),
    "finance": TEXT_SET + GENERAL_SET,
}


def genre_of(look) -> str:
    """The channel's scene genre ("" or unknown = tech)."""
    g = str((look or {}).get("scene_genre") or "tech") if isinstance(look, dict) else str(look or "tech")
    return g if g in GENRE_LAYOUTS else "tech"


# One example per layout (shown to the writer only when its genre has that layout).
FORMAT_EXAMPLES = [
    ("flow", """  {"id": "path", "start": "first words of the narration where this scene begins", "layout": "flow", "title": "short",
   "nodes": [{"label": "Browser", "icon": "globe", "at": "words"}, {"label": "Cache", "sub": "fast copy", "icon": "database", "at": "words"},
             {"label": "Server", "icon": "server", "at": "words"}],
   "sends": [{"label": "request", "back": "page", "at": "words"}, {"label": "request", "to": 2, "back": "hit", "at": "words"}],
   "footer": {"text": "one short takeaway", "at": "words"}}"""),
    ("bars", """  {"id": "speed", "start": "...", "layout": "bars", "title": "short", "unit": "s", "ratio_to": 1,
   "items": [{"label": "Method A", "value": 21, "at": "words", "color": "red"}, {"label": "Method B", "value": 1, "at": "words", "color": "green"}],
   "limit": {"label": "budget", "value": 10, "at": "words"}}"""),
    ("line", """  {"id": "growth", "start": "...", "layout": "line", "title": "short", "prefix": "$", "illustrative": true,
   "x": ["25", "35", "45", "55", "65"], "series": [{"label": "Start at 25", "values": [0, 14784, 43865, 101073, 213610]}],
   "marks": [{"x": 5, "text": "$213,610 at 65", "at": "words"}], "motion": {"at": "words"}}"""),
    ("split", """  {"id": "budget", "start": "...", "layout": "split", "title": "short", "prefix": "$", "total": 3000, "rest_label": "Left over",
   "items": [{"label": "Rent", "value": 1200, "at": "words", "color": "red"}, {"label": "Savings", "value": 600, "at": "words", "color": "green"}]}"""),
    ("grid", """  {"id": "board", "start": "...", "layout": "grid", "title": "short", "rows": 9, "cols": 9, "motion": {"at": "words"},
   "steps": [{"label": "One cell", "cells": 1, "at": "words", "color": "yellow", "symbols": ["5"]},
             {"label": "Whole board", "cells": 80, "at": "words", "color": "blue"}]}"""),
    ("counter", """  {"id": "num", "start": "...", "layout": "counter", "title": "optional",
   "counter": {"from": 0, "to": 14, "unit": "GB", "label": "a 7B model at 16-bit", "at": "words"}}"""),
    ("pipeline", """  {"id": "run", "start": "...", "layout": "pipeline", "title": "short", "tasks": 8, "motion": {"at": "words"},
   "stages": [{"label": "Stage 1", "workers": 8, "work": 1}, {"label": "Stage 2", "workers": 4, "work": 1, "waits_for_all": true, "at": "words"}]}"""),
    ("generation", """  {"id": "order", "start": "...", "layout": "generation", "title": "short", "motion": {"at": "words"},
   "rows": [{"label": "Way A", "mode": "sequential", "words": ["a", "b", "c", "d", "e", "f"]},
            {"label": "Way B", "mode": "plan_then_fill", "beats": [{"at": "words"}, {"at": "words"}, {"at": "words"}],
             "words": ["a", "b", "c", "d", "e", "f"]}]}"""),
    ("levels", """  {"id": "tree", "start": "...", "layout": "levels", "title": "short", "motion": {"at": "words"},
   "levels": [{"label": "Broad", "cells": 2, "at": "words"}, {"label": "Finer", "cells": 6, "at": "words"}],
   "decoder": {"label": "Decoder", "at": "words"}, "result": {"text": "the final text", "at": "words"}}"""),
    ("code", """  {"id": "src", "start": "...", "layout": "code", "title": "short", "filename": "train.py", "motion": {"at": "words"},
   "lines": ["def f(x):", "    return x"], "highlights": [{"line": 2, "at": "words", "note": "short"}]}"""),
    ("memory", """  {"id": "mem", "start": "...", "layout": "memory", "title": "optional", "capacity": 24, "unit": "GB",
   "capacity_label": "24 GB graphics card", "exact": true,
   "items": [{"label": "Model weights", "gb": 14, "at": "words"},
             {"label": "Context cache", "gb": 6, "grows_from": 1.5, "at": "words"}]}"""),
    ("precision", """  {"id": "bits", "start": "...", "layout": "precision", "params_b": 7,
   "items": [{"label": "16-bit", "bits": 16, "at": "words"}, {"label": "4-bit", "bits": 4, "at": "words"}]}"""),
    ("offload", """  {"id": "layers", "start": "...", "layout": "offload", "layers": 32,
   "items": [{"label": "GPU VRAM", "layers": 20, "at": "words"}, {"label": "CPU RAM", "layers": 12, "at": "words"}]}"""),
    ("fit_chart", """  {"id": "fit", "start": "...", "layout": "fit_chart", "limit": 24, "unit": "GB", "bits": 4, "fade": {"out_ms": 400},
   "items": [{"label": "7B", "params_b": 7, "at": "words"}, {"label": "70B", "params_b": 70, "at": "words"}]}"""),
    ("stack", """  {"id": "short-name", "start": "...", "layout": "row" | "steps" | "timeline" | "stack" | "hero",
   "title": "optional heading, 2-5 words",
   "items": [{"label": "2-4 words", "icon": "lucide-icon-name", "at": "words of the narration when it appears",
              "color": "optional: white yellow green red orange blue pink purple grey", "note": "optional small line"}],
   "footer": {"text": "one short takeaway", "at": "words of the narration"},
   "connect": "arrow" | "dashed" | "none", "focus": false}"""),
    ("compare", """  {"id": "versus", "start": "...", "layout": "compare",
   "sides": [{"header": "Left", "icon": "rocket", "at": "words", "color": "green",
              "points": [{"text": "short point", "at": "words", "mark": "check"}]},
             {"header": "Right", "icon": "snail", "at": "words", "color": "red",
              "points": [{"text": "short point", "at": "words", "mark": "cross"}]}]}"""),
]


def format_text(genre: str = "tech") -> str:
    allowed = GENRE_LAYOUTS.get(genre, GENRE_LAYOUTS["tech"])
    return ('{"theme": "chalkboard",\n "scenes": [\n'
            + ",\n".join(t for lay, t in FORMAT_EXAMPLES if lay in allowed) + "\n ]}")


FORMAT = format_text("tech")

# Rule lines tagged with the layout they are about; a line about a layout the genre does not have
# is left out. Untagged lines (None) always apply.
RULE_LINES = [
    (None, "PICK THE PICTURE BY THE KIND OF IDEA - the viewer should WATCH the idea happen. Most of the video is motion scenes; text scenes are the short pauses between them:"),
    ("flow", "- something is handed on, asked for or moves (a request, a payment, a file, data, money, a signal, a decision passing through people or systems) -> flow"),
    ("bars", "- numbers set against each other (times, prices, sizes, scores, costs, before/after) -> bars"),
    ("line", "- something changes over time or grows (compound growth, a price, a trend, a curve, adoption, scaling) -> line"),
    ("split", "- parts of one whole (a budget, a portfolio, fees out of a payment, a memory budget, a day) -> split"),
    ("grid", "- a count out of a total (7 in 10, 1 of 81 cells), a board, a matrix, many identical units, coverage -> grid"),
    ("counter", "- one big number the narrator states -> counter"),
    ("pipeline", "- work spread over workers, queues, bottlenecks, one stage waiting for another -> pipeline"),
    ("generation", "- two ways of producing the same text or sequence -> generation"),
    ("levels", "- coarse to fine, a hierarchy, zooming in -> levels"),
    ("code", "- real code the narrator discusses -> code"),
    ("memory", "- what fits in a memory (GPU, RAM) with the narrator's sizes -> memory / precision / offload / fit_chart"),
    (None, "- ONLY when none of these fits: a rule, warning or the one key sentence -> hero; two things compared point by point -> compare; ordered steps -> steps; separate facts -> stack or row. A video made mostly of hero, stack, row and compare scenes is a slide deck and is rejected."),
    (None, "- First ask for every scene: what will the viewer understand by watching this that the words alone do not show? If the answer is only \"they read the same point\", pick a motion scene."),
    (None, "- MOTION MIX (the app measures this and rejects plans that miss it): at least {motion_share} of the narration time must be motion scenes (everything except row, steps, timeline, compare, stack and hero), and never more than {text_run} text-only scenes or {text_seconds} seconds of them in a row. Claims, evaluations, caveats and examples can be drawn too: timings and results as bars, a share as split or grid, a trend as line, who passes what to whom as flow."),
    (None, "TEXT LAYOUTS (the short pauses between motion scenes):"),
    (None, "- row: 2-4 things side by side. Add connect \"arrow\" for a flow, \"none\" for a set. steps: 3-5 numbered steps in order. timeline: 3-5 points in time."),
    (None, "- stack: 3-4 short lines, one per row, each with an icon; a footer fits under it. hero: ONE big icon with one short sentence (the label may be up to ~50 characters)."),
    (None, "- compare: exactly two sides, each with a header, an icon and 1-3 short points (right vs wrong, A vs B, before vs after)."),
    (None, "MOTION LAYOUTS:"),
    ("flow", "- flow: 2-6 boxes in a row (\"nodes\": label up to 18 characters, 13 with 5-6 nodes; optional \"sub\" line and \"icon\"), each appearing on its words. \"sends\" (0-4): on its words a labelled token (what moves: \"request\", \"$100\", \"file\") travels from node \"from\" (default 1) to node \"to\" (default the last), lighting each box it passes; \"back\" (a label) makes it return (\"response\", \"receipt\", \"hit\"). Use it for requests and responses, payments, supply chains, data passing through steps, who asks whom."),
    ("bars", "- bars: 2-6 bars (\"items\": label up to 28 characters, \"value\", optional \"note\", \"icon\", \"color\"), each growing on its words with its value counting up; \"unit\" (s, GB, %, x) and \"prefix\" ($, EUR sign); \"ratio_to\": the number of the bar the others are compared to (shows x21 or 1/21); \"limit\": a dashed line (a budget, a memory size)."),
    ("line", "- line: \"x\" (2-24 labels: years, ages, months, steps) and 1-3 \"series\" with one value per x label; the line draws from left to right from \"motion\".at over the scene; \"marks\" ({\"x\": label number, \"text\", \"at\"}) pop up on their words. The narrator rarely says every value: then set \"illustrative\": true, and keep any value the narrator does say exact. When the narrator only describes a shape (\"quality drops if rounds are too few\"), also set \"shape\": true: the curve is drawn without any numbers and only the two ends of x are named (\"Fewer rounds\" ... \"More rounds\"), so nothing looks measured."),
    ("split", "- split: one whole and its 2-6 parts (\"items\" with \"value\"), each part sweeping into a ring on its words, with values and shares; \"total\" (when the narrator gives the whole) leaves the rest as a gap named by \"rest_label\"; \"center\" is a short label under the total."),
    ("grid", "- grid: a board of \"rows\" x \"cols\" (each 1-12) and 1-4 \"steps\" that fill the next \"cells\" on their words (\"label\", \"cells\", \"color\", optional \"symbols\": 1-3 character texts shown in the filled cells, such as digits); \"show_share\": true adds \"x of total, %\". Use it for 7-in-10 statistics, puzzle boards, matrices, many identical units."),
    ("counter", "- counter: one big number counting from \"from\" to \"to\" with a short label."),
    ("pipeline", "- pipeline: tasks (4-24) flow through 2-8 stages (as many as the narrator names: if the narration says eight stages, draw eight; labels of 14 characters when there are more than three); each stage has \"workers\" (1-8) and a relative \"work\" per task; \"waits_for_all\": true makes a stage wait until the whole stage before it is finished (shown locked). Use it for parallel work, queues, bottlenecks, \"level 2 waits for level 1\"."),
    ("generation", "- generation: 1-2 rows of 4-14 short words, each row \"mode\": \"sequential\" (one after another) or \"plan_then_fill\" (a rough outline first, then the gaps). Use it to show two ways of producing the same thing side by side. Row words must all be real final text, never \"___\" or placeholders. A row that stands in for something that does not work in words (a model's internal codes) gets \"analogy\": true: it is tagged as an analogy and shows no step count."),
    ("levels", "- levels: a broad level first, then finer levels that split it into more cells, then an optional decoder and the resulting text; \"levels\": [{\"label\", \"cells\" (1-16, more each level), \"at\": words}], \"decoder\": {\"label\", \"at\"}, \"result\": {\"text\", \"at\"}. Use it for hierarchies, coarse-to-fine, layers, zooming in. Give each level its own words."),
    ("code", "- code: 2-12 lines (64 characters at most) typed out, with \"highlights\" (line, short note, \"at\") on the line that matters. Use it only for real code or a real excerpt the narrator discusses, never invented."),
    (None, "- ONE DIAGRAM, MANY BEATS: a motion scene may last 20-60 seconds and keep the same picture while the narrator moves through several points. Tie each change to the words that cause it (\"at\" on nodes, sends, bars, parts, steps, marks, stages, levels, highlights; generation rows take \"beats\": 2-3 {\"at\": words}). Anchors for different steps must be words said in different places. Every motion scene may carry a \"footer\" ({\"text\", \"at\"}): the one sentence that should stay on screen for the point. Without an anchor a step is spaced evenly, which does not follow the narrator, so always anchor them."),
    (None, "- \"motion\": {\"at\": words} is where the animation starts. Give each motion scene 8+ seconds. Colours, fonts and line style come from the channel; \"color\" on an item only carries meaning. Avoid the same layout three times in a row; titles under 48 characters."),
    (None, "- REVISION SAFETY: never remove a teaching point to fit a template. Use fewer decorative elements, a different supported layout, or a longer scene boundary. Illustrative diagrams must be labelled schematic; never imply measured speed from decorative motion. Stages, workers and tasks are different quantities."),
    (None, "- COUNTS ARE THE NARRATOR'S: if the narration says eight stages, draw eight; never change a count or a relationship to fit a component. If a component cannot draw it, pick another motion layout that can (a flow of up to 6 boxes, a grid, bars) and, only when none can, a steps or row scene with the exact count; say in the footer what is simplified. The app rejects a drawn count that differs from the stated one."),
    (None, "NUMBERS (the app checks them):"),
    (None, "- bars, split, line, counter and the number scenes show the narrator's numbers exactly (units too); never invent one. A number that is an example rather than a fact (\"imagine it takes 21 seconds\"), or a shape the narrator only describes (\"it keeps rising\"), is drawn with \"illustrative\": true, which puts an \"illustrative\" tag on screen. Do not use a number scene for a number the narrator never says."),
    ("memory", "- memory: a box (GPU memory, RAM, a budget) filled by up to 4 segments, widths proportional to \"gb\". Segments must add up to at most \"capacity\" (else the scene is rejected). Set \"exact\": true when the narrator gave the numbers, leave it off to show an \"illustrative numbers\" tag. \"grows_from\" (last segment only) makes it grow from a smaller size."),
    ("precision", "- precision: the same model at different bit sizes, 1-4 rows. Give \"params_b\" (billions) and each row's \"bits\"; the app computes size = params x bits / 8 (the arithmetic is checked). Never type the sizes yourself."),
    ("offload", "- offload: a model's layers split across two memories (two zones that add up to \"layers\"). Labels must be short (about 10 characters for a small zone). Say it is simplified; real tools place layers differently."),
    ("fit_chart", "- fit_chart: bars of model size (params_b x bits / 8) against a memory \"limit\" (4 models at most; over the limit turns red). \"fade\" ({\"in_ms\", \"out_ms\"}, each up to 2000) is optional."),
    (None, "PACING AND ANCHORS:"),
    (None, "- The FIRST scene must start with the very first words of the narration; later scenes follow in order. Together the scenes cover the whole narration."),
    (None, "- \"start\" and every \"at\" are copied word for word from the narration (3 or more consecutive words, no need for punctuation) and each must be findable only in its own place: never an \"at\" from another scene."),
    (None, "- A scene lasts about 12-40 seconds. Change the picture when the narration changes idea, not on every sentence. Scene count for this video: about {low}-{high}."),
    (None, "- An item appears when the narrator says it: \"at\" is the words where it is said. Never anchor an item in the last 3 seconds of its scene, and give every item at least 2.5 seconds before the scene ends. Fewer items beat crowded scenes."),
    (None, "- Do not anchor two scenes on the same sentence. Items in a scene go in narration order."),
    (None, "- THE SCREEN IS NEVER EMPTY: every scene shows something from its first second - give it a \"title\" (motion scenes too), or anchor its first element to the scene's opening words. A blank board while the narrator talks is rejected."),
    (None, "TEXT:"),
    (None, "- Labels are 2-4 words (at most 32 characters), plain words, no sentences, no emoji. Numbers and units are welcome (7B, 14 GB, $1,200)."),
    (None, "- Use an icon from the list; another valid Lucide icon name is fine if you are sure of it. An icon must add meaning (a thing, a place, an action), not just repeat the label's noun or decorate. Leave \"icon\" off when no icon helps."),
    (None, "- NAMES ARE SHOWN: when the narrator says a name, acronym or key term (for example NCM, LLM, VRAM, ETF, APR), or spells it out (\"NCM stands for Next-Concept Modeling\"), write it on screen in the scene that covers those words - as the title, a label, the hero line or the footer - spelled as the narrator says it, and add what it stands for when the narrator says that. A viewer must never hear a key term that is not shown."),
    (None, "- Use colour to carry meaning (green = good, red = problem, orange = caution, blue = neutral info, yellow = the key thing); leave \"color\" off when unsure, the app picks."),
    (None, "- Never invent facts. Labels restate what the narrator says."),
    (None, "- Use \"connect\": \"arrow\" only when the items really happen in order or feed each other; use \"none\" for a set of separate things."),
    (None, "- Do NOT output cue numbers or timings. Output ONLY the JSON object, in one ```json block, as text in this chat (no file)."),
]


def rules_for(genre: str = "tech") -> str:
    allowed = GENRE_LAYOUTS.get(genre, GENRE_LAYOUTS["tech"])
    return "\n".join(t for lay, t in RULE_LINES if lay is None or lay in allowed)


RULES = rules_for("tech")


def _timed_narration(cues: list[dict]) -> str:
    """'[m:ss] text' lines, so the writer can pace (it never uses the times)."""
    out = []
    for c in cues:
        t = int(studio._srt_seconds(c["start"]))
        out.append(f"[{t // 60}:{t % 60:02d}] {c['text'].strip()}")
    return "\n".join(out)


DENSITY = {"few": (45, 22), "normal": (32, 14), "many": (22, 10)}   # seconds per scene: (low end, high end)
STYLE_HINT = {
    "icons": "DIAGRAM STYLE: picture-heavy - give most points a fitting icon (flow boxes and bars take icons too).",
    "minimal": "DIAGRAM STYLE: minimal - few icons; lean on words, numbers, charts and mechanism scenes; skip decoration.",
}


def counts(total_seconds: float, density: str = "normal") -> tuple[int, int]:
    lo, hi = DENSITY.get(density or "normal", DENSITY["normal"])
    return max(1, int(total_seconds / lo)), max(2, int(total_seconds / hi))


def _terms_hint(terms: list) -> str:
    if not terms:
        return ""
    names = "; ".join(t["term"] + (f" (= {t['expansion']})" if t["expansion"] else "") for t in terms)
    return f"\nKEY TERMS THE NARRATOR SAYS - each must be written on screen in the scene where it is first said: {names}"


def look_hint(look: dict) -> str:
    return STYLE_HINT.get((look or {}).get("diagram_style") or "", "")


def _rules_text(low, high, rule, genre: str = "tech") -> str:
    text = rules_for(genre).replace("{low}", str(low)).replace("{high}", str(high))
    if rule:
        return (text.replace("{motion_share}", f"{float(rule.get('min_share') or 0):.0%}")
                .replace("{text_run}", str(int(rule.get("text_run") or 0)))
                .replace("{text_seconds}", f"{float(rule.get('text_seconds') or 0):.0f}"))
    return "\n".join(x for x in text.split("\n") if not x.startswith("- MOTION MIX"))


GENRE_INTRO = {
    "tech": "The subject is technology: show how systems work - what passes from where to where, what waits for what, what it costs, how big it is.",
    "finance": "The subject is money: show it move and grow - where it flows, how it splits, how it changes over time, how amounts compare. Every amount is the narrator's, or tagged illustrative.",
}


def writer_prompt(narration: str, total_seconds: float, direction: str = "",
                  look: str = "", density: str = "normal", rule: dict | None = None, genre: str = "tech") -> str:
    low, high = counts(total_seconds, density)
    genre = genre if genre in GENRE_LAYOUTS else "tech"
    if rule is None:
        rule = MOTION_RULES.get(genre, {}) if total_seconds >= MOTION_MIN_SECONDS else {}
    return f"""You are the visual planner of a hand-drawn whiteboard/chalkboard explainer video. A narrator reads the script below; you decide WHAT IS DRAWN while each part is spoken. Think like a great teacher at a board who animates the idea instead of writing it down: the viewer watches things move, fill, grow and pass along, drawn as the words are said. {GENRE_INTRO.get(genre, "")}

The video is {total_seconds / 60:.1f} minutes long. Return a scene plan as JSON in exactly this shape:

{format_text(genre)}

ICONS (Lucide names): {" ".join(ICONS)}

{_rules_text(low, high, rule, genre)}
{("CHANNEL LOOK: " + look + chr(10)) if look else ""}{("EXTRA DIRECTION: " + direction.strip() + chr(10)) if direction.strip() else ""}
NARRATION (the times in brackets are only for pacing; do not copy them):
{narration}
"""


def _ids_in(text: str, plan: dict) -> set:
    """The plan's scene ids a fault/note/fix mentions. Ids of 1-2 characters only count when they
    are addressed directly ('scene b: ...' or 'b: ...') so common short words never match."""
    ids = [str(s.get("id", "")) for s in plan.get("scenes", [])]
    found = set()
    for sid in ids:
        if not sid:
            continue
        esc = re.escape(sid)
        if len(sid) <= 2:
            if re.search(r"scene\s+" + esc + r"\b|(?<![\w-])" + esc + r"\s*:", str(text), re.I):
                found.add(sid)
        elif re.search(r"(?<![\w-])" + esc + r"(?![\w-])", str(text), re.I):
            found.add(sid)
    return found


def revision_ids(plan: dict, faults: list[str]) -> set[str]:
    """Only named scenes may change. A fault that names no scene id is guidance, not a licence to
    rewrite every scene; the plan only opens fully when nothing at all is addressable."""
    ids = [str(s.get("id", "")) for s in plan.get("scenes", [])]
    selected = set()
    for fault in faults:
        found = _ids_in(fault, plan)
        selected.update(found)
        if found and re.search(r"boundary|boundaries|outside.*scene|scene.*start|merge|split", fault, re.I):
            for sid in found:
                k = ids.index(sid)
                selected.update(ids[max(0, k - 1):k + 2])
    return selected or set(ids)


def region_ids(plan: dict, allowed: set) -> set:
    """The scenes that may be restructured: the named ones and their immediate neighbours."""
    ids = [str(x.get("id", "")) for x in plan.get("scenes", [])]
    out = set(allowed)
    for k, sid in enumerate(ids):
        if sid in allowed:
            out.update(ids[max(0, k - 1):k + 2])
    return out


def fix_prompt(plan: dict, faults: list[str], narration: str = "") -> str:
    lines = "\n".join("- " + str(f) for f in faults)
    allowed = sorted(revision_ids(plan, faults))
    region = sorted(region_ids(plan, set(allowed)))
    return ("Correct the listed scenes, preserving every other scene exactly. Do not change ids of scenes you keep or "
            "reorder them. You may change layout, labels and anchors inside the allowed scenes.\n"
            "When timing or teaching needs it you may SPLIT a scene into several or MERGE neighbours: "
            "return the new or merged scene objects in \"scenes\" (new unique ids) and list the ids they replace in "
            "\"remove\". Only the allowed scenes and their immediate neighbours (RESTRUCTURE REGION) may be restructured. "
            "The scenes you return for a restructure must together cover every narration cue the replaced scenes covered, "
            "and every footer, label and note that explained something must still appear. Never drop a scene or an "
            "explanation just to get the check to pass; use a supported layout or adjust boundaries instead.\n"
            "Return ONLY {\"scenes\": [complete replacement or new scene objects], \"remove\": [ids, optional]} in one JSON block. "
            "Unchanged scenes may be omitted; they are retained locally. A full plan is accepted only if "
            "all unrelated scenes are unchanged. Use exact narration anchors; never invent timings or evidence.\n"
            "ALLOWED SCENE IDS: " + json.dumps(allowed) + "\nRESTRUCTURE REGION: " + json.dumps(region)
            + "\nPROBLEMS:\n" + lines
            + "\nTIMED NARRATION:\n" + narration + "\nCURRENT PLAN:\n"
            + json.dumps(plan, ensure_ascii=False))


def _ranges(scenes, cues) -> list:
    """Each scene's (first cue, last cue) - resolved from its word anchors when cues are known
    (most scenes are anchored by words, not cue numbers), else the raw cue_range, else None."""
    scenes = [copy.deepcopy(x) for x in scenes]
    if cues:
        try:
            done = recipes.resolve_anchors({"scenes": scenes}, _cue_objs(cues))["scenes"]
            scenes = done if len(done) == len(scenes) else scenes
        except Exception:  # noqa: BLE001 - an unplaceable scene is the checker's finding, not ours
            pass
    out = []
    for sc in scenes:
        r = sc.get("cue_range") if isinstance(sc, dict) else None
        ok = isinstance(r, (list, tuple)) and len(r) == 2 and all(isinstance(x, int) for x in r)
        out.append((r[0], r[1]) if ok else None)
    return out


def _cue_cover(ranges) -> set:
    out = set()
    for r in ranges:
        if r:
            out.update(range(r[0], r[1] + 1))
    return out


def _explanatory_texts(node, out=None) -> list:
    """Footers, labels, notes and texts inside a scene: what explains, not what decorates."""
    out = [] if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("label", "note", "text") and isinstance(v, str) and len(v.strip()) >= 3:
                out.append(v.strip())
            else:
                _explanatory_texts(v, out)
    elif isinstance(node, list):
        for v in node:
            _explanatory_texts(v, out)
    return out


def _start_of(scene) -> int:
    r = scene.get("cue_range") if isinstance(scene, dict) else None
    return r[0] if isinstance(r, (list, tuple)) and r and isinstance(r[0], int) else 10 ** 9


def merge_revision(plan: dict, reply: dict, allowed: set[str], region: set[str] | None = None,
                   cues: list | None = None) -> dict:
    """Accept a scene patch or unchanged full-plan response, never silent collateral edits.
    With a restructure region the writer may also split or merge: new scene ids in "scenes" and
    replaced ids in "remove", limited to the region, keeping every narration cue covered and
    every explanatory text on screen."""
    original = {str(s.get("id")): s for s in plan["scenes"]}
    order = [str(s.get("id")) for s in plan["scenes"]]
    region = set(region) if region is not None else None
    remove = reply.get("remove") or []
    if not isinstance(remove, list) or not all(isinstance(x, str) for x in remove):
        raise ValueError('"remove" must be a list of scene ids')
    if remove and region is None:
        raise ValueError("Scenes cannot be removed in this revision")
    replacements, added = {}, []
    for scene in reply.get("scenes", []):
        sid = str(scene.get("id", ""))
        if sid in original:
            if sid in replacements:
                raise ValueError("Replacement scenes must have unique existing IDs; got " + sid)
            replacements[sid] = scene
        elif region is not None and sid and sid not in [str(a.get("id")) for a in added]:
            added.append(scene)
        else:
            raise ValueError("Replacement scenes must have unique existing IDs; got " + sid)
    restructure = bool(remove or added)
    for sid, scene in replacements.items():
        if scene == original[sid]:
            continue
        if sid in allowed:
            continue
        if restructure and region is not None and sid in region:
            continue                      # an immediate neighbour taking part in a split/merge
        raise ValueError("Unrelated scene changed: " + sid)
    for rid in remove:
        if rid not in original:
            raise ValueError("Cannot remove unknown scene " + rid)
        if region is None or rid not in region:
            raise ValueError("Cannot remove scene " + rid + ": outside the scenes you may restructure")
        if rid in replacements:
            raise ValueError("Scene " + rid + " is both replaced and removed")
    if not (replacements or added or remove):
        raise ValueError("No replacement scene objects were returned")
    kept = [replacements.get(sid, original[sid]) for sid in order if sid not in remove]
    for sc in added:
        start = (_ranges([sc], cues) or [None])[0]
        start = start[0] if start else _start_of(sc)
        pos = next((k for k, x in enumerate(kept)
                    if ((_ranges([x], cues) or [None])[0] or (_start_of(x),))[0] > start), len(kept))
        kept.insert(pos, sc)
    if restructure:
        before = dict(zip(order, _ranges(plan["scenes"], cues)))
        lost = _cue_cover(before.get(r) for r in remove) | _cue_cover(
            before.get(sid) for sid, sc in replacements.items() if sc != original[sid])
        kept_ranges = _ranges(kept, cues)
        after = _cue_cover(kept_ranges)
        if not lost <= after:
            gone = sorted(lost - after)
            raise ValueError("The restructure leaves narration cue(s) %s with no scene" % gone[:6])
        changed = {str(sc.get("id")) for sc in added} | {sid for sid, sc in replacements.items() if sc != original[sid]}
        new_starts = [r[0] for sc, r in zip(kept, kept_ranges) if r and str(sc.get("id")) in changed]
        old_starts = [r[0] for sid, r in before.items() if r and (sid in remove or sid in changed)]
        if new_starts and old_starts and min(new_starts) > min(old_starts):
            raise ValueError("The restructure starts at narration cue %d but the replaced scenes started at cue %d; "
                             "those words would be handed to the previous scene (cue coverage lost)"
                             % (min(new_starts), min(old_starts)))
        touched = [original[r] for r in remove] + [original[sid] for sid, sc in replacements.items() if sc != original[sid]]
        blob = json.dumps(added + [replacements[sid] for sid in replacements if replacements[sid] != original[sid]],
                          ensure_ascii=False).lower()
        missing = [t for sc in touched for t in _explanatory_texts(sc) if t.lower() not in blob]
        if missing:
            raise ValueError("The restructure dropped explanatory text that must stay on screen: "
                             + "; ".join(repr(m) for m in missing[:5]))
    out = copy.deepcopy(plan)
    out["scenes"] = copy.deepcopy(kept)
    return out


def _revise(ask, plan, faults, narration, cues=None):
    prompt = fix_prompt(plan, faults, narration)
    allowed = revision_ids(plan, faults)
    region = region_ids(plan, allowed)
    for attempt in range(2):
        fixed = _plan_from(ask("writer", prompt, False))
        try:
            if fixed is None:
                raise ValueError("No readable scene patch")
            return merge_revision(plan, fixed, allowed, region, cues)
        except ValueError as exc:
            if attempt:
                raise RuntimeError("Scene revision rejected; existing plan preserved: " + str(exc)) from exc
            prompt = "Your revision was rejected: " + str(exc) + "\n" + fix_prompt(plan, faults, narration)


def _severity(entry):
    """'blocking' | 'minor' | None (missing or anything else: ambiguous)."""
    if isinstance(entry, dict):
        v = str(entry.get("severity") or "").strip().lower()
        return v if v in ("blocking", "minor") else None
    return None


def verdict_problems(verdict: dict, min_score: float = MIN_SCORE) -> list[str]:
    """What is wrong with a well-typed verdict, to be sent back to the judge. Every finding and fix
    needs an explicit severity (a missing or unknown one is NEVER read as minor), and the verdict must
    not contradict itself."""
    problems = []
    faults = verdict.get("faults") if isinstance(verdict.get("faults"), list) else []
    fixes = verdict.get("fixes") if isinstance(verdict.get("fixes"), list) else []
    for kind, rows in (("fault", faults), ("fix", fixes)):
        for k, e in enumerate(rows, 1):
            if _severity(e) is None:
                text = e if isinstance(e, str) else json.dumps(e, ensure_ascii=False)
                problems.append(f'{kind} {k} has no explicit severity ("blocking" or "minor"): {text[:80]}')
    blocking = [e for e in faults + fixes if _severity(e) == "blocking"]
    if verdict.get("pass") is True and blocking:
        problems.append(f'"pass" is true but {len(blocking)} finding(s) are marked blocking')
    if verdict.get("pass") is True and isinstance(verdict.get("score"), (int, float)) \
            and verdict["score"] < min_score:
        problems.append(f'"pass" is true but the score is below {min_score:g}')
    if verdict.get("pass") is False and not blocking and not problems:
        problems.append('"pass" is false but no finding is marked blocking - name what must change, or pass')
    return problems


def clarify_prompt(problems: list[str], min_score: float = MIN_SCORE) -> str:
    return ("Your verdict cannot be used as written:\n" + "\n".join("- " + p for p in problems)
            + "\nReply again with ONE JSON object and nothing else: {\"score\": <1-10>, \"pass\": true|false, "
              "\"faults\": [{\"scene\": \"id\", \"severity\": \"blocking\"|\"minor\", \"problem\": \"...\"}], "
              "\"fixes\": [{\"scene\": \"id\", \"severity\": \"blocking\"|\"minor\", \"change\": \"...\"}]}. "
              "blocking = must change before the video is made; minor = cosmetic, safe to ship. "
              "pass may be true only when nothing is blocking and the score is at least %g." % min_score)


def review_passes(verdict: dict, min_score: float = MIN_SCORE) -> bool:
    """Approved only on a fully-typed verdict (see _verdict_usable) with explicit severities
    (verdict_problems empty), pass=true, score >= the pass mark and nothing blocking: only findings and fixes
    EXPLICITLY marked minor are cosmetic. A malformed or contradictory verdict can never pass."""
    if not _verdict_usable(verdict) or verdict_problems(verdict, min_score):
        return False
    blocking, _minor = split_verdict_faults(verdict.get("faults"))
    return (verdict["pass"] is True and min_score <= verdict["score"] <= 10
            and not blocking and not _blocking_fix_texts(verdict.get("fixes")))


def _atomic_json(path, data):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def judge_prompt(narration: str, plan: dict, summary: str, min_score: float = MIN_SCORE) -> str:
    return f"""You are a strict reviewer of an explainer video's scene plan (hand-drawn whiteboard style). A narrator reads the NARRATION; the plan says what is drawn while each part is spoken. The code already checked the format and the timing. You judge the TEACHING.

Score 1-10 on: (1) every scene shows the idea the narrator is saying in that moment, with the picture that fits (something handed on or requested = flow, numbers set against each other = bars, change over time = line, parts of a whole = split, a count out of a total or a board = grid; a rule or warning = hero, two things compared point by point = compare, a plain list = stack); (2) nothing is invented or wrong against the narration, numbers and units are right; (3) labels are short, plain and readable aloud; (4) variety: not the same layout scene after scene, but no gimmicks either; (5) pacing: a new picture when the idea changes, not too busy, not a long stretch with no new picture; (6) icons and colours mean what they say (green good, red problem); an icon that only repeats its label, or an arrow that suggests a flow that is not there, is a fault; (7) number scenes (memory, precision, offload, fit_chart, counter): every number and unit matches what the narrator says, the arithmetic is right (size = params x bits / 8), nothing is invented, and a scene that should say "illustrative" does. (10) you are given the exact animation specification of every motion scene (stages, rows, levels, code, footer, step anchors): check that counts match the narration, every slot is real text, each step is anchored to the words that cause it, nothing the narrator says is left out, and that watching it teaches the idea; judge only what the specification says is drawn, never call a scene 'only a footer' when it lists animation. (9) the video does not read as a slide deck: where the narration explains a process, a hand-off, a quantity, a comparison of numbers, a change over time, a share, a dependency or evidence, the scene is a motion scene (flow, bars, line, split, grid, counter, pipeline, generation, levels, code, ...) whose steps are anchored to the narrator's words; a hero/stack/row/compare scene over such narration is a fault, and text scenes should be the short pauses between motion scenes, not most of the video. Numbers in bars, split and line must be the narrator's, or the scene is tagged illustrative. (8) every key name or acronym the narrator says (and what it stands for, when spelled out) is written on screen in the scene where it is said; a missing one is a fault.

NARRATION:
{narration}

THE PLAN AS IT WILL BE DRAWN (scene, layout, what appears, and the narration it covers):
{summary}

Reply with ONE JSON object and nothing else:
{{"score": 8.5, "pass": true, "faults": [{{"scene": "id", "severity": "blocking", "problem": "specific problem"}}], "fixes": [{{"scene": "id", "severity": "minor", "change": "the specific change"}}]}}
EVERY fault and EVERY fix is an object with scene, severity and problem/change text. severity is "blocking" (must change before the video is made) or "minor" (cosmetic; safe to ship as it is). A finding without a severity, or a plain string, is rejected and you will be asked again. "pass" may be true only when nothing is marked blocking and the score is at least {min_score:g}; "pass": false needs at least one blocking finding.
The summary includes COMPILED animation parameters with scene-relative milliseconds: these are what the renderer receives. Judge that output, not an imagined animation. This is a specification review, not a claim to have watched rendered pixels.
Distinguish tasks, workers, stages and speed multipliers; these counts are different quantities. Illustrative cell counts are allowed when clearly labelled schematic, not measured architecture. Names must be shown when spoken; expansions are needed when the narrator explains them, not necessarily before. A motion comparison needs consistent example text, but paraphrases that preserve the meaning are not factual errors. Request only changes needed to explain the subject, not motion on every warning or list.
"pass" must be an explicit boolean (true/false, never a string). score must be a number 1-10. "faults" and "fixes" must always be present as arrays, empty [] when nothing is wrong. The faults array holds ONLY findings you want changed, each naming its scene id. Scenes you checked and found correct must NOT appear in faults - do not write "no fault" or "fine" entries; the app treats them as noise. Never narrate your per-scene audit in the JSON: unchecked-in detail belongs in your thinking, not in faults. Name the scene id in every fault and fix. Do not rewrite the whole plan."""


def _no_icons(node):
    """The compiled block without icon outlines (paths are noise for a reviewer)."""
    if isinstance(node, dict):
        return {k: ("<icon>" if k == "icon_paths" else _no_icons(v)) for k, v in node.items()}
    if isinstance(node, list):
        return [_no_icons(v) for v in node]
    return node


def _general_summary(s: dict) -> list:
    """What the general motion layouts draw, in words, for the judge."""
    lay, out = s.get("layout"), []
    tag = " (tagged illustrative)" if s.get("illustrative") else ""
    if lay == "flow":
        out.append("animated flow of boxes: " + " -> ".join(str(n.get("label")) for n in s.get("nodes") or [] if isinstance(n, dict)))
        for x in s.get("sends") or []:
            if isinstance(x, dict):
                out.append(f"token '{x.get('label')}' travels box {x.get('from') or 1} -> {x.get('to') or 'last'}"
                           + (f" and returns as '{x['back']}'" if x.get("back") else "") + f" on '{x.get('at', '')}'")
    elif lay == "bars":
        out.append(f"animated bars{tag}: " + "; ".join(f"{i.get('label')} = {s.get('prefix', '')}{i.get('value')}{s.get('unit', '')}"
                                                      for i in s.get("items") or [] if isinstance(i, dict))
                   + (f"; limit line {s['limit'].get('label')} = {s['limit'].get('value')}" if isinstance(s.get("limit"), dict) else "")
                   + (f"; ratios against bar {s['ratio_to']}" if s.get("ratio_to") else ""))
    elif lay == "split":
        out.append(f"animated ring of parts{tag}" + (f" of a total {s.get('total')}" if s.get("total") else "") + ": "
                   + "; ".join(f"{i.get('label')} = {i.get('value')}" for i in s.get("items") or [] if isinstance(i, dict)))
    elif lay == "line":
        out.append(f"animated line chart{tag}{' (shape only, no numbers shown)' if s.get('shape') else ''} over {s.get('x')}: " + "; ".join(
            f"{x.get('label', 'line')} {x.get('values')}" for x in s.get("series") or [] if isinstance(x, dict)))
        for m in s.get("marks") or []:
            if isinstance(m, dict):
                out.append(f"mark at x {m.get('x')}: {m.get('text')}")
    elif lay == "grid":
        out.append(f"animated {s.get('rows')}x{s.get('cols')} grid{tag}: " + "; ".join(
            f"{x.get('label')} fills {x.get('cells')} cells" for x in s.get("steps") or [] if isinstance(x, dict)))
    return out


def summary_text(plan: dict, cues: list[dict], look: dict | None = None) -> str:
    """One block per scene for the judge: what is drawn and the words it covers."""
    texts = {int(c["index"]): c["text"].strip() for c in cues}
    resolved = recipes.resolve_anchors(plan, _cue_objs(cues))
    compiled = {s["id"]: s for s in recipes.build(plan, _cue_objs(cues)).get("scenes", [])}
    out = []
    for s in resolved["scenes"]:
        a, b = s["cue_range"]
        said = " ".join(texts.get(i, "") for i in range(a, b + 1))
        drawn = []
        for it in s.get("items") or []:
            drawn.append(f"{it.get('label')} [{it.get('icon', '-')}]")
        for sd in s.get("sides") or []:
            pts = "; ".join(str(p.get("text", "")) for p in sd.get("points") or [] if isinstance(p, dict))
            drawn.append(f"{sd.get('header')} [{sd.get('icon', '-')}]: {pts}")
        for key in ("capacity", "params_b", "layers", "limit", "bits"):
            if s.get(key) is not None and not isinstance(s[key], dict):
                drawn.append(f"{key}={s[key]}{s.get('unit', '') if key in ('capacity', 'limit') else ''}")
        for it in s.get("items") or []:
            extra = [f"{k}={it[k]}" for k in ("gb", "bits", "layers", "params_b") if it.get(k) is not None]
            if extra:
                drawn.append(f"{it.get('label')}: " + ", ".join(extra))
        if isinstance(s.get("levels"), list):
            drawn.append("animated levels, broad to fine: " + "; ".join(
                f"{g.get('label')} ({g.get('cells')} cells, on '{g.get('at', '')}')" for g in s["levels"] if isinstance(g, dict))
                + (f"; decoder '{s['decoder'].get('label')}'" if isinstance(s.get("decoder"), dict) else "")
                + (f"; result text '{s['result'].get('text')}'" if isinstance(s.get("result"), dict) else ""))
        if isinstance(s.get("stages"), list):
            drawn.append("animated pipeline of " + str(s.get("tasks", "?")) + " tasks through stages: " + "; ".join(
                f"{g.get('label')} ({g.get('workers')} workers"
                + (", locked until the stage before is finished" if g.get("waits_for_all") else "")
                + (", starts on '" + str(g["at"]) + "'" if g.get("at") else "") + ")"
                for g in s["stages"] if isinstance(g, dict)))
        for r in s.get("rows") if isinstance(s.get("rows"), list) else []:
            if isinstance(r, dict) and r.get("words"):
                how = ("one word after another, left to right" if r.get("mode") == "sequential"
                       else "a rough outline first, then the gaps are filled")
                drawn.append(f"animated row '{r.get('label')}' ({how}): " + " | ".join(map(str, r["words"]))
                             + (" - steps on: " + "; ".join(str(b.get("at")) for b in r.get("beats") or [] if isinstance(b, dict))
                                if r.get("beats") else ""))
        if isinstance(s.get("lines"), list):
            drawn.append(f"code panel {s.get('filename', '')}: " + " / ".join(map(str, s["lines"])))
            for h in s.get("highlights") or []:
                if isinstance(h, dict):
                    drawn.append(f"highlight line {h.get('line')}: {h.get('note', '')}")
        if isinstance(s.get("counter"), dict):
            c = s["counter"]
            drawn.append(f"counter {c.get('from', 0)} -> {c.get('to')} {c.get('unit', '')} '{c.get('label', '')}'")
        if isinstance(s.get("footer"), dict):
            drawn.append("footer: " + str(s["footer"].get("text", "")))
        drawn.extend(_general_summary(s))
        motion = compiled.get(s.get("id"), {}).get("remotion")
        if motion:
            drawn.append("COMPILED animation (scene-relative ms): " + json.dumps(_no_icons(motion), ensure_ascii=False))
        out.append(f"scene {s.get('id')} ({s.get('layout')}"
                   + (f", title '{s['title']}'" if s.get("title") else "") + ")\n"
                   f"  narration: {said}\n  drawn: {' | '.join(drawn)}")
    return motion_summary(resolved, cues, look) + "\n" + "\n".join(out)


class _Cue:
    """What recipes.build reads from a subtitle cue."""
    def __init__(self, c):
        self.index, self.text = int(c["index"]), c["text"]
        self.start_seconds = studio._srt_seconds(c["start"])
        self.end_seconds = studio._srt_seconds(c["end"])


def _cue_objs(cues):
    return [_Cue(c) for c in cues]


def _fix_terms(plan: dict, cues: list, terms: list) -> tuple:
    """Write every spoken key term the writer left off the screen into its scene (footer or title)."""
    if not terms:
        return plan, []
    try:
        resolved = recipes.resolve_anchors(plan, _cue_objs(cues))
    except ExplainerError:
        return plan, []                               # unresolved anchors come back to the writer first
    import copy
    notes = []
    for gap in explainer_terms.missing(resolved, terms):
        trial, added = explainer_terms.insert_missing(copy.deepcopy(plan), resolved, [gap])
        if not added:
            continue                                  # the scene already has a title and a footer: the writer decides
        sid = str(gap["scene"].get("id"))
        if not _fits(trial, cues, sid):               # no room for a footer: say it in the title instead
            trial, added = explainer_terms.insert_missing(copy.deepcopy(plan), resolved, [dict(gap, no_footer=True)])
        if added and _fits(trial, cues, sid):
            plan, notes = trial, notes + added
        else:
            notes.append(f"scene {sid}: '{gap['term']}' is not shown and the scene has no room for it - "
                         "the writer is asked to make room")
    return plan, notes


def _fits(plan: dict, cues: list, sid: str) -> bool:
    """True when the scene still fits its narration time (icons may go: they are decoration)."""
    import copy
    for drop_icons in (False, True):
        trial = copy.deepcopy(plan)
        if drop_icons:
            for s in trial.get("scenes") or []:
                if str(s.get("id")) == sid:
                    for it in s.get("items") or []:
                        if isinstance(it, dict):
                            it.pop("icon", None)
        try:
            recipes.build(trial, _cue_objs(cues))
            if drop_icons:
                plan.clear()
                plan.update(trial)                    # adopt the icon-free scene
            return True
        except ExplainerError as exc:
            if not any(f"Scene {sid}: too much for the time" in e for e in exc.errors):
                return not any(sid in e for e in exc.errors)
    return False


def _split_faults(found: list) -> tuple:
    """(blocking, advisory): a label a little long for its layout is advice for the writer and the
    judge, never a reason to skip the judge; everything else must be fixed first."""
    advisory = [f for f in found if "is longer than" in f]
    return [f for f in found if f not in advisory], advisory


_NO_FAULT_RE = re.compile(
    r"\bno faults?\b(?!\s+(?:with|in|about|regarding|on|for|except|beyond|apart|around|against)\b)"
    r"|\bnot a (?:real |spec |major )?fault\b(?!\s+(?:with|in|about|regarding|on|for|except|beyond|apart|around)\b)"
    r"|\bnothing (?:left )?to (?:fix|change)\b"
    r"|\bno fix(?:es)? (?:needed|required)\b", re.I)
_VERDICT_RE = re.compile(
    r"(?:^|[,.;:!?]\s*|\s[—–-]\s*)"
    r"(?:(?:so|all|thus|still|therefore)\s+)?(?:(?:looks|is|are|seems)\s+)?"
    r"(?:fine|good|acceptable|correct|okay|ok)\b"
    r"(?:\s*(?:[—–-]\s*)?remove\.?)?[\s.!?]*$", re.I)
_MINOR_WORD_RE = re.compile(r"^(?:minor|cosmetic|nitpick|nit|non-blocking)\b", re.I)
_MINOR_TAIL_RE = re.compile(r"(?:^|[;,])\s*minor\b[\s.!?]*$", re.I)
_ID_PREFIX_RE = re.compile(r"^\s*(?:scene\s+)?[A-Za-z][\w-]{0,39}\s*:\s*", re.I)
_LEADING_SID_RE = re.compile(r"^\s*(?:scene\s+)?([A-Za-z][\w-]*?)\s*:")

_FINDING_TEXT_KEYS = ("problem", "fault", "finding", "text", "note", "change")


def _structured_finding(e: dict) -> str | None:
    """A structured finding as one line, or None when it is MALFORMED: an object finding
    must carry non-empty text (problem/fault/finding/text/note/change for a fault,
    change/text/problem for a fix). Malformed entries are never discarded silently - the
    caller keeps them as blocking."""
    scene = str(e.get("scene") or e.get("scene_id") or e.get("id") or "").strip()
    text = ""
    for key in _FINDING_TEXT_KEYS:
        if e.get(key):
            text = str(e[key]).strip()
            break
    if not text:
        return None
    if text and scene and not re.match(r"(?i)^scene\s", text) \
            and not text.lower().startswith(scene.lower() + ":"):
        text = f"scene {scene}: {text}"
    return text


def split_verdict_faults(entries) -> tuple:
    """(blocking, minor) findings. A structured finding {"scene", "severity", "problem"} is
    classified by its severity (missing severity = blocking); one without usable text is
    MALFORMED and stays blocking (an empty entry must never let approval through).
    String findings are read conservatively, clause by clause (split on ';'):
    - cosmetic only when explicitly marked: the whole finding starts with a minor word, or
      ends with a bare '; minor' mark
    - a 'minor ...' clause with content, appended after substantive clauses, is a MIXED
      finding and stays blocking ('title overlaps...; minor colour issue too')
    - self-answered (dropped) only when the LAST clause is a bare verdict ('; good',
      'so fine — remove') or an unscoped negation ('no fault here — remove'); a scoped
      negation ('wrong count; no fault with the colours') does not dismiss the finding
    Praise words anywhere else never change the class."""
    blocking, minor = [], []
    for e in entries or []:
        if isinstance(e, dict):
            text = _structured_finding(e)
            if text is None:
                blocking.append(json.dumps(e, ensure_ascii=False, sort_keys=True))
                continue
            sev = str(e.get("severity") or "").strip().lower()
            (minor if sev == "minor" else blocking).append(text)
            continue
        text = str(e).strip()
        if not text:
            continue
        body = _ID_PREFIX_RE.sub("", text, count=1).strip()
        clauses = [c.strip() for c in body.split(";") if c.strip()]
        if not clauses:
            continue
        last = clauses[-1]
        if _MINOR_WORD_RE.match(last) and len(clauses) == 1:
            minor.append(text)                    # the whole finding is explicitly minor
            continue
        if _MINOR_TAIL_RE.search(text):
            minor.append(text)                    # ends with a bare '; minor' severity mark
            continue
        if _MINOR_WORD_RE.match(last):
            blocking.append(text)                 # a minor note appended to substance: mixed
            continue
        if _NO_FAULT_RE.search(last) or _VERDICT_RE.search(last):
            continue                              # the judge closed this finding itself
        blocking.append(text)
    return blocking, minor


def _verdict_usable(verdict) -> bool:
    """A verdict only counts when every required field is present and correctly typed:
    a finite numeric score 1-10 (not bool), a genuine boolean pass, and faults/fixes lists
    whose entries all carry usable text (a string with content, or an object with a
    non-empty problem/change). Malformed entries make the whole verdict unusable so the
    judge is asked to resend instead of the app guessing."""
    if not isinstance(verdict, dict) or not verdict:
        return False
    if type(verdict.get("pass")) is not bool:
        return False
    score = verdict.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) \
            or not math.isfinite(score) or not 1 <= score <= 10:
        return False
    for key in ("faults", "fixes"):
        value = verdict.get(key)
        if not isinstance(value, list):
            return False
        for x in value:
            if isinstance(x, str):
                if not x.strip():
                    return False
            elif isinstance(x, dict):
                if _structured_finding(x) is None:
                    return False
            else:
                return False
    return True


_VERDICT_RESEND = (
    'Your last reply was not the required verdict object. Reply again with ONE JSON object and '
    'nothing else: {"score": <number 1-10>, "pass": true or false, "faults": [...], "fixes": [...]}. '
    '"faults" and "fixes" are always present, [] when empty. A fault or fix is either a non-empty '
    'object {"scene": "<id>", "severity": "minor" or "blocking", "problem"/"change": "<text>"} - '
    'every entry needs its severity and its text; an empty object, empty string or plain string is rejected.')


def _blocking_fix_texts(fixes) -> list:
    """String fixes always block; a structured fix blocks unless it is explicitly 'minor'.
    A malformed fix object is kept as its raw JSON so the empty entry still blocks."""
    out = []
    for f in fixes or []:
        if isinstance(f, dict):
            text = _structured_finding(f)
            if text is None:
                out.append(json.dumps(f, ensure_ascii=False, sort_keys=True))
                continue
            if str(f.get("severity") or "").strip().lower() == "minor":
                continue
            out.append(text)
        elif str(f).strip():
            out.append(str(f).strip())
    return out


def _pair_fixes(faults: list[str], fixes: list) -> list[str]:
    """Attach each fix to the fault it belongs to (same scene id). Judges list fewer fixes than
    faults once minor notes are filtered, so pairing by position cross-wired the instructions."""
    notes = [str(f) for f in faults]
    taken = set()
    for fix in fixes or []:
        m = _LEADING_SID_RE.match(str(fix))
        target = None
        if m:
            pat = r"(?<![\w-])" + re.escape(m.group(1)) + r"(?![\w-])"
            target = next((i for i, f in enumerate(notes)
                           if i not in taken and re.search(pat, f, re.I)), None)
        if target is None:
            notes.append(str(fix))
        else:
            notes[target] = notes[target] + "  FIX: " + str(fix)
            taken.add(target)
    return notes


_NUMS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_ADJ = r"(?:(?:ordered|separate|sequential|different|distinct|parallel|internal|additional|more|extra|such|numbered|coarse|finer|learned|processing|successive|dependent)\s+){0,2}"
_COUNT = re.compile(r"\b(\d+|two|three|four|five|six|seven|eight|nine|ten)\s+" + _ADJ + r"(stages?|levels?|layers?|steps?|passes|rounds?)\b", re.I)
_NOUN_FOR = {"pipeline": ("stage", "step"), "levels": ("level", "layer")}
_LIMIT = {"pipeline": (2, 8), "levels": (2, 4)}


def count_faults(resolved: dict, cues: list) -> list:
    """The narrator's count must be the drawn count: 'eight stages' is never drawn as two."""
    texts = {int(c["index"]): c["text"] for c in cues}
    faults = []
    for s in resolved.get("scenes") or []:
        lay = s.get("layout")
        if lay not in _NOUN_FOR:
            continue
        drawn = len(s.get("stages") if lay == "pipeline" else s.get("levels") or [])
        a, b = s["cue_range"]
        said = " ".join(texts.get(i, "") for i in range(a, b + 1))
        stated = []
        for m in _COUNT.finditer(said):
            if m.group(2).lower().rstrip("s").rstrip("e") .startswith(tuple(n[:4] for n in _NOUN_FOR[lay])):
                stated.append((int(m.group(1)) if m.group(1).isdigit() else _NUMS[m.group(1).lower()], m.group(0)))
        if not stated or drawn in [n for n, _ in stated]:
            continue                                     # nothing stated, or the drawn count is one the narrator says
        n, phrase = max(stated)
        lo, hi = _LIMIT[lay]
        if lo <= n <= hi:
            faults.append(f"scene {s.get('id')}: the narrator says '{phrase}' but the {lay} draws {drawn}; draw exactly {n}")
        else:
            faults.append(f"scene {s.get('id')}: the narrator says '{phrase}', which a {lay} scene cannot draw "
                          f"({lo}-{hi} supported); use a flow (2-6 boxes), a grid, or a steps scene with exactly "
                          f"{n} items instead of changing the count")
    return faults


TEXT_LAYOUTS = frozenset({"row", "steps", "timeline", "compare", "stack", "hero"})
# What a genre must SHOW rather than list: at least half the narration time is motion, and text-only
# scenes come in short runs. Unknown genres have no rule; a channel can override with
# look["motion_min_share"] / ["text_run_max"] / ["text_run_seconds"] (0 turns a limit off) and pick its
# genre with look["scene_genre"]. Videos under 90 seconds are exempt.
MOTION_RULES = {"tech": {"min_share": 0.50, "text_run": 3, "text_seconds": 75.0},
                "finance": {"min_share": 0.50, "text_run": 3, "text_seconds": 75.0}}
MOTION_MIN_SECONDS = 90.0


def motion_rule(look: dict | None) -> dict:
    look = look or {}
    rule = dict(MOTION_RULES.get(str(look.get("scene_genre") or "tech"), {}))
    for key, src in (("min_share", "motion_min_share"), ("text_run", "text_run_max"),
                     ("text_seconds", "text_run_seconds")):
        if look.get(src) not in (None, ""):
            try:
                val = float(str(look[src]).replace(",", "."))
            except (TypeError, ValueError):
                continue
            if key == "min_share" and val > 1:
                val /= 100.0                    # the channel form takes a percent (20), code may pass 0.2
            rule[key] = int(val) if key == "text_run" else val
    return rule


def motion_measure(resolved: dict, cues: list) -> dict:
    """Seconds per scene and the measured mix: {total, visual, share, runs: [(ids, seconds)], per}."""
    by = {int(c["index"]): c for c in cues}
    per = []
    for sc in resolved.get("scenes") or []:
        a, b = sc.get("cue_range") or (0, 0)
        if a in by and b in by:
            sec = studio._srt_seconds(by[b]["end"]) - studio._srt_seconds(by[a]["start"])
        else:
            sec = 0.0
        per.append((str(sc.get("id")), sc.get("layout"), max(0.0, sec)))
    total = sum(x[2] for x in per) or 1.0
    visual = sum(x[2] for x in per if x[1] not in TEXT_LAYOUTS)
    runs, cur = [], []
    for sid, lay, sec in per:
        if lay in TEXT_LAYOUTS:
            cur.append((sid, sec))
        else:
            if cur:
                runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return {"total": total, "visual": visual, "share": visual / total, "per": per,
            "runs": [([i for i, _ in r], sum(s for _, s in r)) for r in runs]}


def motion_faults(resolved: dict, cues: list, total_seconds: float, look: dict | None = None) -> list:
    """The enforced motion mix: no long stretch of text-only scenes and a minimum share of mechanism /
    diagram / evidence time. Each fault names the scene ids so the writer can change exactly those."""
    rule = motion_rule(look)
    if not rule or total_seconds < MOTION_MIN_SECONDS:
        return []
    m = motion_measure(resolved, cues)
    out = []
    max_n, max_s = int(rule.get("text_run") or 0), float(rule.get("text_seconds") or 0)
    for ids, sec in m["runs"]:
        if (max_n and len(ids) > max_n) or (max_s and sec > max_s):
            out.append(f"scenes {', '.join(ids)}: {len(ids)} text-only scenes in a row ({sec:.0f} s) - at most "
                       f"{max_n} or {max_s:.0f} s; turn at least {max(1, len(ids) - max_n)} of them into a motion scene "
                       "(flow, bars, line, split, grid, counter or the genre's own) or merge neighbours, so the viewer "
                       "sees something happen, keeping every point the narrator makes")
    need = float(rule.get("min_share") or 0)
    if need and m["share"] < need:
        text = sorted((x for x in m["per"] if x[1] in TEXT_LAYOUTS), key=lambda x: -x[2])[:6]
        out.append(f"scenes {', '.join(x[0] for x in text)}: only {m['share']:.0%} of the narration time is a "
                   f"motion scene (at least {need:.0%} needed); redraw the ones where the narrator describes "
                   "something handed on or asked for (flow), numbers or a ratio (bars), a change over time (line), "
                   "parts of a whole (split), a count out of a total (grid) or how something works")
    return out


def motion_summary(resolved: dict, cues: list, look: dict | None = None) -> str:
    """One line for the judge: what the app measured."""
    m = motion_measure(resolved, cues)
    runs = sorted(m["runs"], key=lambda r: -r[1])[:1]
    longest = f"; longest text-only stretch {len(runs[0][0])} scenes / {runs[0][1]:.0f} s" if runs else ""
    return (f"MEASURED MIX: {m['share']:.0%} of the narration time is a motion scene, "
            f"the rest is text layouts{longest}.")


_FAULT_SCENE = re.compile(r"^[Ss]cene (\S+?):")


def drop_faulty_scenes(plan: dict, faults: list, most: int = 3) -> tuple:
    """Last resort after the final round: remove the few scenes the checker still rejects (never the
    first). Their words stay covered by the scene before, and the judge sees the gap. -> (plan, ids)."""
    bad = {m.group(1) for m in (_FAULT_SCENE.match(f) for f in faults) if m}
    scenes = plan.get("scenes") or []
    drop = [str(sc.get("id")) for i, sc in enumerate(scenes) if i > 0 and str(sc.get("id")) in bad]
    if not drop or len(drop) > most:
        return plan, []
    out = dict(plan)
    out["scenes"] = [sc for sc in scenes if str(sc.get("id")) not in drop]
    return out, drop


_NUMWORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
             "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
             "thousand": 1000, "half": 0.5, "quarter": 0.25, "dozen": 12}
_SCALE = {"hundred": 1e2, "thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9,
          "b": 1e9, "trillion": 1e12, "t": 1e12}
_NUM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?|\.\d+)\s*(hundred|thousand|million|billion|trillion|bn|k|m|b|t)?\b", re.I)
NUMBER_LAYOUTS = {"bars": "items", "split": "items", "line": "series"}


def said_numbers(text: str) -> set:
    """Every number the narrator says, with its scale and with the scale dropped ("1.5 million" ->
    1.5 and 1500000), plus number words and halves of percentages ("50 percent" -> 50 and 0.5)."""
    out = set()
    for m in _NUM_RE.finditer(text):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        out.add(v)
        if m.group(2):
            out.add(v * _SCALE[m.group(2).lower()])
        out.add(v / 100)
    words = re.findall(r"[a-z]+", text.lower())
    for i, w in enumerate(words):
        if w in _NUMWORDS:
            v = float(_NUMWORDS[w])
            out.add(v)
            nxt = words[i + 1] if i + 1 < len(words) else ""
            if nxt in _SCALE and nxt not in ("m", "b", "t", "k"):
                out.add(v * _SCALE[nxt])
    return out


def _close(v: float, said: set) -> bool:
    return any(abs(v - n) <= max(1e-9, abs(n) * 0.005) for n in said)


def number_faults(resolved: dict, cues: list) -> list:
    """A number drawn in bars / split / line must be one the narrator says in that scene, unless the
    scene is tagged illustrative (the viewer then sees an "illustrative" tag)."""
    texts = {int(c["index"]): c["text"] for c in cues}
    faults = []
    for s in resolved.get("scenes") or []:
        lay = s.get("layout")
        if lay not in NUMBER_LAYOUTS or s.get("illustrative") is True:
            continue
        a, b = s.get("cue_range") or (0, -1)
        said = said_numbers(" ".join(texts.get(i, "") for i in range(a, b + 1)))
        drawn = []
        if lay == "line":
            for ser in s.get("series") or []:
                if isinstance(ser, dict):
                    drawn += [(v, ser.get("label") or "line") for v in ser.get("values") or []]
        else:
            drawn = [(it.get("value"), it.get("label")) for it in s.get("items") or [] if isinstance(it, dict)]
            if lay == "split" and s.get("total") is not None:
                drawn.append((s.get("total"), "total"))
            if isinstance(s.get("limit"), dict):
                drawn.append((s["limit"].get("value"), s["limit"].get("label")))
        bad = [(v, lab) for v, lab in drawn
               if isinstance(v, (int, float)) and not isinstance(v, bool) and not _close(float(v), said)]
        if bad:
            shown = ", ".join(f"{v:g} ({lab})" for v, lab in bad[:4])
            faults.append(f"scene {s.get('id')}: {shown} {'is' if len(bad) == 1 else 'are'} not said by the narrator in "
                          "this scene; draw only the narrator's numbers, or set \"illustrative\": true when they are "
                          "an example or only show the shape")
    return faults


EMPTY_START_MS = 500     # one fade-in; anything longer at a scene's start is an empty screen


def empty_start_faults(built: dict, cues: list) -> list:
    """There is always something on screen: a scene that would open on an empty board for more
    than EMPTY_START_MS is a fault (pen scenes are already pulled forward by the builder)."""
    cue_ms = {int(c["index"]): (int(studio._srt_seconds(c["start"]) * 1000), int(studio._srt_seconds(c["end"]) * 1000))
              for c in cues}
    out = []
    for sc in built.get("scenes") or []:
        gap = recipes.first_visible_ms(sc, cue_ms)
        if gap > EMPTY_START_MS:
            out.append(f"scene {sc.get('id')}: nothing is on screen for the first {gap / 1000:.1f} s - give it a "
                       "title, or anchor its first element to the scene's opening words")
    return out


def check(plan, cues: list[dict], total_seconds: float, look: dict = None, terms: list = None) -> list[str]:
    """Faults the code can find: the recipe builder's readable errors plus pacing."""
    if not isinstance(plan, dict) or not isinstance(plan.get("scenes"), list) or not plan["scenes"]:
        return ["the reply has no \"scenes\" list"]
    ids = [s.get("id") if isinstance(s, dict) else None for s in plan["scenes"]]
    if any(not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", sid) for sid in ids):
        return ["Every scene needs an id beginning with a letter, using letters, digits, '_' or '-' only."]
    if len(set(ids)) != len(ids):
        return ["Scene ids must be unique; duplicate ids make targeted corrections ambiguous."]
    faults = []
    try:
        built = recipes.build(plan, _cue_objs(cues), look)
    except ExplainerError as exc:
        return list(exc.errors)
    except Exception as exc:  # noqa: BLE001 - a malformed plan is a fault for the writer, never a crash
        return [f"the plan could not be built ({type(exc).__name__}: {str(exc)[:160]}) - check every field has the shape in the format example"]
    scenes = built.get("scenes") or []
    low, high = counts(total_seconds, (look or {}).get("scenes_per_video") or "normal")
    if len(scenes) < low:
        faults.append(f"only {len(scenes)} scenes for {total_seconds / 60:.1f} minutes - plan about {low}-{high}")
    if len(scenes) > high * 1.3:
        faults.append(f"{len(scenes)} scenes is too many for {total_seconds / 60:.1f} minutes - merge neighbours (about {low}-{high})")
    first = plan["scenes"][0]
    resolved = recipes.resolve_anchors(plan, _cue_objs(cues))
    if terms:
        starts = {int(c["index"]): c["start"] for c in cues}
        for gap in explainer_terms.missing(resolved, terms):
            mmss = str(starts.get(gap["cue"], "")).split(",")[0].lstrip("0:") or "0:00"
            faults.append(explainer_terms.fault_text(gap, mmss))
    faults.extend(count_faults(resolved, cues))
    faults.extend(number_faults(resolved, cues))
    faults.extend(empty_start_faults(built, cues))
    faults.extend(motion_faults(resolved, cues, total_seconds, look))
    if resolved["scenes"][0]["cue_range"][0] != int(cues[0]["index"]):
        faults.append("the first scene must start with the very first words of the narration")
    for s in plan["scenes"]:
        cap = LABEL_MAX.get(s.get("layout"), 32)
        for it in s.get("items") or []:
            if len(str(it.get("label", ""))) > cap:
                faults.append(f"scene {s.get('id')}: label '{it.get('label')}' is longer than {cap} characters")
    return faults


# ---- the loop --------------------------------------------------------------------------

def normalize_plan(plan: dict) -> dict:
    """Writers shorten shapes: a footer as plain text, an item or a point as a bare string.
    Put them in the documented shape so one habit never crashes the checker or the judge's summary."""
    def obj(v, key):
        return v if isinstance(v, dict) else {key: str(v)}

    for s in plan.get("scenes") or []:
        if not isinstance(s, dict):
            continue
        if isinstance(s.get("footer"), str):
            s["footer"] = {"text": s["footer"]} if s["footer"].strip() else None
        if s.get("footer") is None:
            s.pop("footer", None)
        if isinstance(s.get("items"), list):
            s["items"] = [obj(i, "label") for i in s["items"] if i not in (None, "")]
        if isinstance(s.get("sides"), list):
            s["sides"] = [sd for sd in s["sides"] if isinstance(sd, dict)]
            for sd in s["sides"]:
                if isinstance(sd.get("points"), list):
                    sd["points"] = [obj(p, "text") for p in sd["points"] if p not in (None, "")]
    plan["scenes"] = [s for s in plan.get("scenes") or [] if isinstance(s, dict)]
    return plan


def _plan_from(reply: str):
    d = studio._parse_json_object(reply or "", want=("scenes",))
    return normalize_plan(d) if d and isinstance(d.get("scenes"), list) else None


def load_inputs(cfg, pid: int, pdir) -> dict:
    """Everything a run (and the page) needs to know about the narration and the writer's inputs."""
    srt = studio.find_srt(pdir)
    if not srt:
        raise RuntimeError("Generate the subtitles first (the scenes are timed from them)")
    cues = studio.parse_srt_cues(srt.read_text(encoding="utf-8-sig"))
    if not cues:
        raise RuntimeError("The subtitles are empty")
    total = studio._srt_seconds(cues[-1]["end"])
    narration = _timed_narration(cues)
    from . import db
    conn = db.connect(cfg.db_path)
    try:
        prod = db.get_production(conn, pid)
        direction = db.stage_extra(prod, "shots") if prod is not None else ""
        try:
            title = prod["title"] if prod is not None else ""
        except (IndexError, KeyError):
            title = ""
    finally:
        conn.close()
    from .video.explainer import validate as _val
    look = _val.project_look(str(pdir))
    terms = explainer_terms.key_terms(cues, title or "")
    return {"srt": srt, "cues": cues, "total": total, "narration": narration, "direction": direction or "",
            "title": title or "", "look": look, "terms": terms,
            "narration_fp": sps.narration_fingerprint(narration),
            "input_fp": sps.inputs_fingerprint(direction or "", look_hint(look) + _terms_hint(terms), title or "")}


def lifecycle_for(cfg, pid: int, job_running: bool = False) -> dict:
    """The state shown on the page; without subtitles there is nothing to compare drafts against."""
    pdir = studio.prod_dir(cfg, pid)
    try:
        inp = load_inputs(cfg, pid, pdir)
        return sps.lifecycle(pdir, inp["narration_fp"], inp["input_fp"], job_running)
    except RuntimeError:
        return sps.lifecycle(pdir, "", "", job_running)


START_MODES = ("auto", "continue", "revise_active", "fresh")


def _seed_plan(pdir, start: str = "auto", cand: dict | None = None, log=lambda m: None) -> tuple | None:
    """-> (plan, source name) or None for a fresh draft.
      continue       the latest draft, only when it is still applicable (not stale)
      revise_active  the active plan.json
      fresh          nothing: draft anew
      auto           the applicable draft, else the active plan, else a fresh draft
    A candidate is chosen by its bound fingerprints, never by file name or modification time."""
    if start not in START_MODES:
        raise RuntimeError("Unknown start mode: " + str(start))
    active = sps.read_json(pdir / "plan.json")
    usable = lambda d: isinstance(d, dict) and isinstance(d.get("scenes"), list) and d["scenes"]
    if start == "fresh":
        return None
    if start == "revise_active":
        if not usable(active):
            raise RuntimeError("There is no active plan to revise. Choose Start fresh.")
        return normalize_plan(copy.deepcopy(active)), "plan.json"
    if cand and cand.get("exists") and cand["status"] in ("needs_revision", "in_progress"):
        if not cand["stale"] and usable(cand["plan"]):
            return normalize_plan(copy.deepcopy(cand["plan"])), "plan_candidate.json"
        if start == "continue":
            raise RuntimeError("The saved draft is stale (" + cand["reason"] + "). "
                               "Choose Revise active plan or Start fresh.")
        log("the saved draft is stale (" + cand["reason"] + ") - not used")
    elif start == "continue":
        raise RuntimeError("There is no draft to continue. Choose Revise active plan or Start fresh.")
    if usable(active):
        return normalize_plan(copy.deepcopy(active)), "plan.json"
    return None


def _backup_dir(pdir, tag: str):
    d = pdir / "backups" / f"{tag}-{time.time_ns()}"
    d.mkdir(parents=True, exist_ok=False)
    return d


def _clear_generated(pdir, log) -> None:
    """Start fresh: the draft, its review and the last run record move into backups/. The ACTIVE
    plan.json (and its review) stays until a new plan is approved or accepted."""
    names = ["plan_candidate.json", "plan_candidate_review.json", "scene_plan_run.json"]
    present = [n for n in names if (pdir / n).exists()]
    if present:
        d = _backup_dir(pdir, "scene-plan-fresh")
        for n in present:
            os.replace(pdir / n, d / n)
        sps.audit(pdir, "fresh_start", moved=present, backup=d.name)
        log("fresh start: previous draft and run record moved to backups/" + d.name)


def run_scenes(cfg, pid: int, ask: Callable, log: Callable[[str], None] = print,
               should_stop: Callable[[], bool] = lambda: False,
               rounds: int = MAX_ROUNDS, start: str = "auto", min_score: float | None = None) -> dict:
    """ask(role, text, new_chat) -> reply text, for role 'writer' or 'judge'.
    Every result is saved as a candidate bound to the narration, the writer's inputs and the active
    plan it began from. Only the judge's approval replaces plan.json here; a draft below the bar
    ends the run with NeedsRevision and waits for a revision or a manual acceptance."""
    pdir = studio.prod_dir(cfg, pid)
    active = pdir / "plan.json"
    initial_plan = active.read_bytes() if active.exists() else None
    inp = load_inputs(cfg, pid, pdir)
    srt, cues, total, narration = inp["srt"], inp["cues"], inp["total"], inp["narration"]
    look, terms, direction = inp["look"], inp["terms"], inp["direction"]
    if terms:
        log("key terms to show on screen: " + ", ".join(t["term"] for t in terms))
    bar = float(min_score) if min_score is not None else pass_mark(cfg, pid)
    log(f"judge pass mark: {bar:g} (from the settings)")
    meta = {"min_score": bar, "narration_fp": inp["narration_fp"], "input_fp": inp["input_fp"],
            "base_digest": sps.active_digest(pdir), "start_mode": start}
    cand = sps.candidate_state(pdir, inp["narration_fp"], inp["input_fp"])
    seeded = _seed_plan(pdir, start, cand, log)
    if start == "fresh":
        _clear_generated(pdir, log)
    plan = None
    if seeded is not None:
        plan, source = seeded
        meta["seed_source"] = source
        log(f"scene plan: {len(cues)} cues, {total / 60:.1f} min - continuing from {source} "
            f"({len(plan['scenes'])} scenes); the checker and judge look at it before the writer is asked")
    else:
        log(f"scene plan: {len(cues)} cues, {total / 60:.1f} min - asking the writer")

    def save_progress(p):
        """Keep the work so a stop or a transport failure never loses the draft."""
        sps.write_json(pdir / "plan_candidate.json", p)
        sps.write_json(pdir / "plan_candidate_review.json", dict(
            meta, status="in_progress", plan_digest=sps.plan_digest(p), faults=[], fixes=[],
            checker_faults_left=[], saved_at=time.time()))

    for attempt in range(2):
        if plan is not None:
            break
        reply = ask("writer", writer_prompt(narration, total, direction,
                                            look_hint(look) + _terms_hint(terms),
                                            look.get("scenes_per_video") or "normal",
                                            motion_rule(look) if total >= MOTION_MIN_SECONDS else {},
                                            genre_of(look)), True)
        plan = _plan_from(reply)
        if plan is None:
            log("writer: no readable plan in the reply - asking once more")
            reply = ask("writer", "That was not the JSON object I asked for. Reply with ONLY the scene plan "
                                  "object {\"theme\": ..., \"scenes\": [...]} in one ```json block.", False)
            plan = _plan_from(reply)
        if plan is None:
            raise RuntimeError("the writer never gave a readable scene plan")
        save_progress(plan)
    best = None            # (rank, plan): a judged plan beats one the checker rejected
    verdict, kept_verdict, kept_problems = {}, {}, []
    revised_times: dict = {}    # scene id -> how often it was already sent back (oscillation guard)
    for rnd in range(1, rounds + 1):
        if should_stop():
            raise RuntimeError("Stopped by you.")
        plan, added = _fix_terms(plan, cues, terms)
        for note in added:
            log("auto-fix: " + note)
        plan, trimmed = recipes.simplify_overfull(plan, _cue_objs(cues), decoration_only=True)
        for note in trimmed:
            log("auto-fix (decoration only): " + note)
        faults, advisory = _split_faults(check(plan, cues, total, look, terms))
        for a in advisory:
            log("note (does not block the judge): " + a)
        if faults:
            log(f"round {rnd}: {len(faults)} fault(s) from the checker, e.g. {faults[0][:140]}")
            rank = (0, -len(faults))
            if best is None or rank > best[0]:
                best, kept_verdict, kept_problems = (rank, plan), {}, []
            if rnd == rounds:
                break
            plan = _revise(ask, plan, faults, narration, cues)
            save_progress(plan)
            continue
        if should_stop():
            raise RuntimeError("Stopped by you.")
        summary = summary_text(plan, cues, look)
        verdict = studio._parse_json_object(
            ask("judge", judge_prompt(narration, plan, summary, bar), True), want=("score",)) or {}
        if not _verdict_usable(verdict):
            log("judge: the verdict was missing score/pass/faults/fixes or had wrong types "
                "- asking the judge to resend it")
            verdict = studio._parse_json_object(
                ask("judge", _VERDICT_RESEND, False), want=("score",)) or {}
        problems = verdict_problems(verdict, bar) if _verdict_usable(verdict) else []
        if problems:
            log("judge: the verdict needs clarification - " + problems[0][:120]
                + (f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""))
            again = studio._parse_json_object(
                ask("judge", clarify_prompt(problems, bar), False), want=("score",)) or {}
            if _verdict_usable(again):
                verdict, problems = again, verdict_problems(again, bar)
            else:
                problems = problems + ["the clarified verdict was not usable"]
        try:
            raw_score = verdict.get("score")
            score = float(raw_score) if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool) else None
            if score is not None and (not math.isfinite(score) or not 1 <= score <= 10):
                score = None
        except (TypeError, ValueError):
            score = None
        log(f"round {rnd}: judge scored {score if score is not None else 'nothing'}"
            + (f" - {len(verdict.get('faults') or [])} fault(s)" if verdict.get("faults") else ""))
        reported = verdict.get("faults")
        reported = reported if isinstance(reported, list) else []
        blocking, minor = split_verdict_faults(reported)
        for m in minor:
            log("judge note (cosmetic, not blocking): " + m[:160])
        rank = (2 if review_passes(verdict, bar) else 1, score if score is not None else 0.0,
                -len(blocking))
        if best is None or rank >= best[0]:
            best, kept_verdict, kept_problems = (rank, plan), verdict, problems
        if score is None:
            log("the judge gave no usable score; the draft remains unapproved")
        if review_passes(verdict, bar):
            break
        if problems:
            log("the judge's verdict stayed ambiguous or contradictory - not guessing: the draft needs a "
                "revision or a manual decision")
            break
        if rnd == rounds:
            break
        fixes = verdict.get("fixes")
        fixes = fixes if isinstance(fixes, list) else []
        notes = _pair_fixes(blocking, _blocking_fix_texts(fixes))
        if not notes:
            notes = ["Review was not approved; resolve the teaching problems without changing correct scenes."]
        # oscillation guard: a scene already revised twice and flagged again would churn - freeze
        # it this round; when nothing but frozen scenes remains, stop instead of going in circles
        open_ids = revision_ids(plan, notes)
        stuck = {sid for sid in open_ids if revised_times.get(sid, 0) >= 2}
        if stuck:
            log(f"round {rnd}: scene(s) {', '.join(sorted(stuck))} were already revised and are still "
                "flagged - frozen for this round to avoid churn")
            notes = [n for n in notes if _ids_in(n, plan) - stuck]
            for sid in open_ids - stuck:
                revised_times[sid] = revised_times.get(sid, 0) + 1
        else:
            for sid in open_ids:
                revised_times[sid] = revised_times.get(sid, 0) + 1
        if not notes:
            log(f"round {rnd}: only already-revised scenes are still flagged - keeping the plan as "
                "it stands; accept it manually or revise it by hand")
            break
        plan = _revise(ask, plan, notes, narration, cues)
        save_progress(plan)
    final = best[1] if best else plan
    verdict, problems = kept_verdict, kept_problems
    left, _advice = _split_faults(check(final, cues, total, look, terms))
    if should_stop():
        raise RuntimeError("Stopped by you.")
    if (active.read_bytes() if active.exists() else None) != initial_plan:
        raise RuntimeError("The active scene plan changed during this run; it was not overwritten.")
    if studio.parse_srt_cues(srt.read_text(encoding="utf-8-sig")) != cues:
        raise RuntimeError("The subtitles changed during this run; the candidate was not promoted.")
    approved = not left and not problems and review_passes(verdict, bar)
    digest = sps.plan_digest(final)
    raw_faults = verdict.get("faults") if isinstance(verdict.get("faults"), list) else []
    v_blocking, v_minor = split_verdict_faults(raw_faults)
    score_out = verdict.get("score")
    score_out = score_out if isinstance(score_out, (int, float)) and not isinstance(score_out, bool) \
        and math.isfinite(score_out) else None
    review = dict(meta, status="approved" if approved else "needs_revision", **{"pass": approved},
                  score=score_out, faults=v_blocking + v_minor,
                  fixes=verdict.get("fixes") if isinstance(verdict.get("fixes"), list) else [],
                  checker_faults_left=left, verdict_problems=problems,
                  review_basis="compiled_specification", visual_review="not_performed",
                  plan_digest=digest, candidate_file="plan_candidate.json",
                  active_plan_preserved=not approved and active.exists(), judge_approved=approved,
                  reviewed_at=time.time())
    sps.write_json(pdir / "plan_candidate.json", final)
    sps.write_json(pdir / "plan_candidate_review.json", review)
    if not approved:
        detail = " | ".join(left[:3]) if left else (" | ".join(v_blocking[:3]) if v_blocking else
                  (" | ".join(problems[:2]) if problems else f"judge score {verdict.get('score')}; approval required"))
        raise NeedsRevision("Scene plan needs revision: " + detail +
                            ". The draft is saved - preview it on the page, revise it, or accept it manually; "
                            "the active plan.json was not replaced.")
    backup_name = None
    if active.exists():
        d = _backup_dir(pdir, "scene-plan")
        (d / "plan.json").write_bytes(active.read_bytes())
        backup_name = d.name
    _atomic_json(active, final)
    sps.write_json(pdir / "plan_review.json", review)
    sps.audit(pdir, "approved", plan_digest=digest, score=score_out, backup=backup_name)
    log(f"Scene plan approved: {len(final['scenes'])} scenes, judge score {verdict['score']}. "
        "Render a short preview to verify appearance and motion before the full video.")
    return final


def accept_candidate(cfg, pid: int, acknowledged: bool, expected_digest: str | None = None) -> dict:
    """Manual acceptance of a draft the judge did not approve. Refused when the draft is stale, changed
    since it was previewed, already accepted or active, technically invalid, or when the user did not
    acknowledge the unresolved findings. Records a backup and an audit line; the plan is labelled
    manually accepted, NEVER judge-approved."""
    pdir = studio.prod_dir(cfg, pid)
    inp = load_inputs(cfg, pid, pdir)
    cand = sps.candidate_state(pdir, inp["narration_fp"], inp["input_fp"])
    if not cand["exists"]:
        raise AcceptError("There is no draft to accept.")
    if cand["status"] == "accepted" or cand["digest"] == sps.active_digest(pdir):
        raise AcceptError("This draft is already the active plan; nothing was changed.")
    if cand["status"] != "needs_revision":
        raise AcceptError(f"Only a finished draft can be accepted (this one is '{cand['status']}'). "
                          "Continue it first.")
    if cand["stale"]:
        raise AcceptError("This draft is stale: " + cand["reason"] + ". Revise or start fresh.")
    if expected_digest and expected_digest != cand["digest"]:
        raise AcceptError("The draft changed since you previewed it. Reload the page and look again.")
    plan = normalize_plan(copy.deepcopy(cand["plan"]))
    left, _ = _split_faults(check(plan, inp["cues"], inp["total"], inp["look"], inp["terms"]))
    if left:
        raise AcceptError("Technical validation failed, so it cannot be accepted: " + " | ".join(left[:3]))
    review = cand["review"]
    findings = list(review.get("faults") or []) + list(review.get("verdict_problems") or [])
    if not acknowledged:
        raise AcceptError("Confirm that you understand the judge did not approve this plan "
                          f"({len(findings)} unresolved finding(s)).")
    active = pdir / "plan.json"
    backup_name = None
    if active.exists():
        d = _backup_dir(pdir, "scene-plan")
        (d / "plan.json").write_bytes(active.read_bytes())
        if (pdir / "plan_review.json").exists():
            (d / "plan_review.json").write_bytes((pdir / "plan_review.json").read_bytes())
        backup_name = d.name
    _atomic_json(active, plan)
    accepted = {"status": "manually_accepted", "pass": False, "judge_approved": False,
                "plan_digest": sps.plan_digest(plan), "accepted_at": time.time(),
                "acknowledged_findings": findings, "judge_score": review.get("score"),
                "candidate_review": review, "review_basis": "manual_acceptance"}
    sps.write_json(pdir / "plan_review.json", accepted)
    sps.write_json(pdir / "plan_candidate_review.json", dict(review, status="accepted"))
    run = sps.read_json(pdir / "scene_plan_run.json") or {}
    run.update(status="manually_accepted", finished_at=time.time())
    sps.write_json(pdir / "scene_plan_run.json", run)
    sps.audit(pdir, "manual_accept", plan_digest=accepted["plan_digest"], judge_score=review.get("score"),
              acknowledged_findings=findings, backup=backup_name, judge_approved=False)
    return accepted


# ---- transports --------------------------------------------------------------------------

def webchat_job(cfg, pid: int, writer: str, judge: str, log, should_stop=lambda: False,
                options: dict | None = None, start: str = "auto") -> dict:
    """Writer and judge are chat sites driven in the browser (the normal route)."""
    from . import webstages as ws
    from .scene_chat import SceneChats
    sites = {"writer": writer, "judge": judge}
    pdir = studio.prod_dir(cfg, pid)
    run = {"status": "running", "started_at": time.time(), "writer": writer, "judge": judge}
    _atomic_json(pdir / "scene_plan_run.json", run)
    try:
        with ws.web_transport(cfg, log, options) as t:
            t.set_stop(should_stop)

            chats = SceneChats(t, sites, log, should_stop)
            n = [0]

            def ask(role, text, new_chat):
                log(f"{sites[role]}: sending {len(text):,} characters")
                reply = chats.ask(role, text, new_chat, ready=ws._has_json)
                n[0] += 1
                tag = f"scene_{role}_reply_{n[0]}"
                ws._dump(cfg, pid, tag + "_1", reply)          # raw replies for debugging (webchat_debug)
                if role != "writer":
                    return reply
                # like the shotlist: a long plan is often cut off by the chat - send 'continue'
                # while the JSON is really incomplete, and join the pieces
                body = ws._json_chunk(reply, True)
                for i in range(ws.MAX_CONTINUES):
                    if _plan_from(body) is not None or "{" not in body:
                        break
                    log(f"{sites[role]}: the plan is cut off ({len(body):,} characters read) - "
                        f"sending 'continue' ({i + 1}/{ws.MAX_CONTINUES})")
                    more = chats.ask(role, CONTINUE_MESSAGE, False, ready=ws._has_json, continuation=True)
                    ws._dump(cfg, pid, f"{tag}_{i + 2}", more)
                    body = ws._join_chunks(body, ws._json_chunk(more, False))
                return body
            try:
                result = run_scenes(cfg, pid, ask, log, should_stop, start=start)
                run.update(status="approved", finished_at=time.time())
                return result
            except Exception as exc:
                stopped = should_stop()
                # a draft below the bar is "needs_revision", a browser/chat problem is "failed"
                kind = "stopped" if stopped else ("needs_revision" if isinstance(exc, NeedsRevision) else "failed")
                run.update(status=kind, finished_at=time.time(),
                           role=None if kind == "needs_revision" else chats.last_role, error=str(exc),
                           active_plan_preserved=True)
                raise
            finally:
                _atomic_json(pdir / "scene_plan_run.json", run)
                _atomic_json(pdir / "scene_plan_chats.json", {
                    role: {"site": sites[role], "url": url} for role, url in chats.role_urls.items()})
                ws.save_chats(cfg, pid, t)
    except Exception as exc:
        if run["status"] in ("running", "approved"):
            preserved = run["status"] != "approved"
            run.update(status="stopped" if should_stop() else "failed", error=str(exc),
                       finished_at=time.time(), active_plan_preserved=preserved)
            _atomic_json(pdir / "scene_plan_run.json", run)
        raise


def api_job(cfg, pid: int, provider: str | None, judge: str | None, log,
            should_stop=lambda: False, start: str = "auto") -> dict:
    """Writer and judge are API providers (studio.llm_generate)."""
    names = {"writer": provider, "judge": judge or provider}
    pdir = studio.prod_dir(cfg, pid)
    run = {"status": "running", "started_at": time.time(), "writer": names["writer"], "judge": names["judge"]}
    _atomic_json(pdir / "scene_plan_run.json", run)

    def ask(role, text, new_chat):
        log(f"{names[role] or 'default LLM'}: sending {len(text):,} characters ({role})")
        run["role"] = role
        return studio.llm_generate(cfg, text, provider=names[role])
    try:
        result = run_scenes(cfg, pid, ask, log, should_stop, start=start)
        run.update(status="approved", finished_at=time.time())
        return result
    except Exception as exc:
        kind = "stopped" if should_stop() else ("needs_revision" if isinstance(exc, NeedsRevision) else "failed")
        run.update(status=kind, finished_at=time.time(), error=str(exc), active_plan_preserved=True)
        raise
    finally:
        _atomic_json(pdir / "scene_plan_run.json", run)


def load_review(pdir) -> dict:
    """The latest review for the page and for tests: the active plan's review (approved or manually
    accepted, only while plan.json is still the reviewed one), or the draft's review while a draft
    waits for a decision. {} when none."""
    result = sps.read_json(pdir / "plan_review.json") or {}
    if result.get("status") in ("approved", "manually_accepted"):
        active = sps.read_json(pdir / "plan.json")
        if active is None or sps.plan_digest(active) != result.get("plan_digest"):
            result = dict(result, status="needs_revision", **{"pass": False})
            result["faults"] = ["The active plan changed after approval; review it again."]
            result["candidate_file"] = None
    cand = sps.read_json(pdir / "plan_candidate_review.json") or {}
    if cand.get("status") == "needs_revision":
        result = dict(cand)
    latest = sps.read_json(pdir / "scene_plan_run.json")
    if latest:
        result["latest_run"] = latest
    return result
