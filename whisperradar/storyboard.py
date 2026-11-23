"""Storyboard: decide WHAT each shot shows before any image prompt is written.

Pass 1 (this module + webstages.run_storyboard): from the subtitles the writer
returns one row per beat - where it starts in the narration, who is in it,
where, whether the event is actual / hypothetical / planned, and ONE action -
and no prompt. The app finds the cue range of every row, runs the checks below
(no LLM) and saves storyboard.json. You read and edit the rows, then approve.

Pass 2 (webstages.run_storyboard_prompts): only an APPROVED storyboard is turned
into prompts, refs and shotlist.json. A production without an approved
storyboard keeps the existing shotlist/beats flow untouched.

storyboard.json:
  {"style": str, "approved": bool, "rows": [row...], "faults": [...],
   "warnings": [...], "compiled": {asset: row-hash}}
row = {starts_with, scene, type, motion, who: [names], where, objects: [names], status, action,
       cues: "a-b" (derived), asset: filled when the shotlist is built}
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import beats as beatsmod

FILE = "storyboard.json"
STATUSES = ("actual", "hypothetical", "planned", "remembered", "imagined")
_STATUS_ALIASES = {"flashback": "remembered", "memory": "remembered",
                   "past": "remembered", "future": "planned",
                   "real": "actual", "metaphor": "imagined"}
EDITABLE = ("starts_with", "scene", "type", "motion", "who", "where", "objects", "status", "action")
PLACES = ("clinic", "hospital", "garage", "dealership", "store", "grocery",
          "bank", "office", "kitchen", "apartment", "gym", "school",
          "restaurant", "pharmacy", "warehouse", "airport", "church",
          "park", "gas station", "laundromat", "dentist", "vet")
SIT_RE = re.compile(r"\b(sits?|sitting|seated|sat)\b", re.I)
STAND_RE = re.compile(r"\b(stands?|standing)\b", re.I)
MAX_SAME_POSTURE_RUN = 4


# ---- rows -------------------------------------------------------------------------

def _names(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,;/]", value)
    out = []
    for v in value or []:
        v = re.sub(r"\s+", " ", str(v)).strip()
        if v and v.lower() not in ("none", "nobody", "n/a") and v not in out:
            out.append(v)
    return out


def clean_status(value) -> str:
    s = str(value or "").strip().lower()
    s = _STATUS_ALIASES.get(s, s)
    return s if s in STATUSES else ""


def clean_row(raw, prev: dict | None = None, allowed=None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    sw = re.sub(r"\s+", " ", str(raw.get("starts_with") or "")).strip()
    action = re.sub(r"\s+", " ", str(raw.get("action") or "")).strip()
    if not sw:
        return None
    try:
        scene = int(raw.get("scene"))
    except (TypeError, ValueError):
        scene = int(prev["scene"]) if prev else 1
    typ = str(raw.get("type") or "SCN").strip().upper()
    if typ not in beatsmod.TYPE_CODES:
        typ = "SCN"
    row = {"starts_with": sw, "scene": max(1, scene), "type": typ,
           "motion": str(raw.get("motion") or "").strip().upper(),
           "who": _names(raw.get("who")),
           "where": re.sub(r"\s+", " ", str(raw.get("where") or "")).strip(),
           "objects": _names(raw.get("objects")),
           "status": clean_status(raw.get("status")), "action": action}
    for k in beatsmod.SHOT_FIELDS:
        if raw.get(k) not in (None, "", [], {}):
            row[k] = raw[k]
    return row


def parse(reply: str, allowed=None) -> dict | None:
    """{"style", "rows"} from a reply; None for a missing or cut-off object."""
    obj = beatsmod._object_with(reply, "rows")
    if not obj:
        return None
    rows, prev = [], None
    for raw in obj["rows"]:
        r = clean_row(raw, prev, allowed)
        if r:
            rows.append(r)
            prev = r
    if not rows:
        return None
    return {"style": str(obj.get("style") or "").strip(), "rows": rows}


def parse_many(text: str, allowed=None) -> dict | None:
    """Rows from a PASTED reply: one JSON object, or several (the LLM's first answer
    plus its answers to "continue"), in the order pasted. Cut-off objects are skipped.
    None when no row can be read."""
    text = re.sub(r"```[a-z]*", "", text or "")
    raw_rows, style, i = [], "", 0
    while i < len(text):
        if text[i] != "{":
            i += 1
            continue
        end = beatsmod._balanced_end(text, i)
        obj = None
        if end is not None:
            try:
                obj = json.loads(text[i:end])
            except ValueError:
                obj = None
        if isinstance(obj, dict) and isinstance(obj.get("rows"), list):
            raw_rows += obj["rows"]
            style = style or str(obj.get("style") or "").strip()
            i = end
        else:
            i += 1
    rows, prev = [], None
    for raw in raw_rows:
        r = clean_row(raw, prev, allowed)
        if r:
            rows.append(r)
            prev = r
    return {"style": style, "rows": rows} if rows else None


def row_hash(row: dict) -> str:
    parts = [row.get("who"), row.get("where"), row.get("status"), row.get("action")]
    if row.get("objects"):          # added later: rows without objects keep their old hash
        parts.append(row["objects"])
    key = json.dumps(parts, ensure_ascii=False)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def note_for(row: dict) -> str:
    """The judge-only note a shot carries: who, where, status."""
    bits = [", ".join(row.get("who") or []) or "nobody",
            row.get("where") or "", row.get("status") or ""]
    return ", ".join(b for b in bits if b)


# ---- checks (no LLM) ------------------------------------------------------------------

def row_ids(rows) -> list[str]:
    """The shot id of each row as the asset names use it (S02_05): scene number and
    the position inside that scene. This is how the person refers to a shot."""
    count: dict = {}
    out = []
    for r in rows:
        try:
            sc = int(r.get("scene"))
        except (TypeError, ValueError):
            sc = 0
        count[sc] = count.get(sc, 0) + 1
        out.append(f"S{sc:02d}_{count[sc]:02d}")
    return out


def relabel(text, ids) -> str:
    """Replace 'row 33' / 'beat 33' in a message by the shot id (S04_07)."""
    def one(m):
        n = int(m.group(2))
        return ids[n - 1] if 1 <= n <= len(ids) else m.group(0)
    return re.sub(r"\b(row|beat) (\d+)\b", one, str(text))


def check_rows(rows, cues, allowed, cap_s, tol, profile=None, allow_reveal=False,
               known_names=None):
    """(starts, ranges, faults, warnings). Faults block approval; warnings are
    advice for the reader."""
    starts, ranges, faults = beatsmod.check(rows, cues, allowed, cap_s, tol,
                                            allow_reveal=allow_reveal, profile=profile)
    warnings: list[str] = []
    # fragmentation: more rows than the channel allows covering a single subtitle line
    share = getattr(profile, "single_cue_share", None) if profile is not None else None
    if share is not None and not getattr(profile, "per_cue", False) and len(rows) > 1:
        one = [n for n, (a, z) in enumerate(ranges, 1) if a and a == z]
        cap = beatsmod.briefs.pacing_share_cap(profile, "single_cue")
        if len(one) / len(rows) > cap + 1e-9:
            faults.append(
                f"{len(one)} of {len(rows)} rows cover a single subtitle line (the channel allows "
                f"{cap * 100:g}%): changing the image more often than the idea changes is "
                f"fragmentation. Join neighbouring rows of one idea: rows "
                + ", ".join(str(n) for n in one[:30]) + ".")
    by = {int(c["index"]): c.get("text", "") for c in cues}
    known = {n.lower() for n in (known_names or [])}
    prev_action = ""
    run_kind, run_len = "", 0
    for n, (r, (a, z)) in enumerate(zip(rows, ranges), 1):
        if not r.get("action"):
            faults.append(f"row {n}: write one action (what the viewer sees).")
        if not r.get("status"):
            faults.append(f"row {n}: status must be one of {', '.join(STATUSES)}.")
        if not r.get("where"):
            warnings.append(f"row {n}: no place given.")
        if known:
            bad = [w for w in r.get("who", []) if w.lower() not in known]
            if bad:
                faults.append(f"row {n}: unknown character {', '.join(bad)} "
                              f"(known: {', '.join(sorted(known_names))}).")
        text = " ".join(by.get(c, "") for c in range(a, z + 1)) if a else ""
        named = [p for p in PLACES if re.search(r"\b" + re.escape(p) + r"s?\b", text.lower())]
        where = (r.get("where") or "").lower() + " " + (r.get("action") or "").lower()
        missing = [p for p in named if p not in where]
        if missing:
            warnings.append(f"row {n}: the narration names {', '.join(missing)} "
                            f"but the row does not show it.")
        act = (r.get("action") or "").strip().lower()
        if act and prev_action and act != prev_action:
            wa, wb = set(re.findall(r"[a-z']+", act)), set(re.findall(r"[a-z']+", prev_action))
            if len(wa) >= 6 and len(wb) >= 6 and len(wa & wb) / len(wa | wb) >= 0.8:
                warnings.append(f"row {n}: nearly the same wording as row {n - 1} - check "
                                f"they show different images (advisory).")
        if act and act == prev_action:
            warnings.append(f"row {n}: same action as the previous row.")
        prev_action = act or prev_action
        kind = "sit" if SIT_RE.search(act) else "stand" if STAND_RE.search(act) else ""
        if kind and kind == run_kind:
            run_len += 1
        else:
            run_kind, run_len = kind, 1 if kind else 0
        if kind and run_len == MAX_SAME_POSTURE_RUN + 1:
            ing = "sitting" if kind == "sit" else "standing"
            warnings.append(f"row {n}: {run_len} {ing} actions in a row - vary what "
                            f"the character does.")
    return starts, ranges, faults, warnings


# ---- the pass-1 prompt ----------------------------------------------------------------

def prompt(script: str, elements_md: str, style: str, bible: str, allowed: list[str],
           cap_s: float, max_words: int, channel_context: str = "",
           min_words: int = 0, animation_note: str = "",
           min_hold_s: float = 0, timing: str = "") -> str:
    codes = ", ".join(allowed)
    types = "; ".join(f"{k} = {v}" for k, v in beatsmod.briefs.TYPE_CODES.items())
    el = ((elements_md or "").strip()
          or "(none - name recurring characters and places the same way each time yourself)")
    blocks = [
        "You are the STORYBOARD artist of a narrated video. Read the NARRATION below and "
        "split it into ROWS. One row = ONE image that stays on screen while its part of "
        "the narration is spoken. You decide what the viewer SEES in each row. You do NOT "
        "write image prompts: another step does that after a person approves your rows.",
        "HOW ROWS WORK:\n"
        "- Rows follow the script in order and together cover ALL of it, first word to last. "
        "No gaps.\n"
        f"- `starts_with`: the first {beatsmod.QUOTE_WORDS} words of the script where this row "
        "begins, copied EXACTLY as written. A row lasts until the next row's `starts_with`. "
        "You never write subtitle cue numbers.\n"
        f"- A row is spoken in about {cap_s:g} seconds: at most about {max_words} words. Split "
        "where the idea, the subject or the example changes."
        + (f" A row should last at least {min_hold_s:g} seconds"
           + (f" (about {min_words} words)" if min_words else "")
           + ": join very short ideas to a neighbour." if min_hold_s else
           (f" A row should carry at least about {min_words} words." if min_words else "")),
        "EACH ROW HAS:\n"
        '- "starts_with": see above.\n'
        '- "scene": a whole number; rows of one part of the story share it; it only goes up.\n'
        f'- "type": one of {types}.\n'
        f'- "motion": one of {codes}. Vary it. {beatsmod.motion_hint(allowed)}\n'
        '- "who": the characters visible in this image, by the exact names of the bible or '
        'elements below ([] when nobody; the presenter is a character like any other).\n'
        '- "where": the place, in a few words (kitchen, clinic waiting area, parking lot).\n'
        '- "objects": the recurring objects clearly visible in this image (a car, a table, a '
        'mug, a document), by their exact names from the elements below, or one short name '
        'you use the SAME way every time; [] when none. An object that appears in 3 or more '
        'rows gets a reference image so it looks identical each time, so list it in every row '
        'where it is visible. Do not list one-off props or things only mentioned.\n'
        f'- "status": one of {", ".join(STATUSES)}. `actual` = it happens in the story; '
        "`hypothetical` = the narration says what WOULD happen or what someone could do; "
        "`planned` = decided but not yet done; `remembered` = an earlier event that DID happen, "
        "told as backstory; `imagined` = a character pictures it. `actual` and `remembered` "
        "are shown as events that happened (a remembered one at its earlier time); "
        "`hypothetical`, `planned` and `imagined` show the described event itself, including "
        "a finished result when the narration describes one, but it did not happen in the "
        "story.\n"
        '- "action": ONE sentence in the present tense: what the character visibly DOES in '
        "this moment.",
        "RULES FOR THE ACTION:\n"
        "- A believable everyday action that matches the narration: someone doing the thing "
        "the narration is about, in the place it names. If the narration names a place "
        "(clinic, hospital, garage, store, bank, office...), the row shows that place.\n"
        "- No invented metaphors, symbols or stand-in props (a seesaw for a budget, a bridge "
        "for a plan). Charts, screens and papers may support an action; they are not the action.\n"
        "- Vary what the characters do from row to row: walking, carrying, cooking, driving, "
        "paying, climbing stairs, talking, reading a screen. Do not default to sitting at a "
        "desk or standing and holding a phone or papers. Do not repeat an action in "
        "neighbouring rows.\n"
        "- Only what a viewer can SEE: show hesitation, worry or a decision through posture, "
        "gaze, expression or gesture. Never write what a character is thinking or "
        "considering.\n"
        "- Do not invent story events, meetings or participants that the narration does not "
        "state. Ordinary setting details needed to show the narrated action are fine.\n"
        "- One depictable moment per row. Never write commentary about the image or about what "
        "it should \"convey\".\n"
        "- Follow the CHANNEL RULES for who appears (a presenter, a narrator on screen, "
        "recurring characters) and for text on screen.",
        "ELEMENTS (recurring characters, places and objects, each with ONE fixed look):\n" + el,
    ]
    if animation_note:
        blocks.append(animation_note)
    if channel_context:
        blocks.insert(1, "CHANNEL RULES (this channel's rules for what the images show: they "
                      "apply to the rows you write; ignore any mention of a shotlist, shots, image files, "
                      "cue numbers or sections and blocks that are not included here - you write a STORYBOARD, in rows):\n" + channel_context)
    style = (style or "").strip()
    if style:
        blocks.append("CHANNEL VISUAL STYLE (the images will be drawn in this style):\n" + style)
    if (bible or "").strip():
        blocks.append("CHARACTER / REFERENCE BIBLE:\n" + bible.strip())
    else:
        blocks.append("There is no character bible for this channel. Do not ask for one.")
    blocks.append(
        "OUTPUT: reply with ONE JSON object in ONE ```json block, as text in this chat (no "
        "file, no canvas, no download), and nothing after it:\n"
        '{"rows": [{"starts_with": "...", "scene": 1, "type": "SCN", "motion": "ZI", '
        '"who": ["Joe"], "where": "kitchen", "objects": [], "status": "actual", "action": "..."}]}\n'
        "If you reach your output limit, stop after the last COMPLETE row. When I reply "
        '"continue", output ONLY the remaining rows in a new ```json block, no repetition, '
        "finishing the object.")
    if timing.strip():
        blocks.append(
            "NARRATION (the script exactly as the voice says it, one sentence per line. Plan "
            "your rows from these sentences and use their opening words as `starts_with`):\n"
            + (script or "").strip())
        hold = (f" every row must also hold at least {min_hold_s:g}s." if min_hold_s else "")
        blocks.append(
            "AUDIO TIMING (the same narration as subtitle cues, `N: text (seconds)`; the "
            "seconds are how long each cue is spoken). Use it ONLY to check how long a row "
            "lasts: a row lasts the sum of the seconds of the cues it covers, so add them up and "
            f"keep every row under {cap_s:g}s;{hold} A cue is a timing unit, not a row: never "
            "make one row per cue, and do not write cue numbers in your reply. A row starts "
            "where a cue starts, so start it at the beginning of a sentence unless the "
            "sentence alone is longer than the maximum. If the sentences and the cues "
            "spell a word differently, the cues match the audio: follow them.\n" + timing.strip())
    else:
        blocks.append("NARRATION (the script exactly as the voice says it, one sentence per line. It is a script, not a subtitle file: there are no cue numbers or timings; use the opening words as `starts_with`):\n"
                      + (script or "").strip())
    return "\n\n".join(blocks) + "\n"


def fix_prompt(faults: list[str], rows: list[dict]) -> str:
    """Ask for the WHOLE corrected storyboard when a hard fault remains."""
    keep = json.dumps({"rows": rows}, ensure_ascii=False)
    return ("Your storyboard has faults the app found. Fix ONLY what they name, keep every "
            "other row exactly as it is, and reply with the COMPLETE corrected object in ONE "
            "```json block (same format, `rows` only).\n\nFAULTS:\n- "
            + "\n- ".join(faults[:40]) + "\n\nYOUR STORYBOARD:\n" + keep + "\n")


# ---- the pass-2 prompt ----------------------------------------------------------------

def prompts_prompt(rows: list[dict], ranges, cues, style: str, bible: str,
                   channel_context: str = "", refs_note: str = "",
                   ref_budget: int = 0, row_refs=None) -> str:
    """Prompts for an APPROVED storyboard. The casting, place, status and action of
    every row are decisions already taken."""
    from . import studio as _studio
    room = _studio.FLOWBATCH_MAX_PROMPT_CHARS - (len(style) + 1 if (style or "").strip() else 0)
    budget_note = (f"Style plus prompt must fit {_studio.FLOWBATCH_MAX_PROMPT_CHARS} characters, "
                   f"so each prompt is at most {room} characters.")
    by = {int(c["index"]): c.get("text", "") for c in cues}
    lines = []
    for n, (r, (a, z)) in enumerate(zip(rows, ranges), 1):
        narr = " ".join(by.get(c, "") for c in range(a, z + 1)).strip()
        lines.append(json.dumps({
            "n": n, "narration": narr, "who": r["who"], "where": r["where"],
            "status": r["status"], "action": r["action"], "motion": r["motion"],
            **({"refs": row_refs[n - 1]} if row_refs else {})},
            ensure_ascii=False))
    blocks = [
        "A person has APPROVED the storyboard below. Write the finished image PROMPT for "
        "every row. Do not change who is in a row, where it is, its status or its action: "
        "describe exactly that moment. Do not add people, places, props or actions that the "
        "row does not have; do not merge, split or reorder rows.",
        "EACH PROMPT:\n"
        "- The one thing the viewer sees, who is in it (by name), the place, the composition. "
        f"{budget_note}\n"
        "- Names every character in the row's `who` and shows the row's `where`.\n"
        "- Shows the status correctly: `actual` as happening; `remembered` as something that "
        "happened at an earlier time; `hypothetical`, `planned` and `imagined` show the "
        "described event itself (including a finished result if the narration describes one) "
        "as something that did not actually happen in the story.\n"
        "- Do NOT repeat the style (it is added separately). No commentary about the image. "
        "No readable text or numbers unless the channel text policy allows them.",
    ]
    if channel_context:
        blocks.insert(0, "CHANNEL PLANNING BRIEF AND INPUTS:\n" + channel_context)
        blocks.insert(1, "PROMPT ADAPTER - the storyboard rows below replace the brief's "
                      "planning tasks and output schema: you only write the prompts. Keep the "
                      "channel's rules for style, composition, text and references.")
    if (style or "").strip():
        blocks.append("CHANNEL VISUAL STYLE:\n" + style.strip())
    if (bible or "").strip():
        blocks.append("CHARACTER / REFERENCE BIBLE:\n" + bible.strip())
    if refs_note:
        blocks.append(refs_note + (
            f"\nA reference description (refPrompts) must be at most {ref_budget} characters."
            if ref_budget else ""))
    blocks.append(
        "OUTPUT: ONE JSON object in ONE ```json block, as text in this chat, nothing after it:\n"
        '{"prompts": [{"n": 1, "prompt": "...", "refs": ["CH_NAME"]}]} '
        '(plus top-level "refs" / "refPrompts" only for NEW references, as described above)\n'
        "`refs` lists only the reference images visible in that image, when the channel uses "
        "references. If you reach your output limit, stop after the last COMPLETE prompt; "
        'when I reply "continue", output ONLY the remaining prompts.')
    blocks.append("APPROVED STORYBOARD (one JSON row per line):\n" + "\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def parse_prompts(reply: str) -> tuple[dict[int, dict], dict]:
    """({n: {"prompt", "refs"?}}, {"refs": {...}, "refPrompts": {...}}) from a
    reply (possibly cut off); empty results when nothing complete is there."""
    obj = beatsmod._object_with(reply, "prompts")
    out: dict[int, dict] = {}
    if not obj:
        return out, {}
    for item in obj["prompts"]:
        if not isinstance(item, dict):
            continue
        try:
            n = int(item.get("n"))
        except (TypeError, ValueError):
            continue
        p = re.sub(r"\s+", " ", str(item.get("prompt") or "")).strip()
        if p:
            d = {"prompt": p}
            if isinstance(item.get("refs"), list):
                d["refs"] = [str(x) for x in item["refs"]]
            out[n] = d
    return out, beatsmod.metadata(obj)


def consistency_faults(rows, names, prompts_by_n: dict[int, dict],
                       characters: dict | None = None) -> list[str]:
    """Faults naming the asset: the prompt must keep the approved casting and place."""
    faults = []
    for n, (r, nm) in enumerate(zip(rows, names), 1):
        p = (prompts_by_n.get(n) or {}).get("prompt", "")
        if not p:
            faults.append(f"{nm}: no prompt was written for row {n}.")
            continue
        low = p.lower()
        miss = []
        for w in r.get("who", []):
            if w.lower() in low:
                continue
            groups = required_words(w, (characters or {}).get(w))
            if not all(any(re.search(r"\b" + re.escape(x) + r"s?\b", low) for x in g)
                       for g in groups):
                miss.append(w)
        if miss:
            faults.append(f"{nm}: the approved storyboard has {', '.join(miss)} in this image; "
                          f"the prompt must name them.")
        place = [w for w in re.findall(r"[a-z]{4,}", (r.get("where") or "").lower())]
        if place and not any(w in low for w in place):
            faults.append(f"{nm}: the approved place ({r['where']}) is not shown in the prompt.")
    return faults


# ---- reference maps: who -> character refs, where -> background ref ---------------------

MIN_PLACE_ROWS = 6        # a place shown this often is offered as a background reference
MIN_OBJECT_ROWS = 3       # an object shown in this many rows (or more) needs a reference
_ROLE_WORDS = {"parents": ("FATHER", "MOTHER"), "parent": ("PARENT",)}


def _slug(text: str) -> str:
    text = re.sub(r"['\u2019]s\b", "", str(text))          # frieda's kitchen -> FRIEDA_KITCHEN
    return re.sub(r"[^A-Z0-9]+", "_", text.upper()).strip("_")


def default_character_refs(name: str, known=()) -> list[str]:
    """A first guess at the reference(s) of a character name, for the person to correct.
    Exact first-name matches keep their supplied ref; 'X's parents' becomes two refs."""
    known = set(known or ())
    n = " ".join(str(name).split())
    if not n:
        return []
    exact = f"CH_{_slug(n)}"
    if exact in known:
        return [exact]
    m = re.match(r"(?i)^(\w+)'s\s+(.+)$", n)
    if m:
        owner, role = m.group(1), m.group(2)
        words = [_slug(w) for w in role.split() if w]
        if words and words[-1].lower() in _ROLE_WORDS:
            lead = "_".join(words[:-1])
            return [f"CH_{_slug(owner)}_" + (lead + "_" if lead else "") + x
                    for x in _ROLE_WORDS[words[-1].lower()]]
        return [f"CH_{_slug(owner)}_{'_'.join(words)}"]
    return [exact]


