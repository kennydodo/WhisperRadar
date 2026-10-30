"""Scene recipes: a short, meaning-level plan -> a full explainer.json.

A script generator (or a person) writes `plan.json`: per scene a layout name,
the items to show and the cue each one belongs to. This module places
everything (no coordinates in the plan), picks colours, schedules the pen so
nothing overlaps or runs past the scene, and returns the explainer.json that
`validate` and the renderer already understand. Same plan + same subtitles =
same file.

plan.json
{
  "theme": "chalkboard",                       (optional; the channel's setting otherwise)
  "scenes": [{
     "id": "s1", "cue_range": [1, 4],
     "title": "Start early, win later",         (optional, drawn first)
     "layout": "row" | "steps" | "timeline" | "compare" | "stack" | "hero"
               | "memory" | "precision" | "offload"   (see mechanisms.py),
     "items": [{"label": "Age 25", "icon": "piggy-bank", "color": "pink",
                "cue": 2, "note": "$1,000 a year"}],
     "connect": "arrow" | "dashed" | "none",    (row/steps/stack, default arrow)
     "cards": false, "focus": false,            (focus = dim the previous item)
     "footer": {"text": "Time does the work", "cue": 4, "highlight": true}
  }]
}
compare takes "sides": [{"header", "icon", "color", "cue", "points": [str | {"text","mark","cue"}]}, x2]
"""
from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass, field

from . import mechanisms
from . import schema as sc
from . import strokes_library
from .validate import ExplainerError

LAYOUTS = ("row", "steps", "timeline", "compare", "stack", "hero", "memory", "precision", "offload")
CYCLE = ("yellow", "green", "blue", "pink", "orange", "purple")
Y_BOTTOM = 0.83          # nothing below this: the subtitles live there
GAP_MS = 70
TAIL_MS = 1000           # every label must finish and stay readable this long before the scene ends
WN = 9.0 / 16.0          # normalized width of a square per unit of normalized height


@dataclass
class El:
    obj: dict
    cue: int
    dur: int
    min: int
    group: str = ""                  # items dim as a group
    dim_prev: bool = False
    extra: list = field(default_factory=list)


# ---- element makers -------------------------------------------------------------

def _compact(text: str) -> str:
    return re.sub(r"\s", "", text)


def _text(oid, text, box, color, cue, group="", font=None, slow=1.0):
    n = max(1, len(_compact(text)))
    obj = {"id": oid, "type": "handwriting", "text": text, "box": _r(box), "color": color}
    if font:
        obj["font"] = font
    return El(obj, cue, int((n * 80 + 220) * slow), n * 66 + 80, group)


def _icon(oid, asset, box, color, cue, group="", sid=""):
    try:
        n = len(strokes_library.load(asset).strokes)
    except ValueError as exc:
        raise ExplainerError([f"{sid}: icon {asset!r}: {exc}"]) from None
    obj = {"id": oid, "type": "stroke_asset", "asset": asset, "box": _r(box), "color": color}
    return El(obj, cue, n * 150 + 300, n * 125 + 40, group)


def _shape(oid, kind, cue, color, group="", dur=600, strokes=1, **kw):
    obj = {"id": oid, "type": "shape", "kind": kind, "color": color}
    obj.update(kw)
    return El(obj, cue, dur, strokes * 125 + 40, group)


def _r(box):
    return [round(float(v), 4) for v in box]


def _asset(name: str) -> str:
    return name if ":" in name else "lucide:" + name


def _lines(label: str, cap=16):
    """One line, or two when it is long and has a space."""
    if len(label) <= cap or " " not in label:
        return label, 1
    words, best = label.split(), None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        score = abs(len(a) - len(b))
        if best is None or score < best[0]:
            best = (score, a + "\n" + b)
    return best[1], 2


def _color(item, i, key="color"):
    return item.get(key) or CYCLE[i % len(CYCLE)]


# ---- layouts --------------------------------------------------------------------

def _title(s, sid, els):
    t = s.get("title")
    if not t:
        return 0.27
    cue = s["cue_range"][0]
    els.append(_text(f"{sid}_title", t, [0.07, 0.06, 0.86, 0.12], "heading", cue,
                     group="title"))
    w = min(0.86, len(t) * 0.027 + 0.02)
    els.append(_shape(f"{sid}_tline", "underline", cue, "orange", "title",
                      box=[0.07, 0.06, w, 0.125], dur=450, fit_to=f"{sid}_title"))
    return 0.27


