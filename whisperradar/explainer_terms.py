"""Key terms: a name the narrator says (NCM, LLM, VRAM...) must be written on screen where it is said.

A video about NCM that never shows the letters N-C-M, or what they stand for, loses the viewer at
exactly the word the video is about. This module finds the terms that matter in the narration,
checks that the scene covering each first mention shows it, and can add it when the writer did not.

A term matters when it is in the production title, is defined ("X stands for ...", "known as X"),
or is said three or more times. Everyday acronyms (AI, US, OK...) are left out.
"""
from __future__ import annotations

import re

STOP = {"AI", "US", "UK", "EU", "OK", "TV", "PC", "IT", "CEO", "FAQ", "USA", "AM", "PM", "ID", "VS", "II", "III"}
_ACR = re.compile(r"\b([A-Z][A-Z0-9]{1,6})(s?)\b(?!\s?-\s?\d)")      # not "LM -1B"
_SPACED = re.compile(r"\b((?:[A-Z][ .\-]){2,5}[A-Z])\b")             # "N C M", "N-C-M"
_DEFINE = [
    re.compile(r"\b([A-Z][A-Z0-9]{1,6})\s+(?:stands for|is short for|means|is an acronym for)\s+([A-Z][\w\-]*(?:[ \-][A-Za-z][\w\-]*){0,5})"),
    re.compile(r"\b(?:known as|called|abbreviated as|short for)\s+([A-Z][A-Z0-9]{1,6})\b(?!\s?-\s?\d)()"),
]


def _tokens(text: str) -> list:
    return re.findall(r"[a-z0-9]+", str(text).lower())