def sync_characters(rows, current=None, known=()) -> dict:
    """{who-name: [CH_ref, ...]} for every name in the rows. Entries the person already
    set are kept as they are (an empty list means: no reference for that character)."""
    cur = {k: list(v) for k, v in (current or {}).items() if isinstance(v, (list, tuple))}
    out: dict = {}
    for r in rows:
        for w in r.get("who") or []:
            if w not in out:
                out[w] = cur[w] if w in cur else default_character_refs(w, known)
    return out


def sync_locations(rows, current=None) -> dict:
    """{where: BG_id or ""} for every place in the rows. A place shown in at least
    MIN_PLACE_ROWS rows gets a suggested id; the person edits, merges (same id for two
    spellings) or clears it."""
    cur = dict(current or {})
    count: dict = {}
    for r in rows:
        w = (r.get("where") or "").strip()
        if w:
            count[w] = count.get(w, 0) + 1
    out: dict = {}
    for w, n in count.items():
        if w in cur:
            out[w] = str(cur[w] or "")
        else:
            out[w] = f"BG_{_slug(w)}" if n >= MIN_PLACE_ROWS else ""
    return out


def sync_objects(rows, current=None) -> dict:
    """{object name: OBJ_id or ""} for every object listed in the rows. An object shown
    in MIN_OBJECT_ROWS or more rows gets an OBJ_ reference (so it looks the same each
    time); fewer rows get none unless the person set one. The person edits, merges or
    clears entries; what they set is kept."""
    cur = dict(current or {})
    count: dict = {}
    for r in rows:
        for o in r.get("objects") or []:
            count[o] = count.get(o, 0) + 1
    out: dict = {}
    for o, n in count.items():
        if o in cur:
            out[o] = str(cur[o] or "")
        elif n >= MIN_OBJECT_ROWS:
            slug = _slug(o)
            out[o] = slug if slug.startswith("OBJ_") else f"OBJ_{slug}"
        else:
            out[o] = ""
    return out