def _item_cue(s, items, i):
    c0, c1 = s["cue_range"]
    given = items[i].get("cue")
    if given is not None:
        return int(given)
    span = c1 - c0 + 1
    return c0 + (i * span) // max(1, len(items))


def _row(s, sid, els, top, numbered=False, timeline=False):
    items = s["items"]
    n = len(items)
    x0, x1 = 0.07, 0.93
    cw = (x1 - x0) / n
    ih = min(0.24, cw * 0.58 / WN)
    iw = ih * WN
    two = any(_lines(i.get("label", ""))[1] == 2 for i in items)
    lh = 0.15 if two else 0.095
    note_h = 0.07 if any(i.get("note") for i in items) else 0
    block = ih + (0.115 if timeline else 0.03) + lh + (0.01 + note_h if note_h else 0)   # timeline: line, nodes, then label
    avail_top, avail_bot = top, s["_bot"]
    y_icon = avail_top + max(0.0, (avail_bot - avail_top - block) / 2)
    if timeline:
        y_line = y_icon + ih + 0.075
        y_icon = y_line - 0.075 - ih
    connect = s.get("connect", "arrow")
    if timeline:
        els.append(_shape(f"{sid}_line", "line", s["cue_range"][0], "grey", "frame",
                          dur=900, points=[[x0, y_line], [x1, y_line]]))
    for i, it in enumerate(items):
        cx = x0 + cw * (i + 0.5)
        cue = _item_cue(s, items, i)
        col = _color(it, i)
        g = f"i{i}"
        if s.get("cards"):
            els.append(_shape(f"{sid}_card{i}", "card", cue, col, g, dur=800, filled=True,
                              box=[cx - cw * 0.46, y_icon - 0.04, cw * 0.92, block + 0.07]))
        yy = y_icon
        if numbered:
            els.append(_shape(f"{sid}_num{i}", "oval", cue, col, g, dur=450,
                              box=[cx - 0.025 * 0.5625 * 2, yy - 0.075, 0.056, 0.07]))
            els.append(_text(f"{sid}_numt{i}", str(i + 1), [cx - 0.012, yy - 0.072, 0.03, 0.064],
                             col, cue, g))
        if it.get("icon"):
            els.append(_icon(f"{sid}_ic{i}", _asset(it["icon"]),
                             [cx - iw / 2, yy, iw, ih], col, cue, g, sid))
        if timeline:
            els.append(_shape(f"{sid}_node{i}", "oval", cue, col, g, dur=450,
                              box=[cx - 0.026, y_line - 0.035, 0.052, 0.07]))
        label, nl = _lines(it.get("label", ""), 16 if cw > 0.2 else 12)
        if label:
            ly = (y_line + 0.04) if timeline else yy + ih + 0.03
            els.append(_text(f"{sid}_lb{i}", label, [cx - cw * 0.46, ly, cw * 0.92,
                                                      0.15 if nl == 2 else 0.095],
                             "text", cue, g))
        if it.get("note"):
            ny = ((y_line + 0.04) if timeline else yy + ih + 0.03) + (0.15 if nl == 2 else 0.095) + 0.01
            els.append(_text(f"{sid}_nt{i}", it["note"], [cx - cw * 0.46, ny, cw * 0.92, 0.065],
                             "grey", cue, g))
        if i and connect != "none" and not timeline:
            px = cx - cw
            ym = yy + ih / 2
            gap0 = px + iw / 2 + 0.012
            gap1 = cx - iw / 2 - 0.012
            if gap1 - gap0 > 0.03:
                els.append(_shape(f"{sid}_cn{i}", "line" if connect == "dashed" else "arrow",
                                  cue, "grey", g, dur=550, strokes=1 if connect == "dashed" else 2,
                                  points=[[gap0, ym], [gap1, ym]], dashed=connect == "dashed",
                                  **({} if connect == "dashed" else {"curve": 0.06})))
    return els


