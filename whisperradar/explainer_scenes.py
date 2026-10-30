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


def fix_prompt(plan: dict, faults: list[str]) -> str:
    lines = "\n".join(f"- {f}" for f in faults[:20])
    return ("Your scene plan has problems found by the checker. Fix ONLY these, keep everything else exactly "
            "as it is, and reply with the COMPLETE corrected plan as one JSON object in one ```json block "
            "(text in this chat, no file).\n\nPROBLEMS:\n" + lines + "\n\nYOUR PLAN:\n"
            + json.dumps(plan, ensure_ascii=False, indent=1))


def judge_prompt(narration: str, plan: dict, summary: str, min_score: float = MIN_SCORE) -> str:
    return f"""You are a strict reviewer of an explainer video's scene plan (hand-drawn whiteboard style). A narrator reads the NARRATION; the plan says what is drawn while each part is spoken. The code already checked the format and the timing. You judge the TEACHING.

Score 1-10 on: (1) every scene shows the idea the narrator is saying in that moment, with the picture that fits (flow = steps/row with arrows, rule or warning = hero, two things compared = compare, tiers or lists = stack); (2) nothing is invented or wrong against the narration, numbers and units are right; (3) labels are short, plain and readable aloud; (4) variety: not the same layout scene after scene, but no gimmicks either; (5) pacing: a new picture when the idea changes, not too busy, not a long stretch with no new picture; (6) icons and colours mean what they say (green good, red problem); an icon that only repeats its label, or an arrow that suggests a flow that is not there, is a fault; (7) number scenes (memory, precision, offload, fit_chart, counter): every number and unit matches what the narrator says, the arithmetic is right (size = params x bits / 8), nothing is invented, and a scene that should say "illustrative" does. (8) every key name or acronym the narrator says (and what it stands for, when spelled out) is written on screen in the scene where it is said; a missing one is a fault.

NARRATION:
{narration}

THE PLAN AS IT WILL BE DRAWN (scene, layout, what appears, and the narration it covers):
{summary}

Reply with ONE JSON object and nothing else:
{{"score": 8.5, "pass": true, "faults": ["scene id: specific problem"], "fixes": ["scene id: the specific change"]}}
"pass" is true only when the score is {min_score:g} or higher and nothing important is left to fix. Name the scene id in every fault and fix. Do not rewrite the whole plan."""


def summary_text(plan: dict, cues: list[dict]) -> str:
    """One block per scene for the judge: what is drawn and the words it covers."""
    texts = {int(c["index"]): c["text"].strip() for c in cues}
    resolved = recipes.resolve_anchors(plan, _cue_objs(cues))
    out = []
    for s in resolved["scenes"]:
        a, b = s["cue_range"]
        said = " ".join(texts.get(i, "") for i in range(a, b + 1))
        said = said if len(said) < 420 else said[:400] + " ..."
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
        if isinstance(s.get("counter"), dict):
            c = s["counter"]
            drawn.append(f"counter {c.get('from', 0)} -> {c.get('to')} {c.get('unit', '')} '{c.get('label', '')}'")
        if isinstance(s.get("footer"), dict):
            drawn.append("footer: " + str(s["footer"].get("text", "")))
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


def check(plan, cues: list[dict], total_seconds: float, look: dict = None, terms: list = None) -> list[str]:
    """Faults the code can find: the recipe builder's readable errors plus pacing."""
    if not isinstance(plan, dict) or not isinstance(plan.get("scenes"), list) or not plan["scenes"]:
        return ["the reply has no \"scenes\" list"]
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


def run_scenes(cfg, pid: int, ask: Callable, log: Callable[[str], None] = print,
               should_stop: Callable[[], bool] = lambda: False,
               rounds: int = MAX_ROUNDS) -> dict:
    """ask(role, text, new_chat) -> reply text, for role 'writer' or 'judge'.
    Saves plan.json (+ plan_review.json) in the production folder."""
    pdir = studio.prod_dir(cfg, pid)
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
    log(f"scene plan: {len(cues)} cues, {total / 60:.1f} min - asking the writer")
    reply = ask("writer", writer_prompt(narration, total, direction or "", look_hint(look) + _terms_hint(terms),
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
    for rnd in range(1, rounds + 1):
        if should_stop():
            raise RuntimeError("Stopped by you.")
        plan, added = _fix_terms(plan, cues, terms)
        for note in added:
            log("auto-fix: " + note)
        plan, trimmed = recipes.simplify_overfull(plan, _cue_objs(cues))
        for note in trimmed:
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
            fixed = _plan_from(ask("writer", fix_prompt(plan, faults), False))
            if fixed is None:
                log("writer: the corrected plan was not readable - keeping the last one")
                break
            plan = fixed
            continue
        summary = summary_text(plan, cues)
        verdict = studio._parse_json_object(
            ask("judge", judge_prompt(narration, plan, summary), True), want=("score",)) or {}
        try:
            score = float(verdict.get("score"))
        except (TypeError, ValueError):
            score = None
        log(f"round {rnd}: judge scored {score if score is not None else 'nothing'}"
            + (f" - {len(verdict.get('faults') or [])} fault(s)" if verdict.get("faults") else ""))
        rank = (1, score if score is not None else 0.0)
        if best is None or rank > best[0]:
            best, kept_verdict = (rank, plan), verdict
        if score is None:
            log("the judge gave no usable score - keeping this plan (it passed the checker)")
            break
        if score >= MIN_SCORE and verdict.get("pass") is not False:
            break
        if rnd == rounds:
            break
        notes = [f"{a}  FIX: {b}" for a, b in zip(verdict.get("faults") or [],
                                                   (verdict.get("fixes") or []) + [""] * 20)]
        fixed = _plan_from(ask("writer", fix_prompt(plan, notes or ["raise the quality"]), False))
        if fixed is None:
            break
        plan = fixed
    final = best[1] if best else plan
    verdict = kept_verdict
    left, _advice = _split_faults(check(final, cues, total, look, terms))
    (pdir / "plan.json").write_text(json.dumps(final, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (pdir / "plan_review.json").write_text(json.dumps(
        {"score": verdict.get("score"), "faults": verdict.get("faults") or [],
         "fixes": verdict.get("fixes") or [], "checker_faults_left": left}, indent=1), encoding="utf-8")
    log(f"plan.json saved: {len(final['scenes'])} scene(s)"
        + (f" - {len(left)} checker fault(s) still open, see the log" if left else ""))
    if left:
        raise RuntimeError("the plan still has faults: " + " | ".join(left[:4]))
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
        return json.loads((pdir / "plan_review.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