def apply_object_map(form: dict):
    """{object name: OBJ_id} from the Review page form, or None when the form has no
    objects table."""
    names = [k for k in form if re.fullmatch(r"obj_name_\d+", k)]
    if not names:
        return None
    out = {}
    for k in sorted(names, key=lambda x: int(x.rsplit("_", 1)[1])):
        i = k.rsplit("_", 1)[1]
        ref = str(form.get(f"obj_id_{i}", "")).strip()
        if ref:
            slug = _slug(ref)
            ref = slug if slug.startswith("OBJ_") else f"OBJ_{slug}"
        out[str(form[k])] = ref
    return out


def apply_maps(form: dict):
    """(characters, locations) from the Review page form, or (None, None) when the form
    carries neither table. Names are tidied into CH_ / BG_ form."""
    chars, locs = None, None
    names = [k for k in form if re.fullmatch(r"char_name_\d+", k)]
    if names:
        chars = {}
        for k in sorted(names, key=lambda x: int(x.rsplit("_", 1)[1])):
            i = k.rsplit("_", 1)[1]
            refs = []
            for part in str(form.get(f"char_refs_{i}", "")).split(","):
                part = part.strip()
                if not part:
                    continue
                slug = _slug(part)
                ref = slug if slug.startswith(("CH_", "OBJ_")) else f"CH_{slug}"
                if ref not in refs:
                    refs.append(ref)
            chars[str(form[k])] = refs
    places = [k for k in form if re.fullmatch(r"loc_where_\d+", k)]
    if places:
        locs = {}
        for k in places:
            i = k.rsplit("_", 1)[1]
            bg = str(form.get(f"loc_id_{i}", "")).strip()
            if bg:
                slug = _slug(bg)
                bg = slug if slug.startswith("BG_") else f"BG_{slug}"
            locs[str(form[k])] = bg
    return chars, locs