def _stack(s, sid, els, top):
    items = s["items"]
    n = len(items)
    rh = min(0.17, (s["_bot"] - top) / n)
    ih = rh * 0.78
    iw = ih * WN
    x_ic = 0.14
    connect = s.get("connect", "arrow")
    for i, it in enumerate(items):
        y = top + i * rh
        cue = _item_cue(s, items, i)
        col = _color(it, i)
        g = f"i{i}"
        if it.get("icon"):
            els.append(_icon(f"{sid}_ic{i}", _asset(it["icon"]), [x_ic, y, iw, ih], col, cue, g, sid))
        label = it.get("label", "")
        tx = x_ic + iw + 0.04
        lh = ih * 0.5 if it.get("note") else ih * 0.8
        ly = y + ih * 0.02 if it.get("note") else y + (ih - lh) / 2
        els.append(_text(f"{sid}_lb{i}", label, [tx, ly, 0.9 - tx, lh], "text", cue, g))
        if it.get("note"):
            els.append(_text(f"{sid}_nt{i}", it["note"], [tx, y + ih * 0.55, 0.9 - tx, ih * 0.38],
                             "grey", cue, g))
        if i and connect != "none":
            yy0 = top + (i - 1) * rh + ih + 0.008
            yy1 = y - 0.008
            if yy1 - yy0 > 0.025:
                els.append(_shape(f"{sid}_cn{i}", "arrow", cue, "grey", g, dur=500, strokes=2,
                                  points=[[x_ic + iw / 2, yy0], [x_ic + iw / 2, yy1]], curve=0.05))
    return els


def _hero(s, sid, els, top):
    it = s["items"][0]
    cue = _item_cue(s, s["items"], 0)
    col = _color(it, 0)
    y = top + (0.06 if not s.get("title") else 0.0)
    label, nl = _lines(it.get("label", ""), 26)
    below = (0.2 if nl == 2 else 0.12) + (0.03 + (0.07 if it.get("note") else 0.0))
    ih = max(0.14, min(0.34, s["_bot"] - y - below - 0.03))      # the icon gives way so the words clear the footer
    iw = ih * WN
    if it.get("icon"):
        els.append(_icon(f"{sid}_ic0", _asset(it["icon"]), [0.5 - iw / 2, y, iw, ih], col, cue, "i0", sid))
    els.append(_text(f"{sid}_lb0", label, [0.08, y + ih + 0.03, 0.84, 0.2 if nl == 2 else 0.12],
                     "text", cue, "i0"))
    if it.get("note"):
        els.append(_text(f"{sid}_nt0", it["note"], [0.1, y + ih + 0.03 + (0.14 if nl == 2 else 0.11), 0.8, 0.07],
                         "grey", cue, "i0"))
    return els


def _compare(s, sid, els, top):
    sides = s.get("sides") or []
    if len(sides) != 2:
        raise ExplainerError([f"Scene {s['id']}: compare needs exactly two sides."])
    if not s.get("title"):
        top += 0.12                      # no title: sit nearer the middle
    for k, side in enumerate(sides):
        x = 0.06 + k * 0.46
        col = side.get("color") or ("green", "red")[k]
        cue = int(side.get("cue") or s["cue_range"][0])
        g = f"i{k}"
        npts = len(side.get("points") or [])
        card_h = min(s["_bot"] - top + 0.02, 0.2 + npts * 0.14 + 0.03)
        els.append(_shape(f"{sid}_card{k}", "card", cue, col, g, dur=900, filled=True,
                          box=[x, top - 0.02, 0.42, card_h]))
        hx = x + 0.025
        if side.get("icon"):
            els.append(_icon(f"{sid}_ic{k}", _asset(side["icon"]), [hx, top + 0.015, 0.1 * WN * 1.0, 0.1],
                             col, cue, g, sid))
            hx += 0.1 * WN + 0.02
        els.append(_text(f"{sid}_hd{k}", side.get("header", ""), [hx, top + 0.005, x + 0.4 - hx, 0.12],
                         col, cue, g))
        pts = side.get("points") or []
        py = top + 0.17
        step = min(0.14, (s["_bot"] - 0.02 - py) / max(1, len(pts)))
        for j, p in enumerate(pts):
            p = {"text": p} if isinstance(p, str) else dict(p)
            pcue = int(p.get("cue") or cue)
            mark = p.get("mark", "check" if k == 0 else "cross")
            if mark in ("check", "cross"):
                els.append(_shape(f"{sid}_mk{k}_{j}", mark, pcue, "green" if mark == "check" else "red",
                                  f"p{k}{j}", dur=400, strokes=1 if mark == "check" else 2,
                                  box=[x + 0.025, py + 0.02, 0.028, 0.06]))
            els.append(_text(f"{sid}_pt{k}_{j}", p.get("text", ""), [x + 0.07, py, 0.335, step * 0.82],
                             "text", pcue, f"p{k}{j}"))
            py += step
    return els


