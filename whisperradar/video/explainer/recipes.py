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

from . import camera as cam_mod
from . import font_styles
from . import mechanisms
from . import vocabulary
from . import schema as sc
from . import strokes_library
from .validate import ExplainerError

LAYOUTS = ("row", "steps", "timeline", "compare", "stack", "hero", "memory", "precision", "offload") + vocabulary.LAYOUTS
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


# A handwriting line is written as tall as its box allows and then shrunk until it fits the box
# width, so a long line in a short, narrow box ends up tiny or clipped. Text that cannot fit on one
# line at a readable size is broken onto two lines before it is placed.
CHAR_W = 0.5             # average glyph width / font size (the hand fonts are about 0.45-0.55)
LINE_RATIO = 1.4         # box height / font size (matches pipe._fit_text)
MIN_READABLE = 0.03      # smallest font size, as a share of the canvas height, we accept
CANVAS_AR = 16 / 9


def one_line_size(text: str, box) -> float:
    """Font size (share of canvas height) the text gets when written on one line in this box."""
    w, h = float(box[2]), float(box[3])
    by_height = h / LINE_RATIO
    by_width = (w * CANVAS_AR) / max(1.0, len(text) * CHAR_W)
    return min(by_height, by_width)


def fit_text(text: str, box):
    """(text, lines): the text broken onto two lines when one line would be too small to read and
    two lines (each at half the height) read better. Existing line breaks are left alone."""
    if "\n" in text or " " not in text or one_line_size(text, box) >= MIN_READABLE:
        return text, text.count("\n") + 1
    two, _n = _lines(text, 0)                    # the most even split
    h = float(box[3]) / 2
    best_line = max(two.split("\n"), key=len)
    two_size = min(h / LINE_RATIO, (float(box[2]) * CANVAS_AR) / max(1.0, len(best_line) * CHAR_W))
    if two_size > one_line_size(text, box):
        return two, 2
    return text, 1