def expected_refs(row: dict, characters: dict, locations: dict, objects: dict | None = None) -> list[str]:
    """The reference names the row's image must carry: its people, its place, then its
    recurring objects."""
    out: list[str] = []
    for w in row.get("who") or []:
        for ref in (characters or {}).get(w) or []:
            if ref and ref not in out:
                out.append(ref)
    bg = ((locations or {}).get((row.get("where") or "").strip()) or "").strip()
    if bg and bg not in out:
        out.append(bg)
    for o in row.get("objects") or []:
        ref = ((objects or {}).get(o) or "").strip()
        if ref and ref not in out:
            out.append(ref)
    return out


_SYNONYMS = {"mother": ("mom", "mum", "mama"), "father": ("dad", "papa"),
             "parents": ("mother", "father", "mom", "dad")}


def required_words(name: str, refs=None) -> list[set]:
    """What the prompt must contain to show this character: a list of word groups, EVERY
    group needing one of its words. A plain name needs the name; "Joe's mother" needs the
    role (mother / mom), not "Joe"; "Joe's parents" needs both a mother and a father (or
    the word parents); "clinic receptionist" needs receptionist, not "clinic"."""
    n = " ".join(str(name).split())
    m = re.match(r"(?i)^(\w+)['\u2019]s\s+(.+)$", n)
    words = re.findall(r"[a-z]+", (m.group(2) if m else n).lower())
    if not words:
        return [set()]
    head = words[-1]
    if head == "parents":
        have = {x.lower() for ref in (refs or []) for x in str(ref)[3:].split("_")}
        both = {"father", "mother"} <= have or not have & {"father", "mother"}
        if both:
            return [{"father", "dad", "parents", "parent"}, {"mother", "mom", "mum", "parents", "parent"}]
        return [{"parents", "parent"} | (have & {"father", "mother"})
                | {s for x in have & {"father", "mother"} for s in _SYNONYMS.get(x, ())}]
    group = {head, head.rstrip("s")} | set(_SYNONYMS.get(head, ()))
    return [group]