def _footer(s, sid, els):
    f = s.get("footer")
    if not f:
        return
    if isinstance(f, str):
        f = {"text": f}
    if not isinstance(f, dict) or not str(f.get("text") or "").strip():
        raise ExplainerError([f"Scene {sid}: footer must be {{\"text\": \"...\", \"at\": \"narration words\"}}."])
    cue = int(f.get("cue") or s["cue_range"][1])
    if f.get("highlight", True):
        els.append(_shape(f"{sid}_fhl", "highlight", cue, "yellow", "footer", dur=450,
                          box=[0.08, 0.76, min(0.84, len(f["text"]) * 0.028 + 0.06), 0.075],
                          fit_to=f"{sid}_ft"))
    els.append(_text(f"{sid}_ft", f["text"], [0.1, 0.76, 0.8, 0.075], "text", cue, "footer"))


def _scene_elements(s, idx):
    sid = re.sub(r"[^A-Za-z0-9_\-]", "", str(s.get("id") or f"s{idx + 1}")) or f"s{idx + 1}"
    if not sid[0].isalpha():
        sid = "s" + sid
    s = dict(s, id=sid)
    s["_bot"] = 0.735 if s.get("footer") else Y_BOTTOM        # room for the footer
    cr = s.get("cue_range")
    if not (isinstance(cr, list) and len(cr) == 2 and cr[0] >= 1 and cr[1] >= cr[0]):
        raise ExplainerError([f"Scene {sid}: cue_range must be [first, last] (1-based)."])
    layout = s.get("layout", "row")
    if layout not in LAYOUTS:
        raise ExplainerError([f"Scene {sid}: layout must be one of {', '.join(LAYOUTS)} (got {layout!r})."])
    items = s.get("items") or []
    if layout != "compare" and not items:
        raise ExplainerError([f"Scene {sid}: {layout} needs items."])
    if layout in ("row", "steps", "timeline") and not (1 <= len(items) <= 5 if layout != "timeline" else 2 <= len(items) <= 6):
        raise ExplainerError([f"Scene {sid}: {layout} fits "
                              f"{'2-6' if layout == 'timeline' else '1-5'} items (got {len(items)}); "
                              "split it across two scenes."])
    if layout == "stack" and len(items) > 5:
        raise ExplainerError([f"Scene {sid}: stack fits up to 5 items (got {len(items)})."])
    els: list = []
    top = _title(s, sid, els)
    if not s.get("title") and layout != "hero":
        top = 0.14
    if layout == "row":
        _row(s, sid, els, top)
    elif layout == "steps":
        _row(s, sid, els, top + 0.05, numbered=True)
    elif layout == "timeline":
        _row(s, sid, els, top, timeline=True)
    elif layout == "stack":
        _stack(s, sid, els, top)
    elif layout == "hero":
        _hero(s, sid, els, top if s.get("title") else 0.12)
    elif layout in mechanisms.BUILDERS:
        mechanisms.BUILDERS[layout](sys.modules[__name__], s, sid, els, top)
    else:
        _compare(s, sid, els, top)
    _footer(s, sid, els)
    for el in els:
        if not (s["cue_range"][0] <= el.cue <= s["cue_range"][1]):
            raise ExplainerError([f"Scene {sid}: object {el.obj['id']} is anchored to cue {el.cue}, "
                                  f"outside the scene's cues {s['cue_range']}."])
    if s.get("focus"):
        seen = set()
        for el in els:
            if el.group.startswith("i") and el.group not in seen:
                seen.add(el.group)
                el.dim_prev = True
    return s, els


# ---- scheduling -------------------------------------------------------------------