def _norm_term(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", raw).upper()


def key_terms(cues: list, title: str = "") -> list:
    """[{term, first, cues, expansion, expansion_cue}] for the narration's key terms. cues are the
    parsed SRT cues ({index, text, ...})."""
    texts = [(int(c["index"]), " ".join(str(c["text"]).split())) for c in cues]
    joined, spans, pos = "", [], 0
    for idx, t in texts:
        spans.append((pos, pos + len(t), idx))
        joined += t + " "
        pos += len(t) + 1

    def cue_at(p):
        for a, b, idx in spans:
            if a <= p < b + 1:
                return idx
        return spans[-1][2] if spans else 0

    found: dict = {}

    def note(term, p):
        d = found.setdefault(term, {"term": term, "cues": [], "expansion": "", "expansion_cue": 0, "defined": False})
        c = cue_at(p)
        if c not in d["cues"]:
            d["cues"].append(c)
        d.setdefault("count", 0)
        d["count"] += 1
        return d

    for m in _ACR.finditer(joined):
        word, plural = m.group(1), m.group(2)
        if word in STOP or word.isdigit() or (plural and len(word) < 2):
            continue
        note(word, m.start())
    for m in _SPACED.finditer(joined):
        term = _norm_term(m.group(1))
        if term not in STOP and len(term) >= 2:
            note(term, m.start())
    for rx in _DEFINE:
        for m in rx.finditer(joined):
            term = _norm_term(m.group(1))
            if term in STOP:
                continue
            d = note(term, m.start())
            d["defined"] = True
            exp = (m.group(2) or "").strip(" .,;")
            if exp and not d["expansion"]:
                d["expansion"] = exp
                d["expansion_cue"] = cue_at(m.start(1) + len(m.group(1)) + 1)
    title_terms = {_norm_term(m.group(1)) for m in _ACR.finditer(title or "")}
    out = []
    for d in found.values():
        if d["term"] in title_terms or d["defined"] or d["count"] >= 3:
            d["cues"].sort()
            d["first"] = d["cues"][0]
            out.append(d)
    out.sort(key=lambda d: d["first"])
    return out


def scene_text(scene: dict) -> str:
    """Everything a scene writes on screen, as one string."""
    parts = [scene.get("title", "")]
    for it in scene.get("items") or []:
        if isinstance(it, dict):
            parts += [it.get("label", ""), it.get("note", "")]
    for sd in scene.get("sides") or []:
        if isinstance(sd, dict):
            parts.append(sd.get("header", ""))
            parts += [p.get("text", "") for p in sd.get("points") or [] if isinstance(p, dict)]
    if isinstance(scene.get("footer"), dict):
        parts.append(scene["footer"].get("text", ""))
    if isinstance(scene.get("counter"), dict):
        parts.append(scene["counter"].get("label", ""))
    return " ".join(str(p) for p in parts if p)


def shows(text: str, term: str, expansion: bool = False) -> bool:
    toks = _tokens(text)
    if expansion:
        want = _tokens(term)
        return bool(want) and any(toks[i:i + len(want)] == want for i in range(len(toks) - len(want) + 1))
    want = term.lower()
    return any(t == want or (t.endswith("s") and t[:-1] == want) for t in toks) \
        or want in "".join(toks) and len(want) >= 4


def scene_for(resolved_scenes: list, cue: int):
    """The scene covering a cue (a gap belongs to the scene before it)."""
    best = None
    for s in resolved_scenes:
        a, b = s["cue_range"]
        if a <= cue <= b:
            return s
        if a <= cue and (best is None or a > best["cue_range"][0]):
            best = s
    return best


def missing(resolved: dict, terms: list) -> list:
    """[{term, scene, cue, expansion, kind}] - what is said but not shown where it is said."""
    out = []
    scenes = resolved.get("scenes") or []
    for t in terms:
        s = scene_for(scenes, t["first"])
        if s is not None and not shows(scene_text(s), t["term"]):
            out.append({"term": t["term"], "scene": s, "cue": t["first"], "expansion": t["expansion"], "kind": "term"})
        if t["expansion"] and t["expansion_cue"]:
            s2 = scene_for(scenes, t["expansion_cue"])
            if s2 is not None and not shows(scene_text(s2), t["expansion"], expansion=True):
                out.append({"term": t["expansion"], "scene": s2, "cue": t["expansion_cue"],
                            "expansion": "", "kind": "expansion", "of": t["term"]})
    return out


def fault_text(m: dict, mmss: str) -> str:
    sid = m["scene"].get("id")
    if m["kind"] == "expansion":
        return (f"scene {sid}: the narrator spells out '{m['term']}' (what {m['of']} stands for, {mmss}) "
                "but it is never shown on screen - write it in this scene's title, a label or the footer")
    more = f" (it stands for {m['expansion']})" if m["expansion"] else ""
    return (f"scene {sid}: the narrator says '{m['term']}'{more} at {mmss} but it is never shown on screen - "
            "write it in this scene's title, a label or the footer")


def insert_missing(plan: dict, resolved: dict, gaps: list) -> tuple:
    """(plan, notes): add each missing term to the plan's scene - as the footer when it has none,
    else as the title when it has none. A scene that has both is left for the writer."""
    notes = []
    by_id = {str(s.get("id")): s for s in plan.get("scenes") or [] if isinstance(s, dict)}
    for g in gaps:
        s = by_id.get(str(g["scene"].get("id")))
        if s is None:
            continue
        text = g["term"] if g["kind"] == "expansion" else (
            f"{g['term']}: {g['expansion']}" if g["expansion"] else g["term"])
        if g["kind"] == "expansion":
            text = f"{g['of']}: {g['term']}"
        if not s.get("footer") and not g.get("no_footer"):
            s["footer"] = {"text": text, "cue": int(g["cue"]), "key": True}
        elif not s.get("title"):
            s["title"] = text
        elif g["kind"] == "expansion" and len(s["title"]) + len(g["term"]) + 3 <= 54:
            # the title already names the term: spell it out in the title ("NCM (Next Concept Modeling): ...")
            acr = g.get("of", "")
            if acr and re.search(rf"\b{re.escape(acr)}\b", s["title"]):
                s["title"] = re.sub(rf"\b{re.escape(acr)}\b", f"{acr} ({g['term']})", s["title"], count=1)
            else:
                s["title"] = f"{s['title']} ({g['term']})"
            text = s["title"]
        else:
            continue
        notes.append(f"scene {s.get('id')}: wrote '{text}' on screen (the narrator says it and it was not shown)")
    return plan, notes