def _text(oid, text, box, color, cue, group="", font=None, slow=1.0):
    text, _nl = fit_text(text, box)
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
    k = float(s.get("_title_scale") or 1.0)               # font style / channel "headline size"
    els.append(_text(f"{sid}_title", t, [0.07, 0.06, 0.86, 0.12 * k], "heading", cue,
                     group="title"))
    w = min(0.86, len(t) * 0.027 + 0.02)
    els.append(_shape(f"{sid}_tline", "underline", cue, "orange", "title",
                      box=[0.07, 0.06, w, 0.125 * k], dur=450, fit_to=f"{sid}_title"))
    return 0.27 + 0.12 * (k - 1.0)


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
    ih = min(0.31, cw * 0.62 / WN)
    iw = ih * WN
    two = any(_lines(i.get("label", ""))[1] == 2 for i in items)
    lh = 0.17 if two else 0.11
    note_h = 0.07 if any(i.get("note") for i in items) else 0
    rest = (0.115 if timeline else 0.03) + lh + (0.01 + note_h if note_h else 0)   # timeline: line, nodes, then label
    avail_top, avail_bot = top, s["_bot"]
    ih = max(0.12, min(ih, avail_bot - avail_top - rest))        # the icons give way so the labels fit above a footer
    iw = ih * WN
    block = ih + rest
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
                                                      0.17 if nl == 2 else 0.11],
                             "text", cue, g))
        if it.get("note"):
            ny = ((y_line + 0.04) if timeline else yy + ih + 0.03) + (0.17 if nl == 2 else 0.11) + 0.01
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
    rh = min(0.22, (s["_bot"] - top) / n)
    ih = rh * 0.8
    iw = ih * WN
    longest = max((len(str(i.get("label", ""))) for i in items), default=10)
    block_w = iw + 0.04 + min(0.62, longest * 0.032)          # icon + words, centred in the frame
    x_ic = max(0.08, 0.5 - block_w / 2)
    top = top + max(0.0, ((s["_bot"] - top) - rh * n) / 2)    # centred between the title and the bottom
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
    below = (0.22 if nl == 2 else 0.14) + (0.03 + (0.07 if it.get("note") else 0.0))
    ih = max(0.14, min(0.42, s["_bot"] - y - below - 0.03))      # the icon gives way so the words clear the footer
    iw = ih * WN
    if it.get("icon"):
        els.append(_icon(f"{sid}_ic0", _asset(it["icon"]), [0.5 - iw / 2, y, iw, ih], col, cue, "i0", sid))
    els.append(_text(f"{sid}_lb0", label, [0.08, y + ih + 0.03, 0.84, 0.22 if nl == 2 else 0.14],
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
        card_h = min(s["_bot"] - top + 0.02, 0.22 + npts * 0.17 + 0.03)
        els.append(_shape(f"{sid}_card{k}", "card", cue, col, g, dur=900, filled=True,
                          box=[x, top - 0.02, 0.42, card_h]))
        hx = x + 0.025
        if side.get("icon"):
            els.append(_icon(f"{sid}_ic{k}", _asset(side["icon"]), [hx, top + 0.015, 0.1 * WN * 1.0, 0.1],
                             col, cue, g, sid))
            hx += 0.1 * WN + 0.02
        els.append(_text(f"{sid}_hd{k}", side.get("header", ""), [hx, top + 0.005, x + 0.4 - hx, 0.14],
                         col, cue, g))
        pts = side.get("points") or []
        py = top + 0.19
        step = min(0.17, (s["_bot"] - 0.02 - py) / max(1, len(pts)))
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


def _into_region(s, sid, els):
    """Draw the scene inside a part of the frame (so a base scene's drawing is not overlapped).
    region = [x, y, w, h] as shares of the frame."""
    r = s["region"]
    if not (isinstance(r, list) and len(r) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in r)
            and r[2] > 0.1 and r[3] > 0.1 and r[0] >= 0 and r[1] >= 0 and r[0] + r[2] <= 1.0001 and r[1] + r[3] <= 1.0001):
        raise ExplainerError([f"Scene {sid}: region must be [x, y, w, h] inside the frame (w and h at least 0.1)."])
    rx, ry, rw, rh = [float(v) for v in r]
    for el in els:
        o = el.obj
        if o.get("box"):
            x, y, w, h = o["box"]
            o["box"] = _r([rx + x * rw, ry + y * rh, w * rw, h * rh])
        if o.get("points"):
            o["points"] = [[round(rx + px * rw, 4), round(ry + py * rh, 4)] for px, py in o["points"]]


def _camera_for(raw, region, prev_end):
    """The scene's camera, only when the plan asks for one (camera: "region" | "overview" | {zoom, center,
    move_ms}). Never added on its own. Returns (camera dict or None, end view)."""
    c = raw.get("camera")
    if not c:
        return None, cam_mod.OVERVIEW
    if c == "region":
        if not region:
            raise ExplainerError([f"Scene {raw.get('id')}: camera \"region\" needs a region."])
        to = cam_mod.region_view(region)
    elif c == "overview":
        to = dict(cam_mod.OVERVIEW)
    elif isinstance(c, dict):
        to = {"zoom": float(c.get("zoom", 1)), "center": list(c.get("center") or [0.5, 0.5])}
    else:
        raise ExplainerError([f"Scene {raw.get('id')}: camera must be \"region\", \"overview\" or {{zoom, center}}."])
    out = {"from": c.get("from") if isinstance(c, dict) and c.get("from") else prev_end, "to": to}
    if isinstance(c, dict) and c.get("move_ms"):
        out["move_ms"] = int(c["move_ms"])
    return out, to


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
    elif layout in vocabulary.BUILDERS:
        vocabulary.BUILDERS[layout](sys.modules[__name__], s, sid, els, top)
    else:
        _compare(s, sid, els, top)
    _footer(s, sid, els)
    if s.get("region"):
        _into_region(s, sid, els)
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
        self.first_of, self.count_of = {}, {}
        for i, (_, c) in enumerate(self.words):
            self.first_of.setdefault(c, i)
            self.count_of[c] = self.count_of.get(c, 0) + 1

    def frac(self, i: int) -> float:
        """Where word i sits inside its subtitle line, 0..1 (so a beat can land on its words, not the line start)."""
        c = self.words[i][1]
        n = self.count_of.get(c, 1)
        return round((i - self.first_of.get(c, i)) / n, 3) if n > 1 else 0.0

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
                    i, c = stream.find(d[key], base, f"{label} {where}")
                    d["cue"] = c
                    frac = stream.frac(i)
                    if frac:
                        d["cue_frac"] = frac
                except ExplainerError as exc:
                    errors.extend(exc.errors)
        s["items"] = [dict(i) for i in (s.get("items") or [])]
        for k, it in enumerate(s["items"]):
            anchor(it, where=f"item {k + 1} ({it.get('label', '')})")
            if isinstance(it.get("rows"), list):
                it["rows"] = [dict(x) if isinstance(x, dict) else {"text": str(x)} for x in it["rows"]]
                for j, rw in enumerate(it["rows"]):
                    anchor(rw, where=f"item {k + 1} row {j + 1}")
        if isinstance(s.get("footer"), dict):
            s["footer"] = dict(s["footer"])
            anchor(s["footer"], where="footer")
        if isinstance(s.get("points"), list):
            s["points"] = [dict(x) if isinstance(x, dict) else {"text": str(x)} for x in s["points"]]
            for k, x in enumerate(s["points"]):
                anchor(x, where=f"point {k + 1}")
        for key in ("counter", "zoom", "motion", "output"):
            if isinstance(s.get(key), dict):
                s[key] = dict(s[key])
                anchor(s[key], where=key)
        for key in ("decoder", "result"):
            if isinstance(s.get(key), dict):
                s[key] = dict(s[key])
                anchor(s[key], where=key)
        if isinstance(s.get("limit"), dict):
            s["limit"] = dict(s["limit"])
            anchor(s["limit"], where="limit")
        for key in ("stages", "highlights", "levels", "nodes", "sends", "steps", "marks", "series", "chips"):
            if isinstance(s.get(key), list):
                s[key] = [dict(x) if isinstance(x, dict) else x for x in s[key]]
                for k, x in enumerate(s[key]):
                    anchor(x, where=f"{key[:-1]} {k + 1}")
        if isinstance(s.get("rows"), list):
            rows = []
            for k, row in enumerate(s["rows"]):
                row = dict(row) if isinstance(row, dict) else row
                if isinstance(row, dict) and isinstance(row.get("beats"), list):
                    row["beats"] = [dict(b) if isinstance(b, dict) else b for b in row["beats"]]
                    for j, b in enumerate(row["beats"]):
                        anchor(b, where=f"row {k + 1} beat {j + 1}")
                rows.append(row)
            s["rows"] = rows
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


# General motion layouts work for any topic (tech, finance, ...); the others are specialised.
GENERAL_LAYOUTS = ("bars", "flow", "line", "grid", "split")
REMOTION_LAYOUTS = ("fit_chart", "counter", "screenshot", "pipeline", "generation", "code", "code_lesson", "levels") + GENERAL_LAYOUTS
FIT_CHART_MS = 1900          # a bar finishes growing, and its labels appear, within this


# animation lengths the components use (ms): keep in step with tools/remotion/src/components
BAR_MS, FLOW_HOP_MS, FLOW_DWELL_MS, SPLIT_MS, CELL_MS = 1300, 650, 350, 900, 350
BEAT_GAP_MS = 600            # two things anchored to the same subtitle line appear this far apart


def _icon_paths(name):
    """A Lucide icon as SVG paths for the Remotion components; None when unknown (decoration only)."""
    if not isinstance(name, str) or not name.strip():
        return None
    icons = strokes_library._lucide()
    key = strokes_library._lucide_key(name.split(":", 1)[-1])
    return list(icons[key]["strokes"]) if key in icons else None


def _general_params(raw: dict, sid: str, layout: str, cr, cue_ms: dict, start: int, dur: int) -> dict:
    """bars / flow / line / grid / split -> component params with scene-relative ms.
    Narration anchors become at_ms; two anchors on the same subtitle line are spread out; an anchor
    said so late that its animation could not finish is moved earlier (never past the scene)."""
    def at_of(spec, default):
        cue = spec.get("cue") if isinstance(spec, dict) else None
        if cue is None:
            return default
        if cue not in cue_ms or not cr[0] <= cue <= cr[1]:
            raise ExplainerError([f"Scene {sid}: \"{spec.get('at', '')}\" is anchored outside the scene."])
        a, b = cue_ms[cue]
        frac = spec.get("cue_frac") if isinstance(spec.get("cue_frac"), (int, float)) else 0.0
        return max(int(a + max(0.0, min(0.95, frac)) * (b - a) - start), 0)

    m = raw.get("motion") if isinstance(raw.get("motion"), dict) else {}
    st = max(at_of(m, 400), 400)
    latest = dur - TAIL_MS
    first_key = _FIRST_OF.get(layout, (None,))[0]
    if not raw.get("title") and first_key and raw.get(first_key):
        # untitled: its first element is what opens the scene (the builder starts the scene on that
        # element's line), so it draws from the scene's first frame, not part-way into the line
        lst = [dict(x) if isinstance(x, dict) else x for x in raw[first_key]]
        if isinstance(lst[0], dict):
            lst[0].pop("cue_frac", None)
            lst[0]["_opens"] = True
        raw = dict(raw, **{first_key: lst})

    def beats(seq, anim, label, step=1200):
        out, prev = [], None
        for k, x in enumerate(seq):
            x = dict(x) if isinstance(x, dict) else {"label": str(x)}
            t = at_of(x, st + k * step)
            t = 0 if x.pop("_opens", False) else max(t, st if k == 0 else 0)
            if prev is not None and t < prev + BEAT_GAP_MS:
                t = prev + BEAT_GAP_MS
            a = anim(x) if callable(anim) else anim
            if t + a > latest:
                t = latest - a
                if prev is not None and t < prev + 250 or t < 0:
                    raise ExplainerError([f"Scene {sid}: too much for the time - the {label}s cannot all appear "
                                          "and finish before the scene ends; use fewer, or a longer scene."])
            x["at_ms"] = int(t)
            prev = t
            for key in ("at", "cue", "cue_frac"):
                x.pop(key, None)
            out.append(x)
        return out

    keep = {"bars": ("title", "sub", "unit", "prefix", "ratio_to", "illustrative"),
            "flow": ("title", "sub"),
            "line": ("title", "sub", "unit", "prefix", "x", "zero", "illustrative", "shape"),
            "grid": ("title", "sub", "rows", "cols", "show_share", "illustrative"),
            "split": ("title", "sub", "unit", "prefix", "total", "total_label", "rest_label", "center",
                      "illustrative")}[layout]
    params = {k: raw[k] for k in keep if raw.get(k) not in (None, "")}
    params["start_ms"] = int(st)

    def clean(x, fields):
        out = {k: x[k] for k in fields if x.get(k) not in (None, "")}
        if "icon" in fields:
            out.pop("icon", None)
            ip = _icon_paths(x.get("icon"))
            if ip:
                out["icon_paths"] = ip
        return out

    if layout in ("bars", "split"):
        anim = BAR_MS if layout == "bars" else SPLIT_MS
        items = beats(raw.get("items") or [], anim, "bar" if layout == "bars" else "part")
        params["items"] = [dict(clean(x, ("label", "value", "note", "color", "icon", "decimals")),
                                at_ms=x["at_ms"]) for x in items]
        if layout == "bars" and isinstance(raw.get("limit"), dict):
            lim = beats([raw["limit"]], 600, "limit")[0]
            params["limit"] = dict(clean(lim, ("label", "value")), at_ms=lim["at_ms"])
    elif layout == "flow":
        nodes = beats(raw.get("nodes") or [], 600, "node", step=700)
        params["nodes"] = [dict(clean(x, ("label", "sub", "color", "icon")), at_ms=x["at_ms"]) for x in nodes]
        n = len(nodes)

        def send_ms(x):
            hops = max(1, abs((x.get("to") or n) - (x.get("from") or 1)))
            one = hops * FLOW_HOP_MS + FLOW_DWELL_MS
            return one * (2 if x.get("back") else 1)
        first = (nodes[-1]["at_ms"] + 600) if nodes else st
        sends = beats(raw.get("sends") or [], send_ms, "send")
        for x in sends:                       # a token never leaves before the boxes it travels through are drawn
            x["at_ms"] = max(x["at_ms"], min(first, latest - send_ms(x)))
        params["sends"] = [dict(clean(x, ("label", "back", "from", "to", "color")), at_ms=x["at_ms"]) for x in sends]
    elif layout == "line":
        run = int(m.get("run_ms") or min(max(latest - st, 2500), 9000))
        if st + run > latest:
            st = max(0, latest - run)
            params["start_ms"] = int(st)
        if st + run > latest:
            raise ExplainerError([f"Scene {sid}: too much for the time - the line needs {run} ms to draw."])
        params["run_ms"] = run
        series = []
        for k, x in enumerate(raw.get("series") or []):
            x = dict(x) if isinstance(x, dict) else {}
            y = clean(x, ("label", "values", "color", "decimals"))
            if x.get("cue") is not None and k > 0:
                y["at_ms"] = min(max(at_of(x, st), st), max(st, latest - 1500))
            series.append(y)
        params["series"] = series
        marks = beats(raw.get("marks") or [], 600, "mark")
        params["marks"] = [dict(clean(x, ("x", "text", "series")), at_ms=x["at_ms"]) for x in marks]
    elif layout == "grid":
        steps = beats(raw.get("steps") or [], lambda x: min(1400, int(x.get("cells") or 1) * 35) + CELL_MS, "step")
        params["steps"] = [dict(clean(x, ("label", "cells", "color", "symbols")), at_ms=x["at_ms"]) for x in steps]
    ft = raw.get("footer")
    if isinstance(ft, str):
        ft = {"text": ft}
    if isinstance(ft, dict) and ft.get("text"):
        fs = at_of(ft, st + 600)
        fs = min(fs, max(latest - 700, st))
        params["footer"] = {"text": str(ft["text"]), "start_ms": int(fs)}
    return params


CODE_LESSON_DIAGRAM_MIN_MS = 2500     # a diagram that comes first needs this long on screen before the code opens


def _code_lesson_params(raw: dict, sid: str, cr, cue_ms: dict, start: int, dur: int) -> dict:
    """code_lesson: typed code (small type) + optional chips, output panel and a diagram that comes
    first and/or sits beside the editor. The diagram is one of the general layouts, written at the top
    level of the scene ("diagram": "flow", "nodes": [...]) so its steps anchor to words like any other."""
    def at_of(spec, default):
        cue = spec.get("cue") if isinstance(spec, dict) else None
        if cue is None:
            return default
        if cue not in cue_ms or not cr[0] <= cue <= cr[1]:
            raise ExplainerError([f"Scene {sid}: \"{spec.get('at', '')}\" is anchored outside the scene."])
        return max(int(cue_ms[cue][0] - start), 0)

    def finish(st, anim, label):
        if st + anim > dur - TAIL_MS:
            raise ExplainerError([f"Scene {sid}: {label} starts too late to finish before the scene ends; "
                                  "give it earlier words."])

    params = {k: v for k, v in raw.items() if k in ("title", "sub", "filename", "lines")}
    dname = raw.get("diagram")
    place = raw.get("diagram_place") or "before_then_beside"
    diagram = None
    if dname:
        if dname not in GENERAL_LAYOUTS:
            raise ExplainerError([f"Scene {sid}: diagram must be one of {', '.join(GENERAL_LAYOUTS)}."])
        dparams = _general_params({k: v for k, v in raw.items() if k not in ("motion", "footer", "title", "sub", "points")},
                                  sid, dname, cr, cue_ms, start, dur)
        diagram = {"place": place, "component": dname, "params": dparams}
        params["diagram"] = diagram

    m = dict(raw.get("motion") or {})
    st = max(at_of(m, 400), 400)
    if diagram and place in ("before", "before_then_beside"):
        st = max(st, CODE_LESSON_DIAGRAM_MIN_MS)
    n_chars = len("\n".join(raw.get("lines") or []))
    type_ms = int(m.get("type_ms") or min(max(n_chars * 45, 2500), 9000))
    finish(st, type_ms, "the code")
    params["start_ms"] = int(st)
    params["type_ms"] = type_ms

    if diagram and place == "before":
        last = max([x for x in _all_at_ms(diagram["params"])] or [0])
        if last + 1200 > st:
            raise ExplainerError([f"Scene {sid}: the diagram is still animating (until {last} ms) when the code opens "
                                  f"({st} ms) and it then disappears; anchor its steps earlier, start the code later, "
                                  "or use diagram_place \"before_then_beside\"."])

    hls = []
    for k, h in enumerate(raw.get("highlights") or []):
        if isinstance(h, dict):
            at = at_of(h, None)
            hls.append(dict(h, start_ms=max(at - st, 0) if at is not None else int(h.get("start_ms", type_ms))))
    params["highlights"] = hls

    chips = []
    for k, c in enumerate(raw.get("chips") or []):
        c = {"label": c} if isinstance(c, str) else dict(c)
        chips.append({"label": str(c.get("label", "")), "color": c.get("color"),
                      "start_ms": int(at_of(c, 300 + 250 * k))})
    if chips:
        params["chips"] = [{k: v for k, v in c.items() if v is not None} for c in chips]

    o = raw.get("output")
    if isinstance(o, dict) and o.get("lines"):
        params["output"] = {"label": str(o.get("label") or "Output"), "lines": [str(x) for x in o["lines"]],
                            "start_ms": int(max(at_of(o, st + type_ms + 400), st))}
    ft = raw.get("footer")
    ft = {"text": ft} if isinstance(ft, str) else ft
    if isinstance(ft, dict) and ft.get("text"):
        fs = at_of(ft, st + 600)
        params["footer"] = {"text": str(ft["text"]), "start_ms": int(min(fs, max(dur - TAIL_MS - 700, st)))}
    return params


def _all_at_ms(node):
    """Every at_ms inside a compiled block (the moments its animation changes)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "at_ms" and isinstance(v, (int, float)):
                yield int(v)
            else:
                yield from _all_at_ms(v)
    elif isinstance(node, list):
        for v in node:
            yield from _all_at_ms(v)


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
        for key in ("title", "sub"):                 # the scene's heading was dropped before
            if raw.get(key):
                params[key] = str(raw[key])
        ft = raw.get("footer")
        ft = {"text": ft} if isinstance(ft, str) else ft
        if isinstance(ft, dict) and ft.get("text"):
            fa = ft.get("cue")
            fs = (max(cue_ms[fa][0] - start, 0) if fa in cue_ms and cr[0] <= fa <= cr[1] else st + cm)
            params["footer"] = {"text": str(ft["text"]), "start_ms": int(min(fs, max(dur - TAIL_MS - 700, 0)))}
    elif layout in ("pipeline", "generation", "code", "levels"):
        m = dict(raw.get("motion") or {})
        st = rel_ms(m, "the animation")
        if not m.get("run_ms") and st + 3000 > dur - TAIL_MS:
            st = max(400, dur - TAIL_MS - 3000)               # said late in the scene: start a little earlier
        run = int(m.get("run_ms") or min(max(dur - st - TAIL_MS, 3000), 18000))
        must_finish(st, run, "the animation")
        params = {k: v for k, v in raw.items() if k in ("title", "sub", "note", "stages", "tasks", "rows",
                                                          "filename", "lines", "highlights", "levels", "decoder",
                                                          "result")}
        params["start_ms"] = int(st)

        def beat_ms(spec, label):
            """Scene-relative ms of a narration anchor, or None when the spec has none."""
            if not isinstance(spec, dict) or spec.get("cue") is None:
                return None
            if spec["cue"] not in cue_ms or not cr[0] <= spec["cue"] <= cr[1]:
                raise ExplainerError([f"Scene {sid}: {label} is anchored outside the scene."])
            return max(int(cue_ms[spec["cue"]][0] - start), 0)
        if layout == "code":
            params["type_ms"] = int(m.get("type_ms") or min(run, 5000))
            hls = []
            for k, h in enumerate(raw.get("highlights") or []):
                if not isinstance(h, dict):
                    continue
                at = beat_ms(h, f"highlight {k + 1}")
                hls.append(dict(h, start_ms=max(at - st, 0) if at is not None else int(h.get("start_ms", params["type_ms"]))))
            params["highlights"] = hls
        else:
            params["run_ms"] = run
        ft = raw.get("footer")
        if isinstance(ft, str):
            ft = {"text": ft}
        if isinstance(ft, dict) and ft.get("text"):
            fa = beat_ms(ft, "the footer")
            fs = int(fa if fa is not None else st + 600)
            fs = min(fs, max(dur - TAIL_MS - 700, st))        # a footer said too late appears a little earlier
            params["footer"] = {"text": str(ft["text"]), "start_ms": fs}

        def in_order(vals, label, gap=500):
            for a, b in zip(vals, vals[1:]):
                if b - a < gap:
                    raise ExplainerError([f"Scene {sid}: {label} are anchored to the same moment of the narration "
                                          f"(less than {gap} ms apart); anchor each to words said in a different place."])
            if vals:
                must_finish(vals[-1], 1200, label)
        if layout == "levels":
            lv = []
            for k, x in enumerate(raw.get("levels") or []):
                x = dict(x)
                at = beat_ms(x, f"level {k + 1}")
                x["at_ms"] = int(at if at is not None else st + k * 1500)
                x.pop("at", None), x.pop("cue", None)
                lv.append(x)
            in_order([x["at_ms"] for x in lv], "the levels")
            params["levels"] = lv
            for key in ("decoder", "result"):
                if isinstance(raw.get(key), dict):
                    d = dict(raw[key])
                    at = beat_ms(d, key)
                    d["at_ms"] = int(at if at is not None else (lv[-1]["at_ms"] + 1500 if lv else st))
                    d.pop("at", None), d.pop("cue", None)
                    must_finish(d["at_ms"], 1200, key)
                    params[key] = d
        if layout == "pipeline":
            stages = []
            for k, sg in enumerate(raw.get("stages") or []):
                sg = dict(sg)
                at = beat_ms(sg, f"stage {k + 1}")
                if at is not None:
                    sg["at_ms"] = at
                sg.pop("at", None), sg.pop("cue", None)
                stages.append(sg)
            params["stages"] = stages
            in_order([g["at_ms"] for g in stages if "at_ms" in g], "the stages")
        if layout == "generation":
            rows = []
            for k, row in enumerate(raw.get("rows") or []):
                row = dict(row)
                bs = [beat_ms(b, f"row {k + 1} beat {j + 1}") for j, b in enumerate(row.pop("beats", None) or [])]
                if bs and None not in bs:
                    in_order(bs, f"the steps of row {k + 1}")
                    row["beats_ms"] = bs
                rows.append(row)
            params["rows"] = rows
        if layout == "pipeline":
            last = max([st] + [g.get("at_ms", st) for g in params["stages"]])
            needed = max(run, last - st + 1200)
            if m.get("run_ms") and needed > run:
                raise ExplainerError([f"Scene {sid}: the stage anchor is after the animation window; "
                                      "extend motion.run_ms or choose earlier stage words."])
            must_finish(st, needed, "the anchored pipeline")
            params["run_ms"] = int(needed)
        if layout == "generation":
            last = max([st] + [b for row in params["rows"] for b in (row.get("beats_ms") or [])])
            needed = max(run, last - st + 1200)
            must_finish(st, needed, "the anchored steps")
            params["run_ms"] = int(needed)      # the window covers every beat, so the spec reads true
        if layout == "levels":
            last = max([st] + [x["at_ms"] for x in params["levels"]]
                       + [params[k]["at_ms"] for k in ("decoder", "result") if k in params])
            needed = max(run, last - st + 1200)
            must_finish(st, needed, "the anchored levels")
            params["run_ms"] = int(needed)
    elif layout == "code_lesson":
        params = _code_lesson_params(raw, sid, cr, cue_ms, start, dur)
    elif layout in GENERAL_LAYOUTS:
        params = _general_params(raw, sid, layout, cr, cue_ms, start, dur)
    else:
        z = dict(raw.get("zoom") or {})
        st = rel_ms(z, "the zoom")
        zm = int(z.get("zoom_ms") or 1600)
        must_finish(st, zm, "the zoom")
        params = {"image": raw.get("image"), "box": z.get("box"), "start_ms": int(st), "zoom_ms": zm,
                  "caption": raw.get("caption", "")}
    if isinstance(raw.get("points"), list) and raw["points"]:
        pts = []
        for k, x in enumerate(raw["points"]):
            x = x if isinstance(x, dict) else {"text": str(x)}
            cue = x.get("cue")
            if cue is None or cue not in cue_ms or not cr[0] <= cue <= cr[1]:
                raise ExplainerError([f"Scene {sid}: point {k + 1} is anchored outside the scene."])
            at = max(cue_ms[cue][0] - start, 150)
            text = str(x.get("text") or "")
            hold = 900 + 320 * len(text.split())            # time to read it once it is fully shown
            if raw.get("points_mode") == "written":
                hold = int(hold * 1.6) + 800                # a written reveal takes about a second to finish
            if at + hold > dur - TAIL_MS:
                raise ExplainerError([f"Scene {sid}: point {k + 1} ({text[:30]}) starts too late to be read before "
                                      f"the scene ends (needs about {hold / 1000:.1f} s on screen); give it earlier "
                                      "words or shorten it."])
            pts.append({"text": str(x.get("text") or ""), "start_ms": int(at)})
        params["points"] = pts
        if raw.get("points_mode"):
            params["points_mode"] = str(raw["points_mode"])
        if raw.get("points_place"):
            params["points_place"] = str(raw["points_place"])
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
    if scene.get("layout") in _MECHANISM + GENERAL_LAYOUTS:
        return ""                                  # the numbers must keep adding up; every part is a point
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


def _icons_step(scene):
    """Decoration only: item icons. Never a footer, label, point or scene."""
    items = scene.get("items") or []
    if any(isinstance(i, dict) and i.get("icon") for i in items):
        for i in items:
            if isinstance(i, dict):
                i.pop("icon", None)
        return "the item icons"
    return ""


def simplify_overfull(plan: dict, cues, decoration_only: bool = False) -> tuple:
    """(plan, notes): a copy of the plan where scenes that cannot be drawn in time
    lose decoration one piece at a time (footer, icons, compare points, last item)
    until they fit. Nothing else is changed; notes say what was removed.
    decoration_only: remove item icons and nothing else - explanatory labels, footers and
    whole scenes are never dropped automatically (the writer splits or merges instead)."""
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
                what = _icons_step(raw) if decoration_only else _simplify_step(raw, steps.get(key, 0))
                steps[key] = steps.get(key, 0) + 1
                if what:
                    notes.append(f"scene {key}: removed {what} (it did not fit the narration time)")
                    moved = True
            if not moved and decoration_only:
                break
            if not moved:
                # nothing left to take off: the scene is simply too short for any picture.
                # Remove it; the scene before it stays on screen through those words.
                keep = [r for i, r in enumerate(plan.get("scenes") or [])
                        if not (isinstance(r, dict) and _sid(r, i) in bad)]
                if not keep or len(keep) == len(plan.get("scenes") or []):
                    break
                for i, r in enumerate(plan["scenes"]):
                    if isinstance(r, dict) and _sid(r, i) in bad:
                        notes.append(f"scene {_sid(r, i)}: removed (too short for any picture; "
                                     "the scene before it stays on screen)")
                plan["scenes"] = keep
    return plan, notes


PEN_FACTOR = {"slow": 1.3, "normal": 1.0, "fast": 0.75}
# Bump when the builder's output changes for the same plan (new placement, timing or safety rules):
# explainer.json files built by an older builder are rebuilt on the next render.
BUILDER_VERSION = "2026-10-31.continuous"
LEAD_MS = 0              # an untitled scene whose first content is later than its start at all is held


def _lead_in(s, els, cue_ms):
    """Something is on screen from a scene's first moment: without a title, the first item (and
    anything anchored with it) starts at the scene's first words instead of waiting for its own,
    when those come more than LEAD_MS later."""
    if not els or s.get("title"):
        return
    c0 = s["cue_range"][0]
    first = min(e.cue for e in els)
    if first in cue_ms and c0 in cue_ms and cue_ms[first][0] - cue_ms[c0][0] > LEAD_MS:
        for e in els:
            if e.cue == first:
                e.cue = c0


_FIRST_OF = {"bars": ("items",), "flow": ("nodes",), "levels": ("levels",)}


def _first_content_cue(raw: dict):
    """The cue of the first thing an untitled scene draws, or None when it draws from its start
    (a title, or a Remotion component that shows its frame at once)."""
    if raw.get("title"):
        return None
    lay = raw.get("layout")
    if lay in LAYOUTS:
        keys = ("items", "sides")
    elif lay in _FIRST_OF:
        keys = _FIRST_OF[lay]
    else:
        return None
    cues = []
    for k in keys:
        for x in raw.get(k) or []:
            if isinstance(x, dict) and isinstance(x.get("cue"), int):
                cues.append(x["cue"])
            for p in (x.get("points") or []) if isinstance(x, dict) else []:
                if isinstance(p, dict) and isinstance(p.get("cue"), int):
                    cues.append(p["cue"])
    if isinstance(raw.get("footer"), dict) and isinstance(raw["footer"].get("cue"), int):
        cues.append(raw["footer"]["cue"])
    for x in raw.get("points") or []:
        if isinstance(x, dict) and isinstance(x.get("cue"), int):
            cues.append(x["cue"])
    return min(cues) if cues else None


def _hold_previous(scenes: list, cue_ms: dict) -> list:
    """Continuous picture without moving any reveal: when an untitled scene would open on an
    empty board for more than LEAD_MS, the scene before it stays on screen until the new scene's
    first content, and the new scene starts there. Returns [(scene id, held seconds)]."""
    held = []
    for i in range(1, len(scenes)):
        cur, prev = scenes[i], scenes[i - 1]
        if not (isinstance(cur, dict) and isinstance(prev, dict)):
            continue
        cr, pr = cur.get("cue_range"), prev.get("cue_range")
        k = _first_content_cue(cur)
        if not (cr and pr and k) or k <= cr[0] or k not in cue_ms or cr[0] not in cue_ms:
            continue
        if cue_ms[k][0] - cue_ms[cr[0]][0] > LEAD_MS and pr[1] == cr[0] - 1 and k <= cr[1]:
            held.append((cur.get("id"), (cue_ms[k][0] - cue_ms[cr[0]][0]) / 1000))
            prev["cue_range"] = [pr[0], k - 1]
            cur["cue_range"] = [k, cr[1]]
    return held


def first_visible_ms(scene: dict, cue_ms: dict) -> int:
    """How long a built scene shows nothing at its start (ms). Pen scenes: the first drawing
    event; Remotion scenes: 0 with a title, else the earliest step of the animation."""
    cr = scene.get("cue_range") or [0, 0]
    start = cue_ms.get(cr[0], (0, 0))[0]
    block = scene.get("remotion")
    if block:
        p = block.get("params") or {}
        if p.get("title"):
            return 0
        # these draw nothing until their first step; the others draw their frame (axes, board,
        # ring, stage boxes, empty slots, the number) from start_ms
        first_of = {"bars": "items", "flow": "nodes", "levels": "levels"}.get(block.get("component"))
        if first_of:
            times = [x.get("at_ms") for x in p.get(first_of) or [] if isinstance(x, dict)]
            times = [t for t in times if isinstance(t, (int, float))]
            return int(min(times)) if times else 0
        return int(p.get("start_ms") or 0)
    starts = []
    for ev in scene.get("events") or []:
        at = ev.get("at") or {}
        if at.get("cue") in cue_ms:
            base = cue_ms[at["cue"]][0 if at.get("edge", "start") == "start" else 1]
            starts.append(base + int(ev.get("offset_ms") or 0) - start)
    return int(min(starts)) if starts else 0


def build(plan: dict, cues, look=None) -> dict:
    """plan dict + SRT cues (video.parsing blocks) -> explainer.json dict.
    Footers (a takeaway bar under a scene) are not drawn for a channel (look given) unless its
    "footers" setting is on: information appears where the narration says it, as titles,
    labels and points. Without a look (bare plan) footers are kept."""
    if (isinstance(plan, dict) and isinstance(look, dict) and isinstance(plan.get("scenes"), list)
            and str(look.get("footers") or "").strip().lower() != "on"):
        plan = dict(plan, scenes=[{k: v for k, v in x.items()
                                   if not (k == "footer" and not (isinstance(v, dict) and v.get("key")))}
                                  if isinstance(x, dict) else x
                                  for x in plan["scenes"]])
    if isinstance(plan, dict) and any(isinstance(x, dict) and ("start" in x or any(
            isinstance(i, dict) and i.get("at") for i in (x.get("items") or [])))
            for x in plan.get("scenes") or []):
        plan = resolve_anchors(plan, cues)
    if not isinstance(plan, dict) or not isinstance(plan.get("scenes"), list) or not plan["scenes"]:
        raise ExplainerError(["plan.json needs a non-empty scenes list."])
    cue_ms = {c.index: (int(round(c.start_seconds * 1000)), int(round(c.end_seconds * 1000)))
              for c in cues}
    plan = dict(plan, scenes=[dict(x, cue_range=list(x["cue_range"])) if isinstance(x, dict) and x.get("cue_range")
                              else x for x in plan["scenes"]])
    _hold_previous(plan["scenes"], cue_ms)
    scenes, errors = [], []
    prev_end = dict(cam_mod.OVERVIEW)
    cam_off = str((look or {}).get("camera") or "").strip().lower() == "off" if isinstance(look, dict) else False
    preset = font_styles.resolve(look)
    title_font = (preset or {}).get("title") or ""
    tscale = font_styles.title_scale(look)
    for idx, raw in enumerate(plan["scenes"]):
        if isinstance(raw, dict) and raw.get("layout") in REMOTION_LAYOUTS:
            try:
                scenes.append(_remotion_scene(raw, idx, cue_ms, plan.get("motion_style")))
            except ExplainerError as exc:
                errors.extend(exc.errors)
            continue
        try:
            if tscale != 1.0 and isinstance(raw, dict):
                raw = dict(raw, _title_scale=tscale)
            s, els = _scene_elements(raw, idx)
            if idx == 0:
                _lead_in(s, els, cue_ms)          # nothing before the first scene to hold
            pen = PEN_FACTOR.get(str((look or {}).get("pen_speed") or "normal"), 1.0)
            if pen != 1.0:
                for e in els:
                    e.dur = max(e.min, int(e.dur * pen))
            if title_font:
                for e in els:
                    if e.group == "title" and e.obj.get("type") == "handwriting":
                        e.obj["font"] = title_font      # marker headline (font style "video1")
            sched = _schedule(s, els, cue_ms)
        except ExplainerError as exc:
            errors.extend(exc.errors)
            continue
        scene = {
            "id": s["id"], "shot_id": f"S{idx + 1:02d}_01", "cue_range": list(s["cue_range"]),
            "objects": [e.obj for e in els],
            "events": _events(s, els, sched, cue_ms)}
        if s.get("base"):
            scene["base"] = str(s["base"])
        if not cam_off:                                  # a channel can switch every camera move off
            try:
                cam, prev_end = _camera_for(s, s.get("region"), prev_end)
            except ExplainerError as exc:
                errors.extend(exc.errors)
                continue
            if cam:
                scene["camera"] = cam
        scenes.append(scene)
    if errors:
        raise ExplainerError(errors)
    doc = {"schema_version": sc.SCHEMA_VERSION, "style": sc.STYLE_WHITEBOARD, "scenes": scenes,
           "canvas": {"width": 1920, "height": 1080}, "builder": BUILDER_VERSION}
    if plan.get("theme"):
        doc["theme"] = plan["theme"]
    if isinstance(plan.get("fonts"), dict):
        doc["fonts"] = plan["fonts"]
    return doc