def _simulate(els, f, cue_ms, start, end):
    t_pen, out = start, []
    for el in els:
        d = max(el.min, int(el.dur * f))
        t0 = max(t_pen, cue_ms[el.cue][0])
        out.append((t0, d))
        t_pen = t0 + d + GAP_MS
    return out, t_pen - GAP_MS <= end - TAIL_MS


def _schedule(s, els, cue_ms):
    c0, c1 = s["cue_range"]
    for c in (c0, c1):
        if c not in cue_ms:
            raise ExplainerError([f"Scene {s['id']}: cue {c} is not in the subtitles."])
    start, end = cue_ms[c0][0], cue_ms[c1][1]
    plan, ok = _simulate(els, 1.0, cue_ms, start, end)
    if not ok:
        lo, hi = 0.0, 1.0
        plan, ok = _simulate(els, 0.0, cue_ms, start, end)
        if not ok:
            last = plan[-1][0] + plan[-1][1]
            over = (last - (end - TAIL_MS)) / 1000
            raise ExplainerError([
                f"Scene {s['id']}: too much for the time - even at the fastest pen speed "
                f"the last object is {over:.1f}s too late: every label must be finished "
                f"{TAIL_MS / 1000:.0f}s before the scene ends ({(end - start) / 1000:.1f}s long). "
                "Use fewer items, or give the items earlier cues, or start the scene on an earlier cue."])
        for _ in range(14):
            mid = (lo + hi) / 2
            if _simulate(els, mid, cue_ms, start, end)[1]:
                lo = mid
            else:
                hi = mid
        plan, ok = _simulate(els, lo, cue_ms, start, end)
    return plan


def _events(s, els, plan, cue_ms):
    events, starts = [], {}
    for el, (t0, d) in zip(els, plan):
        oid = el.obj["id"]
        events.append({"target": oid, "action": "draw_strokes",
                       "at": {"cue": el.cue, "edge": "start"},
                       "offset_ms": int(t0 - cue_ms[el.cue][0]), "duration_ms": int(d)})
        starts.setdefault(el.group, (el.cue, t0))
        for x in el.extra:
            ev = {"target": oid, "at": {"cue": el.cue, "edge": "start"},
                  "offset_ms": int(t0 + d - cue_ms[el.cue][0])}
            ev.update(x)
            events.append(ev)
    groups = [g for g in dict.fromkeys(e.group for e in els) if g.startswith("i")]
    for k, g in enumerate(groups):
        first = next((e for e in els if e.group == g), None)
        if first is not None and first.dim_prev and k:
            cue, t0 = starts[g]
            for el in els:
                if el.group == groups[k - 1]:
                    events.append({"target": el.obj["id"], "action": "dim",
                                   "at": {"cue": cue, "edge": "start"},
                                   "offset_ms": int(t0 - cue_ms[cue][0]),
                                   "duration_ms": 350, "amount": 0.6})
    return events


# ---- anchoring by words ----------------------------------------------------------
#
# A plan can say WHERE something happens by quoting the narration instead of
# knowing cue numbers: a scene "start": "so what is vram doing", an item
# "at": "the model file is usually". The words are matched against the
# subtitles (case and punctuation ignored), so the same plan survives a new
# voice, a new speed or a re-transcription.

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list:
    return _WORD.findall(str(text).lower().replace("'", ""))


class _Stream:
    def __init__(self, cues):
        self.words = []                     # (token, cue index)
        for c in cues:
            for t in _tokens(c.text):
                self.words.append((t, c.index))
        self.last_cue = cues[-1].index if cues else 0

    def find(self, snippet, start=0, what=""):
        want = _tokens(snippet)
        if not want:
            raise ExplainerError([f"{what}: an empty anchor text."])
        toks = [w for w, _ in self.words]
        n = len(want)
        for i in range(start, len(toks) - n + 1):
            if toks[i:i + n] == want:
                return i, self.words[i][1]
        around = " ".join(toks[start:start + 12])
        raise ExplainerError([
            f"{what}: the words {snippet!r} were not found in the subtitles "
            f"after position {start} (the narration there starts: \"{around} ...\")."])


