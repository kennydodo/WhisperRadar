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
from .video.explainer import recipes
from .video.explainer.validate import ExplainerError

MAX_ROUNDS = 5
# longest label each layout can show; stack rows and hero lines are wide, the others share a row
LABEL_MAX = {"stack": 48, "hero": 60, "compare": 40}
MIN_SCORE = 8.0

ICONS = (
    "box blocks layers puzzle file folder database server hard-drive cpu gpu memory-stick monitor laptop "
    "smartphone cloud globe wifi lock shield key user users message-square mail bell bookmark book-open "
    "image video music mic camera calculator ruler scale gauge hourglass clock calendar zap flame battery "
    "sun moon thermometer wrench hammer settings sliders-horizontal search eye lightbulb brain target flag "
    "rocket trophy star heart thumbs-up thumbs-down triangle-alert circle-check circle-x circle-help info "
    "wallet credit-card dollar-sign coins piggy-bank trending-up trending-down chart-bar chart-line "
    "chart-pie shopping-cart store house building car plane train bike truck map-pin route compass "
    "list-checks clipboard-list pencil trash-2 download upload refresh-cw repeat split merge arrow-right "
    "turtle snail rabbit leaf sprout tree-pine droplet shield-check badge-check scissors package gift "
    "graduation-cap briefcase handshake megaphone newspaper code terminal bug bot sparkles wand"
).split()

FORMAT = """\
{"theme": "chalkboard",
 "scenes": [
  {"id": "short-name", "start": "first words of the narration where this scene begins",
   "layout": "row" | "steps" | "timeline" | "stack" | "hero" | "compare",
   "title": "optional heading, 2-5 words",
   "items": [{"label": "2-4 words", "icon": "lucide-icon-name", "at": "words of the narration when it appears",
              "color": "optional: white yellow green red orange blue pink purple grey", "note": "optional small line"}],
   "footer": {"text": "one short takeaway", "at": "words of the narration"},
   "connect": "arrow" | "dashed" | "none", "focus": false},
  {"id": "versus", "start": "...", "layout": "compare",
   "sides": [{"header": "Left", "icon": "rocket", "at": "words", "color": "green",
              "points": [{"text": "short point", "at": "words", "mark": "check"}]},
             {"header": "Right", "icon": "snail", "at": "words", "color": "red",
              "points": [{"text": "short point", "at": "words", "mark": "cross"}]}]},
  {"id": "mem", "start": "...", "layout": "memory", "title": "optional", "capacity": 24, "unit": "GB",
   "capacity_label": "24 GB graphics card", "exact": true,
   "items": [{"label": "Model weights", "gb": 14, "at": "words"},
             {"label": "Context cache", "gb": 6, "grows_from": 1.5, "at": "words"}]},
  {"id": "bits", "start": "...", "layout": "precision", "params_b": 7,
   "items": [{"label": "16-bit", "bits": 16, "at": "words"}, {"label": "4-bit", "bits": 4, "at": "words"}]},
  {"id": "split", "start": "...", "layout": "offload", "layers": 32,
   "items": [{"label": "GPU VRAM", "layers": 20, "at": "words"}, {"label": "CPU RAM", "layers": 12, "at": "words"}]},
  {"id": "fit", "start": "...", "layout": "fit_chart", "limit": 24, "unit": "GB", "bits": 4, "fade": {"out_ms": 400},
   "items": [{"label": "7B", "params_b": 7, "at": "words"}, {"label": "70B", "params_b": 70, "at": "words"}]},
  {"id": "run", "start": "...", "layout": "pipeline", "title": "short", "tasks": 8, "motion": {"at": "words"},
   "stages": [{"label": "Stage 1", "workers": 8, "work": 1}, {"label": "Stage 2", "workers": 4, "work": 1, "waits_for_all": true, "at": "words"}]},
  {"id": "order", "start": "...", "layout": "generation", "title": "short", "motion": {"at": "words"},
   "rows": [{"label": "Way A", "mode": "sequential", "words": ["a", "b", "c", "d", "e", "f"]},
            {"label": "Way B", "mode": "plan_then_fill", "beats": [{"at": "words"}, {"at": "words"}, {"at": "words"}],
             "words": ["a", "b", "c", "d", "e", "f"]}]},
  {"id": "tree", "start": "...", "layout": "levels", "title": "short", "motion": {"at": "words"},
   "levels": [{"label": "Broad", "cells": 2, "at": "words"}, {"label": "Finer", "cells": 6, "at": "words"}],
   "decoder": {"label": "Decoder", "at": "words"}, "result": {"text": "the final text", "at": "words"}},
  {"id": "src", "start": "...", "layout": "code", "title": "short", "filename": "train.py", "motion": {"at": "words"},
   "lines": ["def f(x):", "    return x"], "highlights": [{"line": 2, "at": "words", "note": "short"}]},
  {"id": "num", "start": "...", "layout": "counter", "title": "optional",
   "counter": {"from": 0, "to": 14, "unit": "GB", "label": "a 7B model at 16-bit", "at": "words"}}
 ]}"""