# ---- file -----------------------------------------------------------------------------

def path(pdir) -> Path:
    return Path(pdir) / FILE


def load(pdir) -> dict | None:
    p = path(pdir)
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return d if isinstance(d, dict) and isinstance(d.get("rows"), list) else None


def set_prompt_faults(pdir, faults) -> None:
    """Remember the problems of the last prompt run so the Review page can show which
    shot each belongs to. An empty list clears them."""
    p = path(pdir)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(d, dict):
        return
    d["prompt_faults"] = [str(f) for f in faults]
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def prompt_faults_by_id(faults) -> tuple[dict, list]:
    """({shot id: [problem text]}, [problems without a shot id]) from the stored faults.
    A list fault ("14 images ...: S01_08_CU_ZI.png: CH_X; S02_01_SCN_ZO.png: CH_Y") is
    split so every shot gets its own line, headed by what the fault says."""
    id_re = re.compile(r"\bS\d{2}_\d{2}(?!\d)")
    by_id: dict = {}
    general: list = []
    for f in faults or []:
        f = str(f)
        first = id_re.search(f)
        if not first:
            general.append(f)
            continue
        header = f[:first.start()].strip().rstrip(":").strip()
        for seg in f[first.start():].split("; "):
            m = id_re.match(seg.strip())
            if not m:
                continue
            detail = re.sub(r"^\S+?:\s*", "", seg.strip(), count=1) if ":" in seg else ""
            text = " - ".join(x for x in (header[:110], detail) if x) or seg
            by_id.setdefault(m.group(0), []).append(text)
    return by_id, general