def resolve_anchors(plan: dict, cues) -> dict:
    """Plan with word anchors -> plan with cue numbers (a copy)."""
    stream = _Stream(cues)
    out = {k: v for k, v in plan.items() if k != "scenes"}
    out["scenes"] = []
    errors, pos, starts = [], 0, []
    scenes = plan.get("scenes") or []
    for idx, raw in enumerate(scenes):
        s = dict(raw)
        label = f"Scene {s.get('id') or idx + 1}"
        try:
            if "start" in s:
                p, cue = stream.find(s["start"], pos, f"{label} start")
                pos = p
                s["_pos"], s["_start"] = p, cue
            elif "cue_range" in s:
                s["_start"] = s["cue_range"][0]
                s["_pos"] = pos
            else:
                raise ExplainerError([f"{label}: needs a \"start\" (words of the narration) or a cue_range."])
        except ExplainerError as exc:
            errors.extend(exc.errors)
        out["scenes"].append(s)
    for idx, s in enumerate(out["scenes"]):
        if "_start" not in s:
            continue
        label = f"Scene {s.get('id') or idx + 1}"
        nxt = next((t for t in out["scenes"][idx + 1:] if "_start" in t), None)
        end = (nxt["_start"] - 1) if nxt else stream.last_cue
        if "cue_range" not in s or "start" in s:
            if end < s["_start"]:
                errors.append(f"{label}: the next scene starts on the same cue "
                              f"({s['_start']}); give it later words, or merge the scenes.")
                continue
            s["cue_range"] = [s["_start"], end]
        base = s["_pos"]

        def anchor(d, key="at", where=""):
            if isinstance(d, dict) and d.get(key) and d.get("cue") is None:
                try:
                    _, c = stream.find(d[key], base, f"{label} {where}")
                    d["cue"] = c
                except ExplainerError as exc:
                    errors.extend(exc.errors)
        s["items"] = [dict(i) for i in (s.get("items") or [])]
        for k, it in enumerate(s["items"]):
            anchor(it, where=f"item {k + 1} ({it.get('label', '')})")
        if isinstance(s.get("footer"), dict):
            s["footer"] = dict(s["footer"])
            anchor(s["footer"], where="footer")
        for key in ("counter", "zoom", "motion"):
            if isinstance(s.get(key), dict):
                s[key] = dict(s[key])
                anchor(s[key], where=key)
        sides = []
        for k, side in enumerate(s.get("sides") or []):
            side = dict(side)
            anchor(side, where=f"side {k + 1}")
            pts = []
            for j, pnt in enumerate(side.get("points") or []):
                pnt = {"text": pnt} if isinstance(pnt, str) else dict(pnt)
                anchor(pnt, where=f"side {k + 1} point {j + 1}")
                pts.append(pnt)
            side["points"] = pts
            sides.append(side)
        if sides:
            s["sides"] = sides
        for it in s["items"]:
            if it.get("cue") is not None and not (s["cue_range"][0] <= it["cue"] <= s["cue_range"][1]):
                errors.append(f"{label}: \"{it.get('at')}\" is in cue {it['cue']}, which is outside the scene "
                              f"(cues {s['cue_range']}); it belongs to a later scene.")
    if errors:
        raise ExplainerError(errors)
    for s in out["scenes"]:
        for k in ("_pos", "_start", "start"):
            s.pop(k, None)
    return out


REMOTION_LAYOUTS = ("fit_chart", "counter", "screenshot", "pipeline", "generation", "code")
FIT_CHART_MS = 1900          # a bar finishes growing, and its labels appear, within this


