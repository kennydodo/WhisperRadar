"""Hand-drawn shapes for whiteboard/chalkboard scenes.

A `shape` object is generated geometry, not an asset: the scene file only names
a kind ("card", "oval", "arrow", ...) and a box or two points, so a script
generator never has to author coordinates. Every shape is drawn by the same
pen as icons and handwriting (arc-length progress, erase, glow), and every
shape is deterministic: the wobble is seeded from the object id.

Kinds and what they need:
  card       rounded box around `box`            (filled: true darkens inside)
  oval       loose ellipse around `box`
  underline  wavy line along the bottom of `box`
  highlight  wide marker sweep through `box`     (translucent)
  brace      curly brace filling `box`, tip toward `side` (right|left|top|bottom)
  arrow      curved arrow from points[0] to points[1]   (curve: -1..1)
  line       line from points[0] to points[1]            (dashed: true)
  check      tick mark in `box`
  cross      X mark in `box`
"""
from __future__ import annotations

import hashlib
import math
import random

from .strokes import Stroke, flatten_cubic

KINDS = ("card", "oval", "underline", "highlight", "brace", "arrow", "line",
         "check", "cross")
NEEDS_POINTS = ("arrow", "line")
SIDES = ("left", "right", "top", "bottom")


def seed_for(object_id: str) -> int:
    return int(hashlib.sha1(object_id.encode("utf-8")).hexdigest()[:8], 16)


# ---- helpers ---------------------------------------------------------------------

def _densify(pts, step):
    out = [pts[0]]
    for a, b in zip(pts, pts[1:]):
        n = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / step))
        for i in range(1, n + 1):
            t = i / n
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return out


def _wobble(pts, amp, rng, step=7.0):
    """Offset a polyline along its normal by smooth low-frequency noise, so
    lines look hand-held rather than ruled."""
    if amp <= 0 or len(pts) < 2 or getattr(rng, "straight", False):
        return list(pts)
    pts = _densify(pts, step)
    f1, f2 = rng.uniform(0.012, 0.022), rng.uniform(0.035, 0.055)
    p1, p2 = rng.uniform(0, 6.28), rng.uniform(0, 6.28)
    out, s = [], 0.0
    for i, p in enumerate(pts):
        if i:
            s += math.hypot(p[0] - pts[i - 1][0], p[1] - pts[i - 1][1])
        a = pts[max(0, i - 1)]
        b = pts[min(len(pts) - 1, i + 1)]
        tx, ty = b[0] - a[0], b[1] - a[1]
        n = math.hypot(tx, ty) or 1.0
        nx, ny = -ty / n, tx / n
        d = amp * (math.sin(s * f1 + p1) + 0.45 * math.sin(s * f2 + p2))
        out.append((p[0] + nx * d, p[1] + ny * d))
    return out


def _rounded_rect(x, y, w, h, r):
    """Clockwise outline starting just after the top-left corner."""
    r = max(1.0, min(r, w / 2, h / 2))
    pts = [(x + r, y), (x + w - r, y)]

    def arc(cx, cy, a0, a1):
        for i in range(1, 9):
            a = math.radians(a0 + (a1 - a0) * i / 8)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    arc(x + w - r, y + r, -90, 0)
    pts.append((x + w, y + h - r))
    arc(x + w - r, y + h - r, 0, 90)
    pts.append((x + r, y + h))
    arc(x + r, y + h - r, 90, 180)
    pts.append((x, y + r))
    arc(x + r, y + r, 180, 270)
    return pts


def _stroke(pts, width, color=""):
    return Stroke(points=[(float(a), float(b)) for a, b in pts],
                  width=width, color=color)


# ---- generators --------------------------------------------------------------------

def _card(box, rng, width):
    x, y, w, h = box
    pts = _rounded_rect(x, y, w, h, min(w, h) * 0.16)
    pts = pts + pts[:3]                              # the pen overshoots its start
    # the loop closes a few px off, like a real hand
    pts = [(px + rng.uniform(-0.6, 0.6), py) for px, py in pts]
    return [_stroke(_wobble(pts, max(1.2, width * 0.35), rng), width)]


def _oval(box, rng, width):
    x, y, w, h = box
    cx, cy, rx, ry = x + w / 2, y + h / 2, w / 2, h / 2
    start = rng.uniform(-130, -80)
    pts = []
    n = 72
    for i in range(n + 1):
        a = math.radians(start + 380 * i / n)
        grow = 1.0 + 0.04 * i / n                    # the loop does not quite close
        pts.append((cx + rx * grow * math.cos(a), cy + ry * grow * math.sin(a)))
    return [_stroke(_wobble(pts, max(1.0, width * 0.3), rng), width)]


def _underline(box, rng, width):
    x, y, w, h = box
    yy = y + h
    pts = [(x, yy + rng.uniform(-2, 2)),
           (x + w * 0.5, yy + rng.uniform(-3, 3)),
           (x + w, yy + rng.uniform(-6, 0))]
    return [_stroke(_wobble(pts, max(1.0, width * 0.4), rng), width)]


def _highlight(box, rng, width):
    x, y, w, h = box
    pts = [(x, y + h * 0.5 + rng.uniform(-2, 2)),
           (x + w, y + h * 0.5 + rng.uniform(-3, 3))]
    return [_stroke(_wobble(pts, 1.5, rng), max(width * 2.0, h * 0.8))]