def save(pdir, rows, ranges, style="", faults=(), warnings=(), approved=False,
         compiled=None, characters=None, locations=None, objects=None,
         compiled_refs=None) -> dict:
    """characters / locations: the reviewed reference maps. None keeps what the file
    already has; the maps are filled for any new who/where either way."""
    prev = load(pdir) or {}
    if characters is None:
        characters = prev.get("characters")
    if locations is None:
        locations = prev.get("locations")
    if objects is None:
        objects = prev.get("objects")
    if compiled_refs is None:
        compiled_refs = prev.get("compiled_refs")
    characters = sync_characters(rows, characters)
    locations = sync_locations(rows, locations)
    objects = sync_objects(rows, objects)
    out_rows = []
    for r, (a, z) in zip(rows, ranges):
        d = dict(r)
        d["cues"] = (f"{a}" if a == z else f"{a}-{z}") if a else ""
        out_rows.append(d)
    data = {"style": style, "approved": bool(approved), "rows": out_rows,
            "faults": list(faults), "warnings": list(warnings),
            "compiled": dict(compiled or {}),
            "characters": characters, "locations": locations, "objects": objects,
            "compiled_refs": dict(compiled_refs or {}),
            "prompt_faults": list(prev.get("prompt_faults") or [])}
    path(pdir).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                          encoding="utf-8")
    return data