def _remotion_scene(raw: dict, idx: int, cue_ms: dict, style=None) -> dict:
    """A scene drawn by the Remotion backend: no objects, just a validated block."""
    sid = re.sub(r"[^A-Za-z0-9_\-]", "", str(raw.get("id") or f"s{idx + 1}")) or f"s{idx + 1}"
    if not sid[0].isalpha():
        sid = "s" + sid
    cr = raw.get("cue_range")
    if not (isinstance(cr, list) and len(cr) == 2 and cr[0] >= 1 and cr[1] >= cr[0]):
        raise ExplainerError([f"Scene {sid}: cue_range must be [first, last] (1-based)."])
    for c in cr:
        if c not in cue_ms:
            raise ExplainerError([f"Scene {sid}: cue {c} is not in the subtitles."])
    start, end = cue_ms[cr[0]][0], cue_ms[cr[1]][1]
    dur = end - start
    layout = raw["layout"]

    def rel_ms(spec, label):
        cue = spec.get("cue") if isinstance(spec, dict) else None
        if cue is None:
            return 400
        if cue not in cue_ms or not cr[0] <= cue <= cr[1]:
            raise ExplainerError([f"Scene {sid}: {label} is anchored outside the scene."])
        return max(cue_ms[cue][0] - start, 400)

    def must_finish(st, anim, label):
        if st + anim > dur - TAIL_MS:
            raise ExplainerError([f"Scene {sid}: {label} starts too late to finish "
                                  "before the scene ends; give it earlier words."])

    if layout == "fit_chart":
        items = raw.get("items") or []
        out_items = []
        for k, it in enumerate(items):
            cue = _item_cue({"cue_range": list(cr)}, items, k)
            if cue not in cue_ms or not cr[0] <= cue <= cr[1]:
                raise ExplainerError([f"Scene {sid}: item {k + 1} is anchored outside the scene."])
            st = max(cue_ms[cue][0] - start, 400 if k == 0 else 0)
            must_finish(st, FIT_CHART_MS, f"item {k + 1} ({it.get('label', '')})")
            out_items.append({"label": it.get("label", ""), "params_b": it.get("params_b"),
                              "start_ms": int(st)})
        params = {"limit": raw.get("limit"), "unit": raw.get("unit") or "GB", "bits": raw.get("bits"),
                  "items": out_items}
    elif layout == "counter":
        c = dict(raw.get("counter") or {})
        st = rel_ms(c, "the counter")
        cm = int(c.get("count_ms") or 2200)
        must_finish(st, cm, "the counter")
        params = {"from": c.get("from", 0), "to": c.get("to"), "unit": c.get("unit", ""),
                  "label": c.get("label", ""), "start_ms": int(st), "count_ms": cm}
    elif layout in ("pipeline", "generation", "code"):
        m = dict(raw.get("motion") or {})
        st = rel_ms(m, "the animation")
        run = int(m.get("run_ms") or min(max(dur - st - TAIL_MS, 3000), 12000))
        must_finish(st, run, "the animation")
        params = {k: v for k, v in raw.items() if k in ("title", "sub", "note", "stages", "tasks", "rows",
                                                          "filename", "lines", "highlights")}
        params["start_ms"] = int(st)
        if layout == "code":
            params["type_ms"] = int(m.get("type_ms") or min(run, 5000))
            params["highlights"] = [dict(h, start_ms=int(h.get("start_ms", params["type_ms"]))) for h in
                                    (raw.get("highlights") or []) if isinstance(h, dict)]
        else:
            params["run_ms"] = run
    else:
        z = dict(raw.get("zoom") or {})
        st = rel_ms(z, "the zoom")
        zm = int(z.get("zoom_ms") or 1600)
        must_finish(st, zm, "the zoom")
        params = {"image": raw.get("image"), "box": z.get("box"), "start_ms": int(st), "zoom_ms": zm,
                  "caption": raw.get("caption", "")}
    from . import remotion_backend
    block = {"component": raw["layout"], "params": params}
    if raw.get("fade"):
        block["fade"] = raw["fade"]
    if raw.get("motion_style") or style:
        block["style"] = raw.get("motion_style") or style
    errs = remotion_backend.check_block(block, f"Scene {sid}")
    if errs:
        raise ExplainerError(errs)
    return {"id": sid, "shot_id": f"S{idx + 1:02d}_01", "cue_range": list(cr), "objects": [],
            "events": [], "remotion": block}


_OVERFULL = re.compile(r"^Scene (\S+): too much for the time")
_MECHANISM = ("memory", "precision", "offload")


def _sid(raw, idx):
    sid = re.sub(r"[^A-Za-z0-9_\-]", "", str(raw.get("id") or f"s{idx + 1}")) or f"s{idx + 1}"
    return sid if sid[0].isalpha() else "s" + sid


