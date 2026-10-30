"""Mechanism scenes: drawings whose geometry is computed from real numbers.

Three layouts, all built from the same shape/handwriting primitives as the
other layouts, so they share the pen, the cache and Stop/resume:

  memory     a memory box (VRAM, RAM ...) filled by segments whose widths are
             proportional to their size. Scene: capacity, unit, items
             [{label, gb, at, color, grows_from}], optional allow_overflow,
             exact (hides the "illustrative" tag), capacity_label.
  precision  one bar per number format, widths proportional to the size that
             format needs. Scene: params_b (billions), items [{label, bits, at}].
             The size is computed (params x bits / 8), never typed.
  offload    the layer stack split between two memories. Scene: layers,
             items [{label, layers, at, note}] (two zones, in order).

The numbers are checked: segments must fit the box unless the scene says
allow_overflow, precision sizes are calculated, and the layer counts must add up.
"""
from __future__ import annotations

from .validate import ExplainerError

X0, X1 = 0.07, 0.93
W = X1 - X0
MAX_SEGMENTS = 4
STRETCH_MS = 900


def _fmt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _num(s, key, errors, sid, lo=0.0):
    v = s.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= lo:
        errors.append(f"Scene {sid}: {key} must be a number greater than {lo:g} (got {v!r}).")
        return None
    return float(v)


def _guard(errors):
    if errors:
        raise ExplainerError(errors)


def _cue(R, s, items, i):
    return R._item_cue(s, items, i)


# ---- memory ----------------------------------------------------------------------

def memory(R, s, sid, els, top):
    items, errors = s["items"], []
    cap = _num(s, "capacity", errors, sid)
    unit = str(s.get("unit") or "GB")
    if len(items) > MAX_SEGMENTS:
        errors.append(f"Scene {sid}: memory fits up to {MAX_SEGMENTS} segments (got {len(items)}).")
    sizes = []
    for k, it in enumerate(items):
        g = it.get("gb")
        if isinstance(g, bool) or not isinstance(g, (int, float)) or g <= 0:
            errors.append(f"Scene {sid}: segment {k + 1} ({it.get('label', '')}) needs gb > 0.")
            g = 0.0
        sizes.append(float(g))
        gf = it.get("grows_from")
        if gf is not None:
            if k != len(items) - 1:
                errors.append(f"Scene {sid}: only the last segment can grow (grows_from).")
            elif not isinstance(gf, (int, float)) or not 0 < gf < g:
                errors.append(f"Scene {sid}: grows_from must be between 0 and the segment size.")
        if len(it.get("label", "")) > 32:
            errors.append(f"Scene {sid}: segment label {it.get('label')!r} is over 32 characters.")
    _guard(errors)
    total = sum(sizes)
    over = total - cap
    if over > 1e-9 and not s.get("allow_overflow"):
        raise ExplainerError([
            f"Scene {sid}: the segments add up to {_fmt(total)} {unit} but the box holds "
            f"{_fmt(cap)} {unit}. Shrink them, or set allow_overflow to show it not fitting."])
    span = max(cap, total)
    sx = W / span
    bar_y, bar_h = top + 0.03, 0.15
    c0 = s["cue_range"][0]
    cap_label = s.get("capacity_label") or f"{_fmt(cap)} {unit}"
    els.append(R._text(f"{sid}_cap", cap_label, [X0, bar_y - 0.085, 0.4, 0.075], "heading", c0, "frame"))
    if not s.get("exact"):
        els.append(R._text(f"{sid}_ill", "illustrative numbers", [0.6, bar_y - 0.075, 0.33, 0.055],
                           "grey", c0, "frame"))
    els.append(R._shape(f"{sid}_box", "card", c0, "grey", "frame", dur=800,
                        box=[X0 - 0.006, bar_y - 0.012, cap * sx + 0.012, bar_h + 0.024]))
    x, rows = X0, []
    for k, (it, g) in enumerate(zip(items, sizes)):
        cue = _cue(R, s, items, k)
        col = it.get("color") or R.CYCLE[k % len(R.CYCLE)]
        w = g * sx
        beyond = x + w > X0 + cap * sx + 1e-9
        if beyond:
            col = "red"
        gid = f"i{k}"
        seg = R._shape(f"{sid}_seg{k}", "card", cue, col, gid, dur=700, filled=True,
                       box=[x + 0.002, bar_y, max(0.01, w - 0.004), bar_h])
        gf = it.get("grows_from")
        if gf:
            seg.extra.append({"action": "stretch", "from_amount": float(gf) / g, "amount": 1.0,
                              "duration_ms": STRETCH_MS})
        els.append(seg)
        if w >= 0.1:
            els.append(R._text(f"{sid}_val{k}", f"{_fmt(g)} {unit}", [x + 0.01, bar_y + bar_h * 0.28,
                                                                     w - 0.02, bar_h * 0.44],
                               "text", cue, gid))
        rows.append((cue, col, f"{it.get('label', '')} - {_fmt(g)} {unit}", gid))
        x += w
    if over > 1e-9:
        rows.append((rows[-1][0], "red", f"does not fit: {_fmt(over)} {unit} too much", "i%d" % (len(items) - 1)))
    elif cap - total > 1e-9:
        rows.append((rows[-1][0], "grey", f"free - {_fmt(cap - total)} {unit}", "i%d" % (len(items) - 1)))
    ly, pitch = bar_y + bar_h + 0.045, 0.065
    if ly + pitch * len(rows) > s["_bot"] + 0.005:
        raise ExplainerError([f"Scene {sid}: no room for the legend; use fewer segments or drop the footer."])
    for j, (cue, col, text, gid) in enumerate(rows):
        els.append(R._text(f"{sid}_lg{j}", text, [X0, ly + j * pitch, 0.86, pitch - 0.005], col, cue, gid))
    return els