def apply_edits(rows: list[dict], form: dict) -> list[dict]:
    """New rows from edited cells. `form` maps "<field>_<n>" (n from 1) to text.
    Fields the form does not carry keep their value; a row never changes its
    optional shot fields (overlays, ai_motion...)."""
    out = []
    for n, r in enumerate(rows, 1):
        d = dict(r)
        for f in EDITABLE:
            key = f"{f}_{n}"
            if key not in form:
                continue
            v = form[key]
            if f in ("who", "objects"):
                d[f] = _names(v)
            elif f == "status":
                d["status"] = clean_status(v)
            elif f == "scene":
                try:
                    d["scene"] = max(1, int(v))
                except (TypeError, ValueError):
                    pass
            elif f in ("type", "motion"):
                d[f] = str(v or "").strip().upper()
            else:
                d[f] = re.sub(r"\s+", " ", str(v or "")).strip()
        if d.get("type") not in beatsmod.TYPE_CODES:
            d["type"] = "SCN"
        out.append(d)
    return out


def out_of_date(sb: dict) -> list[str]:
    """Assets whose row, or whose attached references (the reviewed maps), changed after
    their prompt was written. Storyboards written before references were recorded only
    compare the row."""
    compiled = sb.get("compiled") or {}
    recorded = sb.get("compiled_refs") or {}
    chars, locs, objs = sb.get("characters"), sb.get("locations"), sb.get("objects")
    out = []
    for r in sb.get("rows", []):
        a = r.get("asset")
        if a not in compiled:
            continue
        if compiled[a] != row_hash(r):
            out.append(a)
        elif a in recorded and list(recorded[a]) != expected_refs(r, chars, locs, objs):
            out.append(a)
    return out