def _brace(box, rng, width, side):
    x, y, w, h = box
    # a "}" in a unit box; `side` is the way the tip points
    if side in ("left", "right"):
        k = 1.0 if side == "right" else -1.0
        x0 = x if side == "right" else x + w

        def P(u, v):
            return (x0 + k * u * w, y + v * h)
    else:
        k = 1.0 if side == "bottom" else -1.0
        y0 = y if side == "bottom" else y + h

        def P(u, v):
            return (x + v * w, y0 + k * u * h)
    segs = [(P(0, 0), P(0.45, 0), P(0.45, 0.1), P(0.45, 0.25)),
            (P(0.45, 0.25), P(0.45, 0.42), P(0.55, 0.47), P(1, 0.5)),
            (P(1, 0.5), P(0.55, 0.53), P(0.45, 0.58), P(0.45, 0.75)),
            (P(0.45, 0.75), P(0.45, 0.9), P(0.45, 1), P(0, 1))]
    pts = []
    for s in segs:
        part = flatten_cubic(*s, tol=0.6)
        pts.extend(part if not pts else part[1:])
    return [_stroke(_wobble(pts, max(1.0, width * 0.25), rng), width)]


def _bez(p0, p1, p2, t):
    u = 1 - t
    return (u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
            u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1])


def _arrow(p_from, p_to, rng, width, curve):
    (x1, y1), (x2, y2) = p_from, p_to
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / length, dx / length
    ctrl = ((x1 + x2) / 2 + nx * length * curve,
            (y1 + y2) / 2 + ny * length * curve)
    pts = [_bez(p_from, ctrl, p_to, i / 30) for i in range(31)]
    shaft = _stroke(_wobble(pts, max(0.8, width * 0.25), rng), width)
    # head: open V, oriented along the curve's end tangent
    tx, ty = x2 - ctrl[0], y2 - ctrl[1]
    tn = math.hypot(tx, ty) or 1.0
    tx, ty = tx / tn, ty / tn
    hl = max(width * 4.5, min(length * 0.3, width * 8))
    spread = math.radians(28)
    wings = []
    for sgn in (1, -1):
        a = math.atan2(-ty, -tx) + sgn * spread
        wings.append((x2 + hl * math.cos(a), y2 + hl * math.sin(a)))
    head = _stroke([wings[0], (x2, y2), wings[1]], width)
    return [shaft, head]


def _line(p_from, p_to, rng, width, dashed):
    (x1, y1), (x2, y2) = p_from, p_to
    if not dashed:
        return [_stroke(_wobble([p_from, p_to], max(0.8, width * 0.25), rng), width)]
    length = math.hypot(x2 - x1, y2 - y1) or 1.0
    dash, gap = width * 3.2, width * 2.6
    out, pos = [], 0.0
    while pos < length:
        end = min(length, pos + dash)
        a = pos / length
        b = end / length
        out.append(_stroke([(x1 + (x2 - x1) * a, y1 + (y2 - y1) * a),
                            (x1 + (x2 - x1) * b, y1 + (y2 - y1) * b)], width))
        pos = end + gap
    return out


def _check(box, rng, width):
    x, y, w, h = box
    pts = [(x + w * 0.02, y + h * 0.55), (x + w * 0.36, y + h * 0.95),
           (x + w * 0.98, y + h * 0.02)]
    return [_stroke(_wobble(_densify(pts, 6), max(0.8, width * 0.2), rng), width)]


def _cross(box, rng, width):
    x, y, w, h = box
    a = [(x + w * 0.05, y + h * 0.05), (x + w * 0.95, y + h * 0.95)]
    b = [(x + w * 0.95, y + h * 0.05), (x + w * 0.05, y + h * 0.95)]
    return [_stroke(_wobble(a, 1.0, rng), width), _stroke(_wobble(b, 1.0, rng), width)]


def shape_strokes(spec, box_px, points_px, width: float, straight: bool = False):
    """Pixel-space strokes for a shape spec. `width` is the pen width at the
    1920 reference size (the renderer scales it). straight=True is the clean
    graphic look: no hand wobble, arrows without a bend."""
    rng = random.Random(seed_for(spec.id))
    rng.straight = straight
    kind = spec.kind
    if kind == "card":
        return _card(box_px, rng, width)
    if kind == "oval":
        return _oval(box_px, rng, width)
    if kind == "underline":
        return _underline(box_px, rng, width)
    if kind == "highlight":
        return _highlight(box_px, rng, width)
    if kind == "brace":
        return _brace(box_px, rng, width, spec.side or "right")
    if kind == "arrow":
        return _arrow(points_px[0], points_px[1], rng, width, 0.0 if straight else spec.curve)
    if kind == "line":
        return _line(points_px[0], points_px[1], rng, width, spec.dashed)
    if kind == "check":
        return _check(box_px, rng, width)
    if kind == "cross":
        return _cross(box_px, rng, width)
    raise ValueError(f"unknown shape kind {kind!r}")


def stroke_budget(spec) -> int:
    """Strokes the validator counts toward the minimum draw time. Dashes are
    one pen move, so a dashed line counts as one."""
    return {"arrow": 2, "cross": 2}.get(spec.kind, 1)
