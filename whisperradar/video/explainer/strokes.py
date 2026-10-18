"""Ordered-stroke geometry: curves -> flattened points -> arc-length tables.

A stroke is an ordered polyline (already flattened from its path commands)
plus style. Draw progress is measured by ARC LENGTH (not point count), so a
stroke draws at constant pen speed regardless of control-point density.

The evaluator can ask for the visible geometry at ANY frame without
replaying earlier frames: partial_stroke() is a pure function of progress.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


# ---- flattening -------------------------------------------------------------------

def flatten_cubic(p0, p1, p2, p3, tol=0.4):
    """Cubic bezier -> polyline points (endpoint included), error <= tol px."""
    # adaptive subdivision by flatness (exact recursive criterion)
    def flat_enough(a, b, c, d):
        # control-point distance from the chord
        ux = 3 * b[0] - 2 * a[0] - d[0]
        uy = 3 * b[1] - 2 * a[1] - d[1]
        vx = 3 * c[0] - 2 * d[0] - a[0]
        vy = 3 * c[1] - 2 * d[1] - a[1]
        return max(ux * ux + uy * uy, vx * vx + vy * vy) <= 16 * tol * tol

    out = [p0]

    def emit(a, b, c, dd):
        if flat_enough(a, b, c, dd):
            out.append(dd)
            return
        mid1 = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        mid2 = ((b[0] + c[0]) / 2, (b[1] + c[1]) / 2)
        mid3 = ((c[0] + dd[0]) / 2, (c[1] + dd[1]) / 2)
        mid12 = ((mid1[0] + mid2[0]) / 2, (mid1[1] + mid2[1]) / 2)
        mid23 = ((mid2[0] + mid3[0]) / 2, (mid2[1] + mid3[1]) / 2)
        mid = ((mid12[0] + mid23[0]) / 2, (mid12[1] + mid23[1]) / 2)
        emit(a, mid1, mid12, mid)
        emit(mid, mid23, mid3, dd)

    emit(p0, p1, p2, p3)
    return out


def flatten_quadratic(p0, p1, p2, tol=0.4):
    """Quadratic bezier -> polyline (lifted to a cubic)."""
    return flatten_cubic(p0,
                         (p0[0] + 2 / 3 * (p1[0] - p0[0]),
                          p0[1] + 2 / 3 * (p1[1] - p0[1])),
                         (p2[0] + 2 / 3 * (p1[0] - p2[0]),
                          p2[1] + 2 / 3 * (p1[1] - p2[1])),
                         p2, tol)


def arc_lengths(points):
    """Cumulative arc length at each point; [0] = 0, [-1] = total."""
    out = [0.0]
    total = 0.0
    for i in range(1, len(points)):
        dx = points[i][0] - points[i - 1][0]
        dy = points[i][1] - points[i - 1][1]
        total += math.hypot(dx, dy)
        out.append(total)
    return out


@dataclass
class Stroke:
    """One pen-down stroke: flattened polyline + style + authored weight."""
    points: list                      # [(x, y), ...] in asset-local units
    width: float = 3.0                # stroke width in local units
    color: str = ""                   # "" = role default (ink/accent)
    fill: bool = False                # fill-after-outline
    weight: float = 1.0               # relative duration weight vs siblings
    pause_after: float = 0.0          # relative pause weight after this stroke
    _arclen: list = field(default_factory=list, repr=False)

    def __post_init__(self):
        if len(self.points) < 2:
            # a dot: tiny 2-point stroke so progress math stays uniform
            x, y = (self.points[0] if self.points else (0.0, 0.0))
            self.points = [(x, y), (x + 0.01, y)]
        if not self._arclen:
            self._arclen = arc_lengths(self.points)

    @property
    def length(self) -> float:
        return self._arclen[-1]

    def partial(self, progress: float):
        """Points along the stroke for arc-length progress 0..1 (pure)."""
        if progress >= 1.0:
            return list(self.points)
        if progress <= 0.0:
            return self.points[:1]
        target = self._arclen[-1] * progress
        out = [self.points[0]]
        for i in range(1, len(self.points)):
            seg_len = self._arclen[i] - self._arclen[i - 1]
            if self._arclen[i] >= target:
                if seg_len <= 0:
                    continue
                t = (target - self._arclen[i - 1]) / seg_len
                a, b = self.points[i - 1], self.points[i]
                out.append((a[0] + (b[0] - a[0]) * t,
                            a[1] + (b[1] - a[1]) * t))
                return out
            out.append(self.points[i])
        return out

    def tip(self, progress: float):
        """Pen-tip position at progress (last point of the partial stroke)."""
        pts = self.partial(progress)
        return pts[-1]


def stroke_from_path_commands(cmds, width=3.0, color="", fill=False,
                              weight=1.0, pause_after=0.0, tol=0.4) -> Stroke:
    """cmds: list like ["M", x, y, "L", x, y, "C", x1,y1,x2,y2,x,y, "Q", ...].
    Builds one flattened polyline (pen never lifts inside a stroke)."""
    points = []
    i = 0
    cur = (0.0, 0.0)
    while i < len(cmds):
        c = cmds[i]
        if c == "M":
            cur = (float(cmds[i + 1]), float(cmds[i + 2]))
            if not points:
                points.append(cur)
            i += 3
        elif c == "L":
            nxt = (float(cmds[i + 1]), float(cmds[i + 2]))
            points.extend(_seg(cur, nxt, tol))
            cur = nxt
            i += 3
        elif c == "C":
            pts = flatten_cubic(cur,
                                (float(cmds[i + 1]), float(cmds[i + 2])),
                                (float(cmds[i + 3]), float(cmds[i + 4])),
                                (float(cmds[i + 5]), float(cmds[i + 6])), tol)
            points.extend(pts[1:])
            cur = pts[-1]
            i += 7
        elif c == "Q":
            pts = flatten_quadratic(cur,
                                    (float(cmds[i + 1]), float(cmds[i + 2])),
                                    (float(cmds[i + 3]), float(cmds[i + 4])), tol)
            points.extend(pts[1:])
            cur = pts[-1]
            i += 5
        elif c == "Z":
            if points and points[0] != cur:
                points.append(points[0])
            cur = points[0] if points else (0.0, 0.0)
            i += 1
        else:
            raise ValueError(f"unsupported path command {c!r}")
    return Stroke(points=points, width=width, color=color, fill=fill,
                  weight=weight, pause_after=pause_after)


def _seg(a, b, tol):
    """Intermediate points for a straight segment (none needed; endpoints
    handled by the caller) - kept for symmetry with curve flattening."""
    return [b]


# ---- stroke groups ------------------------------------------------------------------

@dataclass
class StrokeAsset:
    """A reusable drawing: ordered strokes in a local viewBox."""
    strokes: list                     # Stroke, in authored draw order
    viewBox: tuple                    # (w, h) local units
    name: str = ""

    @property
    def total_weight(self) -> float:
        return sum(s.weight for s in self.strokes)

    @property
    def pause_weight(self) -> float:
        return sum(s.pause_after for s in self.strokes)


def schedule(asset: StrokeAsset, start_frame: int, duration_frames: int,
             pause_fraction: float = 0.12):
    """-> list of (stroke, stroke_start, stroke_end, pause_end) in scene-local
    frames. Durations are budgeted across stroke LENGTHS and pauses; a
    zero/negative total duration raises (the validator rejects overscheduled
    art earlier, this is the last guard)."""
    if duration_frames <= 0:
        raise ValueError("draw duration must be positive")
    draw_w = sum(s.weight * max(s.length, 1.0) for s in asset.strokes)
    pause_w = asset.pause_weight * pause_fraction
    total_w = draw_w + pause_w
    if total_w <= 0:
        raise ValueError("asset has no drawable strokes")
    out = []
    cursor = float(start_frame)
    unit = duration_frames / total_w
    for s in asset.strokes:
        dur = s.weight * max(s.length, 1.0) * unit
        st = cursor
        en = st + dur
        cursor = en + s.pause_after * pause_fraction * unit
        out.append((s, st, en, cursor))
    return out
