"""What the judge passes to the writer.

One fixed behaviour ("Style only"): every part of the judge's verdict goes to the
writer, notes that carry details of the original (names, quotes) are scrubbed by
the code, and the judge is told to describe the problem, never the original.

There used to be templates to choose from (per channel and per production,
plus your own in handover_templates.json). They were removed; the old database
values (productions.handover, own_channels.handover_default) are left in place
and are no longer read.
"""
from __future__ import annotations

PARTS = {
    "script": (("must_fix", "Must-fix corrections"),
               ("feedback", "Feedback (optional polish)"),
               ("weak_spans", "Weak passages")),
    "plan": (("faults", "Faults"), ("fixes", "Fixes"),
             ("other", "Any other verdict notes")),
}
ALL_PARTS = tuple(p for s in PARTS.values() for p, _ in s)
DEFAULT_ID = "style"

BUILTIN = {
    "style": {"id": "style", "name": "Style only", "builtin": True,
              "scrub": True, "guard": True, "parts": list(ALL_PARTS),
              "help": "Notes are scrubbed of anything from the original; the "
                      "judge is told to describe the problem, never the original."},
}

INSTRUCTION_KEYS = ("plan", "plan_judge", "script", "script_judge")


def get(cfg=None, tid: str | None = None) -> dict:
    return BUILTIN[DEFAULT_ID]


def for_production(cfg, prod, stage: str) -> dict:
    return BUILTIN[DEFAULT_ID]


def for_pid(cfg, pid: int, stage: str) -> dict:
    return BUILTIN[DEFAULT_ID]


def instruction(cfg, prod, key: str) -> str:
    """The extra direction for plan / plan_judge / script / script_judge: the
    production's own text (Your instructions on the production page)."""
    from . import db
    note = (db.stage_extra(prod, key) or "").strip()
    if key in ("script", "script_judge"):
        from . import openings
        policy = openings.instruction(cfg, prod, writer=key == "script")
        return "\n\n".join(x for x in (note, policy) if x)
    return note


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
    return v