RULES = """\
LAYOUTS (pick the one that teaches the sentence):
- row: 2-4 things side by side (parts, ingredients, options). Add connect "arrow" for a flow, "none" for a set.
- steps: 3-5 numbered steps in order. timeline: 3-5 points in time.
- stack: 3-4 short lines, one per row, each with an icon (lists of facts or tiers); a footer fits under it.
- hero: ONE big icon with one short sentence (a rule, a warning, the key idea). The label may be up to ~50 characters.
- compare: exactly two sides, each with a header, an icon and 1-3 short points (right vs wrong, A vs B, before vs after).
MOTION SCENES (the viewer watches the idea happen; use them where the narrator describes a process, a comparison of how two things work, or evidence):
- A video made mostly of hero, stack and row scenes is a slide deck. Wherever the narrator explains how something works, how two ways differ, what waits for what, or shows evidence, use a motion scene, about one for every 60-90 seconds of narration. Hero, stack and row are for the short statements between them. Prefer fewer, longer scenes (15-40 seconds) over many 3-6 second ones.
- First ask for every scene: what will the viewer understand by watching this that the words alone do not show? If the answer is only "they read the same point", pick a motion scene or a better picture.
- levels: a broad level first, then finer levels that split it into more cells, then an optional decoder and the resulting text; "levels": [{"label", "cells" (1-16, more each level), "at": words}], "decoder": {"label", "at"}, "result": {"text", "at"}. Use it for hierarchies, coarse-to-fine, layers, zooming in. Give each level its own words.
- REVISION SAFETY: never remove a teaching point to fit a template. Use fewer decorative elements, a different supported layout, or a longer scene boundary. Illustrative diagrams must be labelled schematic; never imply measured speed from decorative motion. Stages, workers and tasks are different quantities.
- COUNTS ARE THE NARRATOR'S: if the narration says eight stages, draw eight; never change a count or a relationship to fit a component. If a component cannot draw it (levels: 2-4, pipeline: 2-8, a row: 4-14 words), use a steps or row scene with the exact count, and say in the footer what is simplified. The app rejects a drawn count that differs from the stated one.
- pipeline: tasks (4-24) flow through 2-8 stages (as many as the narrator names: if the narration says eight stages, draw eight; labels of 14 characters when there are more than three); each stage has "workers" (1-8) and a relative "work" per task; "waits_for_all": true makes a stage wait until the whole stage before it is finished (shown locked). Use it for parallel work, queues, bottlenecks, "level 2 waits for level 1".
- generation: 1-2 rows of 4-14 short words, each row "mode": "sequential" (one after another) or "plan_then_fill" (a rough outline first, then the gaps). Use it to show two ways of producing the same thing side by side.
- code: 2-12 lines (64 characters at most) typed out, with "highlights" (line, start_ms, short note) on the line that matters. Use it only for real code or a real excerpt the narrator discusses, never invented.
- ONE DIAGRAM, MANY BEATS: a motion scene may last 20-60 seconds and keep the same diagram while the narrator moves through several points. Tie each change to the words that cause it: pipeline stages take "at" (the stage unlocks and starts when those words are said), generation rows take "beats" (a list of {"at": words}, one per pass: the outline, the next detail, the last detail; sequential rows take 2: where the writing starts and ends), code highlights take "at". Anchors for different steps must be words said in different places (at least half a second apart, never inside one short phrase). Every motion scene may carry a "footer" ({"text", "at"}): the one sentence that should stay on screen for the point. Row words must all be real final text, never "___" or placeholders: the animation already shows every slot empty until its step. Without an anchor a step is spaced evenly, which does not follow the narrator, so always anchor them.
- "motion": {"at": words, "run_ms": optional} is where the animation starts. Give each scene 6+ seconds. Colours, fonts and line style come from the channel; never choose them. At most one or two motion scenes in a row; titles under 48 characters.
NUMBER SCENES (only when the narrator states the quantities; the app draws them to scale and checks the arithmetic):
- memory: a box (GPU memory, RAM, a budget) filled by up to 4 segments, widths proportional to "gb". Segments must add up to at most "capacity" (else the scene is rejected). Use the narrator's numbers; set "exact": true when the narrator gave them, leave it off to show an "illustrative numbers" tag. "grows_from" (last segment only) makes it grow from a smaller size.
- precision: the same model at different bit sizes, 1-4 rows. Give "params_b" (billions) and each row's "bits"; the app computes size = params x bits / 8. Never type the sizes yourself.
- offload: a model's layers split across two memories (two zones that add up to "layers"). Labels must be short (about 10 characters for a small zone). Say it is simplified; real tools place layers differently.
- fit_chart: bars of model size (params_b x bits / 8) against a memory "limit" (4 models at most; over the limit turns red). counter: one big number counting from "from" to "to" with a short label. Both are drawn by a second renderer; "fade" ({"in_ms", "out_ms"}, each up to 2000) is optional. Use at most one or two of these per video.
- Do not use a number scene for a number the narrator never says. Numbers must match the narration exactly (units too).
PACING AND ANCHORS:
- The FIRST scene must start with the very first words of the narration; later scenes follow in order. Together the scenes cover the whole narration.
- "start" and every "at" are copied word for word from the narration (3 or more consecutive words, no need for punctuation) and each must be findable only in its own place: never an "at" from another scene.
- A scene lasts about 8-25 seconds. Change the picture when the narration changes idea, not on every sentence. Scene count for this video: about {low}-{high}.
- An item appears when the narrator says it: "at" is the words where it is said. Never anchor an item in the last 3 seconds of its scene, and give every item at least 2.5 seconds before the scene ends. Fewer items beat crowded scenes.
- Do not anchor two scenes on the same sentence. Items in a scene go in narration order.
TEXT:
- Labels are 2-4 words (at most 32 characters), plain words, no sentences, no emoji. Numbers and units are welcome (7B, 14 GB).
- Use an icon from the list; another valid Lucide icon name is fine if you are sure of it. Every item should have an icon.
- NAMES ARE SHOWN: when the narrator says a name, acronym or key term (for example NCM, LLM, VRAM), or spells it out ("NCM stands for Next-Concept Modeling"), write it on screen in the scene that covers those words - as the title, a label, the hero line or the footer - spelled as the narrator says it, and add what it stands for when the narrator says that. A viewer must never hear a key term that is not shown.
- Use colour to carry meaning (green = good, red = problem, orange = caution, blue = neutral info, yellow = the key thing); leave "color" off when unsure, the app picks.
- Never invent facts. Labels restate what the narrator says.
- An icon must add meaning (a thing, a place, an action), not just repeat the label's noun or decorate. Leave "icon" off when no icon helps.
- Use "connect": "arrow" only when the items really happen in order or feed each other; use "none" for a set of separate things.
- Do NOT output cue numbers or timings. Output ONLY the JSON object, in one ```json block, as text in this chat (no file)."""


