"""Picture vocabulary for scene plans: the parts the reference explainers are built from.

  cards    2-4 cards side by side. Each item: label (the card title), icon, color, at,
           rows [text | {text, at}] (1-4 short lines inside the card).
  group    a dashed container that holds 2-5 things. Scene: caption (what the container
           is, e.g. "Production"), connect arrow|dashed|none; items as in row.
  chips    2-12 short pills (tools, terms, models, tags): items [{label, color, at}].
  callout  a real example, quote or source line in a box. One item: label (the text,
           up to ~160 characters), note (where it comes from), icon; scene tag (default
           "real example").

Built from the same shape / handwriting primitives as every other layout.
"""
from __future__ import annotations

import textwrap

from .validate import ExplainerError

X0, X1 = 0.06, 0.94


def _wrap(text, width, font):
    """(text with line breaks, lines, box height): the text wrapped so that, written at the same
    font size (a share of the canvas height) everywhere, it fits `width` (a share of the width).
    Equal sizes across a scene instead of each line shrinking to its own box."""
    chars = max(4, int(width * 1920 / (0.6 * font * 1080)))
    lines = textwrap.wrap(str(text), chars) or [""]
    return "\n".join(lines), len(lines), len(lines) * font * 1.4


def _need(s, sid, lo, hi, what):
    n = len(s.get("items") or [])
    if not lo <= n <= hi:
        raise ExplainerError([f"Scene {sid}: {s.get('layout')} fits {lo}-{hi} {what} (got {n}); "
                              "split it across two scenes."])


def _row_cue(R, s, card_cue, row, j, n):
    """Cue of a card row: its own "at" (resolved to cue) or the card's."""
    if isinstance(row, dict) and row.get("cue") is not None:
        return int(row["cue"])
    return card_cue


def cards(R, s, sid, els, top):
    _need(s, sid, 2, 4, "cards")
    items, n = s["items"], len(s["items"])
    gap = 0.03
    cw = ((X1 - X0) - gap * (n - 1)) / n
    bot = s["_bot"]
    T_FONT, R_FONT = 0.066, 0.05
    for _try in range(8):                 # smaller type (same in every card) until the cards fit the frame
        rows_of, heads = [], []
        for it in items:
            rows = [{"text": r} if isinstance(r, str) else dict(r) for r in (it.get("rows") or [])][:4]
            rows_of.append([(r,) + _wrap(r.get("text", ""), cw - 0.045, R_FONT) for r in rows])
            heads.append(_wrap(it.get("label", ""), cw - 0.04, T_FONT))
        head_h = max(0.0, max(h[2] for h in heads))
        need = 0.03 + 0.1 + head_h + 0.02 + max((sum(r[3] + 0.02 for r in rows) for rows in rows_of), default=0) + 0.02
        if need <= bot - top or T_FONT < 0.04:
            break
        T_FONT, R_FONT = T_FONT * 0.92, R_FONT * 0.92
    ch = min(bot - top, max(need, 0.4))
    y0 = top + max(0.0, (bot - top - ch) / 2)
    for k, (it, rows) in enumerate(zip(items, rows_of)):
        x = X0 + k * (cw + gap)
        cue = R._item_cue(s, items, k)
        col = R._color(it, k)
        g = f"i{k}"
        els.append(R._shape(f"{sid}_card{k}", "card", cue, col, g, dur=900, filled=True,
                            box=[x, y0, cw, ch]))
        y = y0 + 0.03
        if it.get("icon"):
            ih = 0.1
            els.append(R._icon(f"{sid}_ic{k}", R._asset(it["icon"]), [x + 0.02, y, ih * R.WN, ih], col, cue, g, sid))
            y += ih + 0.015
        txt, _n, h = heads[k]
        els.append(R._text(f"{sid}_hd{k}", txt, [x + 0.02, y, cw - 0.04, h], col, cue, g))
        y += h + 0.025
        for j, (row, rtxt, _rn, rh) in enumerate(rows):
            rc = _row_cue(R, s, cue, row, j, len(rows))
            els.append(R._text(f"{sid}_rw{k}_{j}", rtxt, [x + 0.022, y, cw - 0.045, rh], "text", rc, f"{g}r{j}"))
            y += rh + 0.02
    return els


def group(R, s, sid, els, top):
    _need(s, sid, 2, 5, "things")
    bot = s["_bot"]
    c0 = s["cue_range"][0]
    els.append(R._shape(f"{sid}_frame", "frame", c0, "grey", "frame", dur=1100,
                        box=[0.04, top - 0.01, 0.92, bot - top + 0.0]))
    cap = str(s.get("caption") or "").strip()
    if cap:
        els.append(R._text(f"{sid}_cap", cap, [0.07, top + 0.005, 0.6, 0.085], "grey", c0, "frame"))
    inner = dict(s, _bot=bot - 0.03)
    R._row(inner, sid, els, top + (0.1 if cap else 0.04))
    return els


def chips(R, s, sid, els, top):
    _need(s, sid, 2, 12, "chips")
    items = s["items"]
    ch, hgap, vgap = 0.1, 0.022, 0.05
    widths = [min(0.5, 0.0245 * len(str(it.get("label", ""))) + 0.06) for it in items]
    lines, cur, used = [], [], 0.0
    for k, w in enumerate(widths):
        if cur and used + w > 0.88:
            lines.append(cur)
            cur, used = [], 0.0
        cur.append(k)
        used += w + hgap
    lines.append(cur)
    bot = s["_bot"]
    block = len(lines) * ch + (len(lines) - 1) * vgap
    y = top + max(0.0, (bot - top - block) / 2)
    for line in lines:
        total = sum(widths[k] for k in line) + hgap * (len(line) - 1)
        x = 0.5 - total / 2
        for k in line:
            it = items[k]
            cue = R._item_cue(s, items, k)
            col = R._color(it, k)
            g = f"i{k}"
            els.append(R._shape(f"{sid}_pill{k}", "pill", cue, col, g, dur=600, filled=True,
                                box=[x, y, widths[k], ch]))
            els.append(R._text(f"{sid}_lb{k}", it.get("label", ""),
                               [x + 0.02, y + 0.008, widths[k] - 0.04, ch - 0.016], "text", cue, g))
            x += widths[k] + hgap
        y += ch + vgap
    return els


def callout(R, s, sid, els, top):
    _need(s, sid, 1, 1, "item")
    it = s["items"][0]
    cue = R._item_cue(s, s["items"], 0)
    col = R._color(it, 0)
    bot = s["_bot"]
    note = it.get("note")
    txt, _n, body_h = _wrap(it.get("label", ""), 0.76, 0.068)
    h = min(bot - top - 0.02, max(0.34, body_h + 0.13 + 0.05 + (0.1 if note else 0.03)))
    y0 = top + max(0.0, (bot - top - h) / 2)
    els.append(R._shape(f"{sid}_box", "card", cue, col, "i0", dur=900, filled=True, box=[0.09, y0, 0.82, h]))
    els.append(R._text(f"{sid}_tag", str(s.get("tag") or "real example"), [0.12, y0 + 0.03, 0.4, 0.075],
                       "orange", cue, "i0"))
    els.append(R._text(f"{sid}_lb0", txt, [0.12, y0 + 0.13, 0.76, body_h], "text", cue, "i0"))
    if note:
        els.append(R._text(f"{sid}_nt0", str(note), [0.12, y0 + h - 0.105, 0.76, 0.075], "grey", cue, "i0"))
    return els


BUILDERS = {"cards": cards, "group": group, "chips": chips, "callout": callout}
LAYOUTS = tuple(BUILDERS)