# ---- precision -------------------------------------------------------------------

def precision(R, s, sid, els, top):
    items, errors = s["items"], []
    params = _num(s, "params_b", errors, sid)
    if not 1 <= len(items) <= 4:
        errors.append(f"Scene {sid}: precision fits 1-4 rows (got {len(items)}).")
    bits = []
    for k, it in enumerate(items):
        b = it.get("bits")
        if isinstance(b, bool) or not isinstance(b, (int, float)) or b <= 0:
            errors.append(f"Scene {sid}: row {k + 1} ({it.get('label', '')}) needs bits > 0.")
            b = 0
        bits.append(b)
    _guard(errors)
    gbs = [params * b / 8 for b in bits]
    mx = max(gbs)
    pitch = min(0.17, (s["_bot"] - top - (0 if s.get("footer") else 0.06)) / len(items))
    bar_h = pitch * 0.5
    bx, bmax = 0.24, 0.30
    for k, (it, b, g) in enumerate(zip(items, bits, gbs)):
        y = top + k * pitch
        cue = _cue(R, s, items, k)
        col = it.get("color") or R.CYCLE[k % len(R.CYCLE)]
        gid = f"i{k}"
        els.append(R._text(f"{sid}_nm{k}", it.get("label") or f"{_fmt(b)}-bit",
                           [X0, y, 0.16, bar_h], "text", cue, gid))
        w = max(0.02, bmax * g / mx)
        els.append(R._shape(f"{sid}_bar{k}", "card", cue, col, gid, dur=700, filled=True,
                            box=[bx, y, w, bar_h]))
        els.append(R._text(f"{sid}_gb{k}", f"{_fmt(g)} GB", [bx + w + 0.02, y, 0.2, bar_h], col, cue, gid))
        els.append(R._text(f"{sid}_calc{k}", f"{_fmt(params)}B x {_fmt(b)} bits / 8 = {_fmt(g)} GB",
                           [bx, y + bar_h + 0.005, 0.62, pitch * 0.34], "grey", cue, gid))
    if not s.get("footer"):
        els.append(R._text(f"{sid}_raw", "weights only - real files add some overhead",
                           [X0, 0.775, 0.8, 0.05], "grey", s["cue_range"][1], "footer"))
    return els


# ---- offload ---------------------------------------------------------------------

def offload(R, s, sid, els, top):
    items, errors = s["items"], []
    n = s.get("layers")
    if isinstance(n, bool) or not isinstance(n, int) or n < 2:
        errors.append(f"Scene {sid}: layers must be a whole number of at least 2 (got {n!r}).")
    if len(items) != 2:
        errors.append(f"Scene {sid}: offload needs exactly two zones (got {len(items)}).")
    _guard(errors)
    counts = []
    for k, it in enumerate(items):
        c = it.get("layers")
        if isinstance(c, bool) or not isinstance(c, int) or c < 1:
            errors.append(f"Scene {sid}: zone {k + 1} ({it.get('label', '')}) needs layers >= 1.")
            c = 0
        counts.append(c)
    _guard(errors)
    if sum(counts) != n:
        raise ExplainerError([f"Scene {sid}: the zones hold {sum(counts)} layers but layers is {n}."])
    c0 = s["cue_range"][0]
    flow = s.get("flow_label") or "each token passes every layer, in order"
    y_arrow = top + 0.02
    els.append(R._text(f"{sid}_flow", flow, [X0, y_arrow, W, 0.07], "grey", c0, "frame"))
    els.append(R._shape(f"{sid}_arrow", "arrow", c0, "grey", "frame", dur=700, strokes=2,
                        points=[[X0, y_arrow + 0.105], [X1, y_arrow + 0.105]], curve=0.04))
    zy, zh = y_arrow + 0.16, 0.2
    x, start = X0, 1
    for k, (it, c) in enumerate(zip(items, counts)):
        cue = _cue(R, s, items, k)
        col = it.get("color") or ("green", "orange")[k]
        gid = f"i{k}"
        w = W * c / n
        label = it.get("label", "")
        if len(label) * 0.02 > w - 0.02:
            raise ExplainerError([f"Scene {sid}: zone label {label!r} is too long for a zone of {c} of {n} layers."])
        els.append(R._shape(f"{sid}_zone{k}", "card", cue, col, gid, dur=800, filled=True,
                            box=[x + 0.003, zy, w - 0.006, zh]))
        els.append(R._text(f"{sid}_zl{k}", label, [x + 0.01, zy + zh * 0.28, w - 0.02, zh * 0.44],
                           "text", cue, gid))
        rng = f"layers {start}-{start + c - 1}" if c > 1 else f"layer {start}"
        els.append(R._text(f"{sid}_rg{k}", rng, [x, zy + zh + 0.03, w, 0.07], col, cue, gid))
        if it.get("note"):
            els.append(R._text(f"{sid}_nt{k}", it["note"], [x, zy + zh + 0.11, w, 0.06], "grey", cue, gid))
        x += w
        start += c
    if not s.get("footer"):
        els.append(R._text(f"{sid}_simp", "simplified split",
                           [X0, 0.775, 0.85, 0.05], "grey", s["cue_range"][1], "footer"))
    return els


BUILDERS = {"memory": memory, "precision": precision, "offload": offload}
