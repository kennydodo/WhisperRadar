"""Hand-over templates: what the judge passes to the writer, per production and stage.

A template says (a) which parts of the judge's verdict go to the writer, (b) whether
the code scrubs notes that carry details of the original (names, quotes), and (c)
whether the judge is told to keep the original out of its notes.

Built-ins:
  style - the current behaviour: every part, scrubbed, judge told to keep the original out.
  full  - how it used to be: every part as the judge wrote it, no scrub, no such warning.
Your own templates live in handover_templates.json next to the database.
A production picks one per stage (productions.handover = {"plan": id, "script": id});
nothing picked = "style".
"""
from __future__ import annotations

import json
import re
from pathlib import Path

TEMPLATE_FILE = "handover_templates.json"
DEFAULT_ID = "style"
STAGES = ("plan", "script")
PARTS = {
    "script": (("must_fix", "Must-fix corrections"),
               ("feedback", "Feedback (optional polish)"),
               ("weak_spans", "Weak passages")),
    "plan": (("faults", "Faults"), ("fixes", "Fixes"),
             ("other", "Any other verdict notes")),
}
ALL_PARTS = tuple(p for s in PARTS.values() for p, _ in s)

BUILTIN = {
    "style": {"id": "style", "name": "Style only (current)", "builtin": True,
              "scrub": True, "guard": True, "parts": list(ALL_PARTS),
              "help": "Notes are scrubbed of anything from the original; the "
                      "judge is told to describe the problem, never the original."},
    "full": {"id": "full", "name": "Full feedback (as it used to be)",
             "builtin": True, "scrub": False, "guard": False,
             "parts": list(ALL_PARTS),
             "help": "Everything the judge writes goes to the writer as written, "
                     "including details of the original (names, hook, ending). "
                     "Expect copied names and story beats."},
}


def template_path(cfg) -> Path:
    return Path(cfg.db_path).parent / TEMPLATE_FILE


def _clean(tid: str, raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or tid).strip()
    parts = [p for p in (raw.get("parts") or []) if p in ALL_PARTS]
    return {"id": tid, "name": name, "builtin": False,
            "scrub": bool(raw.get("scrub", True)),
            "guard": bool(raw.get("guard", True)),
            "mask_names": bool(raw.get("mask_names", False)),
            "parts": parts, "help": str(raw.get("help") or ""),
            "instructions": _clean_instructions(raw.get("instructions"))}


INSTRUCTION_KEYS = ("plan", "plan_judge", "script", "script_judge")