# ---- cut-off replies --------------------------------------------------------------------

def join_continuation(body: str, chunk: str, key: str, ident) -> str:
    """Join a 'continue' reply onto a cut-off JSON reply whose list is `key`.
    `ident(item)` is a hashable id of an entry (starts_with words, or n); an entry
    the writer repeats is kept once. Works like beats.join_continuation."""
    decoder = json.JSONDecoder()

    def parts(text):
        match = re.search(r'"' + re.escape(key) + r'"\s*:\s*\[', text)
        start = match.end() if match else 0
        prefix = text[:start] if match else ""
        items, pos = [], start
        while pos < len(text):
            while pos < len(text) and (text[pos].isspace() or text[pos] == ","):
                pos += 1
            if pos == len(text) or text[pos] == "]":
                return prefix, items, pos, pos < len(text)
            try:
                item, end = decoder.raw_decode(text, pos)
            except ValueError:
                return prefix, items, pos, False
            if not isinstance(item, dict):
                return prefix, items, pos, False
            items.append(item)
            pos = end
        return prefix, items, pos, False

    prefix, old, _, _ = parts(body)
    _, new, tail, closed = parts(chunk)
    if not prefix or not new:
        return body + "\n" + chunk
    anchor = ident(new[0])
    for i in range(len(old) - 1, -1, -1):
        if ident(old[i]) == anchor:
            old = old[:i]
            break
    merged = prefix + "\n" + ",\n".join(json.dumps(b, ensure_ascii=False) for b in old + new)
    if closed:
        return merged + chunk[tail:]
    return merged + ",\n" + chunk[tail:]


def row_ident(item) -> tuple:
    return tuple(beatsmod.tokens(str(item.get("starts_with", ""))))


def prompt_ident(item):
    return str(item.get("n"))