def _simplify_step(scene, step):
    """Take one decoration off an over-full scene. Returns what was removed, or "" when nothing is left."""
    items = scene.get("items") or []
    if step == 0 and scene.get("footer") and not (isinstance(scene["footer"], dict) and scene["footer"].get("key")):
        scene.pop("footer")                          # a footer that shows a spoken key term stays
        return "its footer"
    if step <= 1 and any(isinstance(i, dict) and i.get("icon") for i in items):
        for i in items:
            if isinstance(i, dict):
                i.pop("icon", None)
        return "the item icons"
    if scene.get("layout") in _MECHANISM:
        return ""                                  # the numbers must keep adding up
    sides = scene.get("sides") or []
    if step <= 2 and any(isinstance(sd, dict) and sd.get("points") for sd in sides):
        for sd in sides:
            if isinstance(sd, dict):
                sd["points"] = []
        return "the compare points"
    if len(items) > 2:
        items.pop()
        return "the last item"
    return ""


def simplify_overfull(plan: dict, cues) -> tuple:
    """(plan, notes): a copy of the plan where scenes that cannot be drawn in time
    lose decoration one piece at a time (footer, icons, compare points, last item)
    until they fit. Nothing else is changed; notes say what was removed."""
    import copy
    plan = copy.deepcopy(plan)
    notes, steps = [], {}
    for _ in range(60):
        try:
            build(plan, cues)
            break
        except Exception as exc:  # noqa: BLE001 - only over-full scenes are ours to fix
            if not isinstance(exc, ExplainerError):
                break
            bad = {m.group(1) for m in (_OVERFULL.match(e) for e in exc.errors) if m}
            if not bad:
                break
            moved = False
            for idx, raw in enumerate(plan.get("scenes") or []):
                if not isinstance(raw, dict) or _sid(raw, idx) not in bad:
                    continue
                key = _sid(raw, idx)
                what = _simplify_step(raw, steps.get(key, 0))
                steps[key] = steps.get(key, 0) + 1
                if what:
                    notes.append(f"scene {key}: removed {what} (it did not fit the narration time)")
                    moved = True
            if not moved:
                break
    return plan, notes


PEN_FACTOR = {"slow": 1.3, "normal": 1.0, "fast": 0.75}


def build(plan: dict, cues, look=None) -> dict:
    """plan dict + SRT cues (video.parsing blocks) -> explainer.json dict."""
    if isinstance(plan, dict) and any(isinstance(x, dict) and ("start" in x or any(
            isinstance(i, dict) and i.get("at") for i in (x.get("items") or [])))
            for x in plan.get("scenes") or []):
        plan = resolve_anchors(plan, cues)
    if not isinstance(plan, dict) or not isinstance(plan.get("scenes"), list) or not plan["scenes"]:
        raise ExplainerError(["plan.json needs a non-empty scenes list."])
    cue_ms = {c.index: (int(round(c.start_seconds * 1000)), int(round(c.end_seconds * 1000)))
              for c in cues}
    scenes, errors = [], []
    for idx, raw in enumerate(plan["scenes"]):
        if isinstance(raw, dict) and raw.get("layout") in REMOTION_LAYOUTS:
            try:
                scenes.append(_remotion_scene(raw, idx, cue_ms, plan.get("motion_style")))
            except ExplainerError as exc:
                errors.extend(exc.errors)
            continue
        try:
            s, els = _scene_elements(raw, idx)
            pen = PEN_FACTOR.get(str((look or {}).get("pen_speed") or "normal"), 1.0)
            if pen != 1.0:
                for e in els:
                    e.dur = max(e.min, int(e.dur * pen))
            sched = _schedule(s, els, cue_ms)
        except ExplainerError as exc:
            errors.extend(exc.errors)
            continue
        scenes.append({
            "id": s["id"], "shot_id": f"S{idx + 1:02d}_01", "cue_range": list(s["cue_range"]),
            "objects": [e.obj for e in els],
            "events": _events(s, els, sched, cue_ms)})
    if errors:
        raise ExplainerError(errors)
    doc = {"schema_version": sc.SCHEMA_VERSION, "style": sc.STYLE_WHITEBOARD, "scenes": scenes,
           "canvas": {"width": 1920, "height": 1080}}
    if plan.get("theme"):
        doc["theme"] = plan["theme"]
    if isinstance(plan.get("fonts"), dict):
        doc["fonts"] = plan["fonts"]
    return doc