def _timed_narration(cues: list[dict]) -> str:
    """'[m:ss] text' lines, so the writer can pace (it never uses the times)."""
    out = []
    for c in cues:
        t = int(studio._srt_seconds(c["start"]))
        out.append(f"[{t // 60}:{t % 60:02d}] {c['text'].strip()}")
    return "\n".join(out)


DENSITY = {"few": (38, 17), "normal": (25, 11), "many": (18, 8)}   # seconds per scene: (low end, high end)
STYLE_HINT = {
    "icons": "DIAGRAM STYLE: picture-heavy - give most points a fitting icon and prefer rows, steps and compare scenes.",
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


def writer_prompt(narration: str, total_seconds: float, direction: str = "",
                  look: str = "", density: str = "normal") -> str:
    low, high = counts(total_seconds, density)
    return f"""You are the visual planner of a hand-drawn whiteboard/chalkboard explainer video. A narrator reads the script below; you decide WHAT IS DRAWN while each part is spoken. Think like a great teacher at a board: one clear picture per idea, drawn as the words are said.

The video is {total_seconds / 60:.1f} minutes long. Return a scene plan as JSON in exactly this shape:

{FORMAT}

ICONS (Lucide names): {" ".join(ICONS)}

{RULES.replace("{low}", str(low)).replace("{high}", str(high))}
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


def fix_prompt(plan: dict, faults: list[str], narration: str = "") -> str:
    lines = "\n".join("- " + str(f) for f in faults)
    allowed = sorted(revision_ids(plan, faults))
    return ("Correct the listed scenes, preserving every other scene exactly. Do not delete scenes, "
            "change their IDs or reorder them. You may change layout, labels and anchors inside the allowed scenes. "
            "Return ONLY {\"scenes\": [complete replacement scene objects]} in one JSON block. "
            "Unchanged scenes may be omitted; they will be retained locally. A full plan is accepted only if "
            "all unrelated scenes are unchanged. Use a supported layout or adjust the allowed scene boundaries when timing is impossible; never "
            "silently omit teaching content. Use exact narration anchors; never invent timings or evidence.\n"
            "ALLOWED SCENE IDS: " + json.dumps(allowed) + "\nPROBLEMS:\n" + lines
            + "\nTIMED NARRATION:\n" + narration + "\nCURRENT PLAN:\n"
            + json.dumps(plan, ensure_ascii=False, indent=1))


def merge_revision(plan: dict, reply: dict, allowed: set[str]) -> dict:
    """Accept a scene patch or unchanged full-plan response, never silent collateral edits."""
    original = {str(s.get("id")): s for s in plan["scenes"]}
    replacements = {}
    for scene in reply.get("scenes", []):
        sid = str(scene.get("id", ""))
        if sid not in original or sid in replacements:
            raise ValueError("Replacement scenes must have unique existing IDs; got " + sid)
        if sid not in allowed and scene != original[sid]:
            raise ValueError("Unrelated scene changed: " + sid)
        replacements[sid] = scene
    if not replacements:
        raise ValueError("No replacement scene objects were returned")
    out = copy.deepcopy(plan)
    out["scenes"] = [copy.deepcopy(replacements.get(str(s.get("id")), s)) for s in plan["scenes"]]
    return out


def _revise(ask, plan, faults, narration):
    prompt = fix_prompt(plan, faults, narration)
    for attempt in range(2):
        fixed = _plan_from(ask("writer", prompt, False))
        try:
            if fixed is None:
                raise ValueError("No readable scene patch")
            return merge_revision(plan, fixed, revision_ids(plan, faults))
        except ValueError as exc:
            if attempt:
                raise RuntimeError("Scene revision rejected; existing plan preserved: " + str(exc)) from exc
            prompt = "Your revision was rejected: " + str(exc) + "\n" + fix_prompt(plan, faults, narration)


def review_passes(verdict: dict) -> bool:
    """Approved only on pass + score >= 8 with no BLOCKING finding: self-answered entries
    ('...fine; no fault here — remove') and cosmetic ones ('; minor') never block approval,
    but an unmarked fix the judge wants applied does."""
    score = verdict.get("score")
    blocking, _minor = split_verdict_faults(verdict.get("faults"))
    fixes = verdict.get("fixes")
    blocking_fixes = [f for f in (fixes if isinstance(fixes, list) else [])
                      if not _MINOR_MARK_RE.search(str(f).split(";")[-1].strip())]
    return (isinstance(score, (int, float)) and not isinstance(score, bool)
            and math.isfinite(score) and MIN_SCORE <= score <= 10
            and verdict.get("pass") is True and not blocking and not blocking_fixes)


def _atomic_json(path, data):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def judge_prompt(narration: str, plan: dict, summary: str, min_score: float = MIN_SCORE) -> str:
    return f"""You are a strict reviewer of an explainer video's scene plan (hand-drawn whiteboard style). A narrator reads the NARRATION; the plan says what is drawn while each part is spoken. The code already checked the format and the timing. You judge the TEACHING.

Score 1-10 on: (1) every scene shows the idea the narrator is saying in that moment, with the picture that fits (flow = steps/row with arrows, rule or warning = hero, two things compared = compare, tiers or lists = stack); (2) nothing is invented or wrong against the narration, numbers and units are right; (3) labels are short, plain and readable aloud; (4) variety: not the same layout scene after scene, but no gimmicks either; (5) pacing: a new picture when the idea changes, not too busy, not a long stretch with no new picture; (6) icons and colours mean what they say (green good, red problem); an icon that only repeats its label, or an arrow that suggests a flow that is not there, is a fault; (7) number scenes (memory, precision, offload, fit_chart, counter): every number and unit matches what the narrator says, the arithmetic is right (size = params x bits / 8), nothing is invented, and a scene that should say "illustrative" does. (10) you are given the exact animation specification of every motion scene (stages, rows, levels, code, footer, step anchors): check that counts match the narration, every slot is real text, each step is anchored to the words that cause it, nothing the narrator says is left out, and that watching it teaches the idea; judge only what the specification says is drawn, never call a scene 'only a footer' when it lists animation. (9) the video does not read as a slide deck: where the narration explains a process, a comparison of how two things work, a dependency or evidence, the scene is a motion scene (pipeline, generation, code) whose steps are anchored to the narrator's words; a long run of hero/stack/row scenes over such narration is a fault. (8) every key name or acronym the narrator says (and what it stands for, when spelled out) is written on screen in the scene where it is said; a missing one is a fault.

NARRATION:
{narration}

THE PLAN AS IT WILL BE DRAWN (scene, layout, what appears, and the narration it covers):
{summary}

Reply with ONE JSON object and nothing else:
{{"score": 8.5, "pass": true, "faults": ["scene id: specific problem"], "fixes": ["scene id: the specific change"]}}
The summary includes COMPILED animation parameters with scene-relative milliseconds: these are what the renderer receives. Judge that output, not an imagined animation. This is a specification review, not a claim to have watched rendered pixels.
Distinguish tasks, workers, stages and speed multipliers; these counts are different quantities. Illustrative cell counts are allowed when clearly labelled schematic, not measured architecture. Names must be shown when spoken; expansions are needed when the narrator explains them, not necessarily before. A motion comparison needs consistent example text, but paraphrases that preserve the meaning are not factual errors. Request only changes needed to explain the subject, not motion on every warning or list.
"pass" must be an explicit boolean. It is true only when the score is {min_score:g} or higher and nothing important is left to fix. The faults array holds ONLY findings you want changed, each naming its scene id. Scenes you checked and found correct must NOT appear in faults - do not write "no fault" or "fine" entries; the app treats them as noise. A cosmetic, nice-to-have finding (in faults or fixes) ends with "; minor" and does not block approval. Never narrate your per-scene audit in the JSON: unchecked-in detail belongs in your thinking, not in faults. Name the scene id in every fault and fix. Do not rewrite the whole plan."""


def summary_text(plan: dict, cues: list[dict]) -> str:
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
            if s.get(key) is not None:
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
        for r in s.get("rows") or []:
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
        motion = compiled.get(s.get("id"), {}).get("remotion")
        if motion:
            drawn.append("COMPILED animation (scene-relative ms): " + json.dumps(motion, ensure_ascii=False))
        out.append(f"scene {s.get('id')} ({s.get('layout')}"
                   + (f", title '{s['title']}'" if s.get("title") else "") + ")\n"
                   f"  narration: {said}\n  drawn: {' | '.join(drawn)}")
    return "\n".join(out)


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
    r"\bno fault\b|\bnot a (?:real |spec |major )?fault\b|\bnothing (?:left )?to (?:fix|change)\b"
    r"|\bno fix(?:es)? (?:needed|required)\b", re.I)
_VERDICT_RE = re.compile(
    r"^(?:so\s+|all\s+|looks\s+)?(?:fine|good|acceptable|correct|okay|ok)\b[\s.!?]*"
    r"(?:[—–-]\s*remove)?[\s.!?]*$", re.I)
_MINOR_MARK_RE = re.compile(r"(?:^|[;,])\s*minor\b", re.I)
_LEADING_SID_RE = re.compile(r"^\s*(?:scene\s+)?([A-Za-z][\w-]*?)\s*:")


def split_verdict_faults(entries) -> tuple:
    """(blocking, minor): judges narrate their per-scene audit, so the verdict carries entries the
    judge answers itself ('...so fine; no fault here — remove') and cosmetic ones it marks 'minor'.
    An entry only counts as self-answered when its LAST clause is the verdict ('; good',
    'so fine — remove') or an explicit negation ('no fault here'): praise words elsewhere
    ('...although the colours are good') never hide a complaint, and 'minor' marks a cosmetic
    finding only as a trailing clause, never from anywhere in the sentence."""
    blocking, minor = [], []
    for e in entries or []:
        text = str(e).strip()
        if not text:
            continue
        tail = text.split(";")[-1].strip()
        tail_clauses = [tail] + ([tail.split(",")[-1].strip()] if "," in tail else [])
        self_answered = (any(_VERDICT_RE.match(c) for c in tail_clauses)
                         or any(_NO_FAULT_RE.search(c) for c in tail_clauses))
        if _MINOR_MARK_RE.search(tail):
            minor.append(text)
        elif not self_answered:
            blocking.append(text)
    return blocking, minor


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
                          f"({lo}-{hi} supported); use a steps or row scene with exactly {n} items instead of "
                          "changing the count")
    return faults


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


def _seed_plan(pdir) -> tuple | None:
    """-> (plan, source name): the active plan (your reviewed baseline) or the best machine
    candidate, so a re-run continues from what exists instead of re-drafting every scene.
    Delete plan.json (or Start over) to force a fresh draft."""
    for name in ("plan.json", "plan_candidate.json"):
        path = pdir / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("scenes"), list) and data["scenes"]:
            return normalize_plan(copy.deepcopy(data)), name
    return None


def run_scenes(cfg, pid: int, ask: Callable, log: Callable[[str], None] = print,
               should_stop: Callable[[], bool] = lambda: False,
               rounds: int = MAX_ROUNDS) -> dict:
    """ask(role, text, new_chat) -> reply text, for role 'writer' or 'judge'.
    Saves every result as a candidate; only explicit approval replaces plan.json."""
    pdir = studio.prod_dir(cfg, pid)
    active = pdir / "plan.json"
    initial_plan = active.read_bytes() if active.exists() else None
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
    if terms:
        log("key terms to show on screen: " + ", ".join(t["term"] for t in terms))
    seeded = _seed_plan(pdir)
    plan = None
    if seeded is not None:
        plan, source = seeded
        log(f"scene plan: {len(cues)} cues, {total / 60:.1f} min - checking the existing plan "
            f"({source}, {len(plan['scenes'])} scenes) before asking the writer")
    else:
        log(f"scene plan: {len(cues)} cues, {total / 60:.1f} min - asking the writer")
    for attempt in range(2):
        if plan is not None:
            break
        reply = ask("writer", writer_prompt(narration, total, direction or "",
                                            look_hint(look) + _terms_hint(terms),
                                            look.get("scenes_per_video") or "normal"), True)
        plan = _plan_from(reply)
        if plan is None:
            log("writer: no readable plan in the reply - asking once more")
            reply = ask("writer", "That was not the JSON object I asked for. Reply with ONLY the scene plan "
                                  "object {\"theme\": ..., \"scenes\": [...]} in one ```json block.", False)
            plan = _plan_from(reply)
        if plan is None:
            raise RuntimeError("the writer never gave a readable scene plan")
    best = None            # (rank, plan): a judged plan beats one the checker rejected
    verdict, kept_verdict = {}, {}
    revised_times: dict = {}    # scene id -> how often it was already sent back (oscillation guard)
    for rnd in range(1, rounds + 1):
        if should_stop():
            raise RuntimeError("Stopped by you.")
        plan, added = _fix_terms(plan, cues, terms)
        for note in added:
            log("auto-fix: " + note)
        faults, advisory = _split_faults(check(plan, cues, total, look, terms))
        for a in advisory:
            log("note (does not block the judge): " + a)
        if faults:
            log(f"round {rnd}: {len(faults)} fault(s) from the checker, e.g. {faults[0][:140]}")
            rank = (0, -len(faults))
            if best is None or rank > best[0]:
                best, kept_verdict = (rank, plan), {}
            if rnd == rounds:
                break
            plan = _revise(ask, plan, faults, narration)
            continue
        if should_stop():
            raise RuntimeError("Stopped by you.")
        summary = summary_text(plan, cues)
        verdict = studio._parse_json_object(
            ask("judge", judge_prompt(narration, plan, summary), True), want=("score",)) or {}
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
        rank = (2 if review_passes(verdict) else 1, score if score is not None else 0.0,
                -len(blocking))
        if best is None or rank >= best[0]:
            best, kept_verdict = (rank, plan), verdict
        if score is None:
            log("the judge gave no usable score; the draft remains unapproved")
        if review_passes(verdict):
            break
        if rnd == rounds:
            break
        fixes = verdict.get("fixes")
        fixes = fixes if isinstance(fixes, list) else []
        notes = _pair_fixes(blocking, fixes)
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
                "it stands; edit those scenes by hand or drop them if they cannot fit")
            break
        plan = _revise(ask, plan, notes, narration)
    final = best[1] if best else plan
    verdict = kept_verdict
    left, _advice = _split_faults(check(final, cues, total, look, terms))
    if should_stop():
        raise RuntimeError("Stopped by you.")
    if (active.read_bytes() if active.exists() else None) != initial_plan:
        raise RuntimeError("The active scene plan changed during this run; it was not overwritten.")
    if studio.parse_srt_cues(srt.read_text(encoding="utf-8-sig")) != cues:
        raise RuntimeError("The subtitles changed during this run; the candidate was not promoted.")
    approved = not left and review_passes(verdict)
    digest = hashlib.sha256(json.dumps(final, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    raw_faults = verdict.get("faults") if isinstance(verdict.get("faults"), list) else []
    v_blocking, v_minor = split_verdict_faults(raw_faults)
    review = {"status": "approved" if approved else "needs_revision", "pass": approved,
              "score": verdict.get("score") if isinstance(verdict.get("score"), (int, float)) and not isinstance(verdict.get("score"), bool) and math.isfinite(verdict["score"]) else None,
              "faults": v_blocking + v_minor,
              "fixes": verdict.get("fixes") if isinstance(verdict.get("fixes"), list) else [], "checker_faults_left": left,
              "review_basis": "compiled_specification", "visual_review": "not_performed",
              "plan_digest": digest, "candidate_file": "plan_candidate.json",
              "active_plan_preserved": not approved and (pdir / "plan.json").exists()}
    _atomic_json(pdir / "plan_candidate.json", final)
    _atomic_json(pdir / "plan_candidate_review.json", review)
    _atomic_json(pdir / "plan_review.json", review)
    if not approved:
        detail = " | ".join(left[:3]) if left else (" | ".join(v_blocking[:3]) if v_blocking
                                                    else f"judge score {verdict.get('score')}; approval required")
        raise RuntimeError("Scene plan needs revision: " + detail +
                           ". Draft saved as plan_candidate.json; existing plan.json was not replaced.")
    active = pdir / "plan.json"
    if active.exists():
        backup = pdir / "backups" / ("scene-plan-" + str(time.time_ns()))
        backup.mkdir(parents=True, exist_ok=False)
        (backup / "plan.json").write_bytes(active.read_bytes())
    _atomic_json(active, final)
    log(f"Scene plan approved: {len(final['scenes'])} scenes, judge score {verdict['score']}. "
        "Render a short preview to verify appearance and motion before the full video.")
    return final


# ---- transports --------------------------------------------------------------------------

def webchat_job(cfg, pid: int, writer: str, judge: str, log, should_stop=lambda: False,
                options: dict | None = None) -> dict:
    """Writer and judge are chat sites driven in the browser (the normal route)."""
    from . import webstages as ws
    sites = {"writer": writer, "judge": judge}
    with ws.web_transport(cfg, log, options) as t:
        t.set_stop(should_stop)

        def ask(role, text, new_chat):
            log(f"{sites[role]}: sending {len(text):,} characters")
            return t.ask(sites[role], text, (), new_chat=new_chat, ready=ws._has_json)
        try:
            return run_scenes(cfg, pid, ask, log, should_stop)
        finally:
            ws.save_chats(cfg, pid, t)


def api_job(cfg, pid: int, provider: str | None, judge: str | None, log,
            should_stop=lambda: False) -> dict:
    """Writer and judge are API providers (studio.llm_generate)."""
    names = {"writer": provider, "judge": judge or provider}

    def ask(role, text, new_chat):
        log(f"{names[role] or 'default LLM'}: sending {len(text):,} characters ({role})")
        return studio.llm_generate(cfg, text, provider=names[role])
    return run_scenes(cfg, pid, ask, log, should_stop)


def load_review(pdir) -> dict:
    """The last judge verdict saved next to plan.json ({} when none)."""
    try:
        result = json.loads((pdir / "plan_review.json").read_text(encoding="utf-8"))
        if result.get("status") == "approved":
            try:
                active = json.loads((pdir / "plan.json").read_text(encoding="utf-8"))
                digest = hashlib.sha256(json.dumps(active, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            except (OSError, ValueError):
                digest = None
            if not digest or digest != result.get("plan_digest"):
                result = dict(result, status="needs_revision", **{"pass": False})
                result["faults"] = ["The active plan changed after approval; review it again."]
                result["candidate_file"] = None
        return result
    except (OSError, ValueError):
        return {}