def _clean_instructions(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {k: str(raw.get(k) or "").strip() for k in INSTRUCTION_KEYS
            if str(raw.get(k) or "").strip()}


def user_templates(path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    out = {}
    for k, v in (data.items() if isinstance(data, dict) else []):
        key = slug(k)
        t = _clean(key, v)
        if key and key not in BUILTIN and t:
            out[key] = t
    return out


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40]


def all_templates(cfg) -> dict:
    out = {k: dict(v) for k, v in BUILTIN.items()}
    out.update(user_templates(template_path(cfg)))
    return out


def save_user_template(cfg, name: str, scrub: bool, guard: bool,
                       parts: list, help_text: str = "",
                       instructions: dict | None = None,
                       mask_names: bool = False) -> str:
    """Add or replace a user template; returns its id."""
    tid = slug(name)
    if not tid:
        raise ValueError("give the template a name")
    if tid in BUILTIN:
        raise ValueError("that name is taken by a built-in template")
    path = template_path(cfg)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    data[tid] = {"name": name.strip(), "scrub": bool(scrub), "guard": bool(guard),
                 "mask_names": bool(mask_names),
                 "parts": [p for p in parts if p in ALL_PARTS],
                 "help": help_text,
                 "instructions": _clean_instructions(instructions)}
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return tid


def rename_user_template(cfg, tid: str, new_name: str) -> bool:
    """Change only the display name; the id (what productions point at) stays."""
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("give the template a name")
    if slug(new_name) in BUILTIN:
        raise ValueError("that name is taken by a built-in template")
    path = template_path(cfg)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or tid not in data:
        return False
    data[tid]["name"] = new_name
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def delete_user_template(cfg, tid: str) -> bool:
    path = template_path(cfg)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or tid not in data:
        return False
    del data[tid]
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def choice_for(prod, stage: str) -> str:
    """The template id a production picked for a stage ('' = default)."""
    try:
        raw = prod["handover"] if prod is not None else None
        data = json.loads(raw or "{}")
    except (KeyError, IndexError, TypeError, ValueError):
        return ""
    return str(data.get(stage) or "") if isinstance(data, dict) else ""


def get(cfg, tid: str | None) -> dict:
    tpls = all_templates(cfg)
    return tpls.get(tid or DEFAULT_ID) or tpls[DEFAULT_ID]


def for_production(cfg, prod, stage: str) -> dict:
    return get(cfg, choice_for(prod, stage))


def instruction(cfg, prod, key: str) -> str:
    """The extra direction for plan / plan_judge / script / script_judge: the
    hand-over template's text for that stage first, then the production's own."""
    from . import db
    stage = "plan" if key.startswith("plan") else "script"
    mine = (db.stage_extra(prod, key) or "").strip()
    theirs = (for_production(cfg, prod, stage).get("instructions") or {}).get(key, "")
    if theirs and mine:
        return (f"Template instructions:\n{theirs}\n\n"
                f"This production's own instructions:\n{mine}\n\n"
                "If the two disagree on a point, follow this production's own "
                "instructions on that point only. Keep every other template "
                "instruction.")
    return theirs or mine


def for_pid(cfg, pid: int, stage: str) -> dict:
    from . import db
    conn = db.connect(cfg.db_path) if hasattr(db, "connect") else None
    try:
        return for_production(cfg, db.get_production(conn, pid), stage)
    finally:
        if conn is not None:
            conn.close()


# ------------------------------------------------------------ name masking
def mask_names(items, script: str, source: str, allow: str = "") -> list:
    """Replace every name the original uses (a capitalised word in mid-sentence)
    that the script under review does not already use with "[name]". The rest
    of each note is kept, so the point still reaches the writer."""
    from . import studio
    skip = studio._NAME_SKIP
    src = source or ""
    lower_src = set(re.findall(r"\b[a-z][a-z']+\b", src))
    names = {m.group(1) for m in re.finditer(
        r"(?<![.!?\n]\s)(?<!^)\b([A-Z][a-z]{2,})\b", src)}
    names = {w for w in names if w.lower() not in skip and w.lower() not in lower_src}
    own = f"{script or ''} {allow or ''}"
    names = {w for w in names if not re.search(r"\b" + re.escape(w) + r"\b", own)}
    out = []
    for it in items or []:
        text = str(it)
        for w in sorted(names, key=len, reverse=True):
            text = re.sub(r"\b" + re.escape(w) + r"\b(?:'s)?", "[name]", text)
        out.append(re.sub(r"(?:\[name\]\s*){2,}", "[name] ", text).strip())
    return out


# ------------------------------------------------------------ script stage
def script_notes(tpl: dict, must: list, feedback: list, weak: list,
                 script: str, source: str, allow: str = "") -> tuple:
    """-> (must, feedback, weak, dropped): the parts the template passes,
    scrubbed when it says so. A part the template leaves out comes back empty."""
    from . import studio
    parts = set(tpl.get("parts") or [])
    must = list(must or []) if "must_fix" in parts else []
    feedback = list(feedback or []) if "feedback" in parts else []
    weak = list(weak or []) if "weak_spans" in parts else []
    dropped = 0
    if tpl.get("scrub", True) and source:
        must, g0 = studio.scrub_for_writer(must, script, source, allow=allow)
        feedback, g1 = studio.scrub_for_writer(feedback, script, source, allow=allow)
        weak, g2 = studio.scrub_for_writer(weak, script, source, allow=allow)
        dropped = g0 + g1 + g2
    elif tpl.get("mask_names") and source:
        must = mask_names(must, script, source, allow)
        feedback = mask_names(feedback, script, source, allow)
        weak = mask_names(weak, script, source, allow)
    return must, feedback, weak, dropped


def retry_notes(tpl: dict, rating: dict, script: str, source: str,
                allow: str = "") -> list:
    """The API route's notes for the next attempt (must-fix first, then the
    feedback, else the weak passages - as the retry always did)."""
    must, feedback, weak, _ = script_notes(
        tpl, rating.get("must_fix"), rating.get("feedback"),
        rating.get("weak_spans"), script, source, allow)
    return must + (feedback or weak)


# -------------------------------------------------------------- plan stage
SELECTION_KEYS = ("closest", "alternates", "best", "pick", "narrow", "weak")


def plan_verdict(tpl: dict, verdict: dict, own_text: str, original: str) -> dict:
    """What goes back to the title writer. The judge's selection (closest,
    alternates, best, pick, narrow, weak) never does; the template decides
    which notes pass and whether they are scrubbed."""
    from . import studio
    parts = set(tpl.get("parts") or [])
    v = {}
    for k, val in (verdict or {}).items():
        if k in SELECTION_KEYS:
            continue
        group = k if k in ("faults", "fixes") else "other"
        if group in parts:
            v[k] = val
    if tpl.get("scrub", True):
        for k in ("faults", "fixes"):
            items = v.get(k)
            if isinstance(items, list):
                v[k], _gone = studio.scrub_for_writer(
                    [str(x) for x in items], own_text, original)
    elif tpl.get("mask_names") and original:
        for k in ("faults", "fixes"):
            items = v.get(k)
            if isinstance(items, list):
                v[k] = mask_names(items, own_text, original)
    return v


# ------------------------------------------------------------------ preview
# EXAMPLE TEXT ONLY, invented for the /handover preview - never from a production
SAMPLE_SOURCE = ("Zed Exampleton founded Example Freight Co in 1998, sold it, "
                 "and ended the video on a quiet harbour at sunrise.")
SAMPLE_SCRIPT = "Maya opened the old jar and counted the coins slowly."
SAMPLE_NOTES = {
    "must_fix": ["The 4% figure in the third paragraph is wrong: it is 3%."],
    "feedback": ["The hook is slow - open with a sharper question.",
                 "Open like Zed Exampleton does, with the sale of Example Freight Co.",
                 "End on a quiet harbour at sunrise, as the original does."],
    "weak_spans": ["Maya opened the old jar and counted the coins slowly."],
}


JUDGE_SUMMARY = {
    True: "Describe the problem and the kind of change only. Never name, "
          "quote or point to anything from the original.",
    False: "May refer to the original when explaining what to change.",
}


def judge_clause(guard: bool) -> str:
    """The sentence the script judge is given about its notes (read from the
    real prompt, so this page can never drift from what is sent)."""
    from . import studio
    text = studio.rating_prompt("T", "general", SAMPLE_SCRIPT, "brief", "style",
                                0.0, original=SAMPLE_SOURCE, guard=guard)
    at = text.find("forwarded to the writer")
    if at == -1:
        return ""
    return _paragraph(text, text.rfind("\n", 0, at) + 1)


def _paragraph(text: str, start: int) -> str:
    end = text.find("\n- ", start + 3)
    end2 = text.find("\n\n", start)
    ends = [e for e in (end, end2) if e != -1]
    return text[start:min(ends) if ends else len(text)].strip()


def preview(tpl: dict) -> dict:
    """What a template does with a sample verdict: the judge's instruction and
    what reaches the writer in each stage."""
    must, fb, weak, dropped = script_notes(
        tpl, SAMPLE_NOTES["must_fix"], SAMPLE_NOTES["feedback"],
        SAMPLE_NOTES["weak_spans"], SAMPLE_SCRIPT, SAMPLE_SOURCE)
    plan_in = {"faults": ["Title 2 repeats Example Freight Co wording."],
               "fixes": ["Name the real subject, not a person."],
               "score": 6.5, "closest": "x", "alternates": ["y"], "pick": "x"}
    plan_out = plan_verdict(tpl, plan_in, "the plan text", SAMPLE_SOURCE)
    guard = bool(tpl.get("guard", True))
    return {"judge_clause": judge_clause(guard),
            "judge_summary": JUDGE_SUMMARY[guard],
            "script": {"must_fix": must, "feedback": fb, "weak_spans": weak,
                       "dropped": dropped},
            "plan": plan_out}
