"""Build whisperradar/video/explainer/icons/lucide.json from the lucide-static
npm package (ISC license).

    npm install lucide-static@<version>          (anywhere)
    python scripts/build_lucide_library.py <path>/node_modules/lucide-static

Every Lucide icon is a 24x24 line drawing. Each SVG element becomes one pen
stroke in the explainer's own path commands (M/L/C only: arcs, rects, circles,
ellipses, lines and polygons are converted), scaled to the engine's 100x100
asset box and rounded to 0.1. The result is checked into the repo, so the
engine needs neither Node nor svgelements at run time; this script is only for
refreshing the set.

Needs: pip install svgelements
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from svgelements import (Arc, Circle, Close, CubicBezier, Ellipse, Line, Move,
                         Polygon, Polyline, QuadraticBezier, Rect, SimpleLine)
from svgelements import Path as SvgPath

SCALE = 100.0 / 24.0
OUT = Path(__file__).resolve().parents[1] / "whisperradar" / "video" / "explainer" / "icons"


def _num(v: float) -> str:
    s = f"{v * SCALE:.1f}"
    return s[:-2] if s.endswith(".0") else s


def _pt(p) -> str:
    return f"{_num(p.x)} {_num(p.y)}"


def _shape(tag: str, a: dict):
    f = lambda k, d=0.0: float(a.get(k, d))  # noqa: E731
    if tag == "path":
        return SvgPath(a["d"])
    if tag == "circle":
        return SvgPath(Circle(f("cx"), f("cy"), f("r")))
    if tag == "ellipse":
        return SvgPath(Ellipse(f("cx"), f("cy"), f("rx"), f("ry")))
    if tag == "rect":
        rx = f("rx", a.get("ry", 0.0))
        ry = f("ry", rx)
        return SvgPath(Rect(f("x"), f("y"), f("width"), f("height"), rx, ry))
    if tag == "line":
        return SvgPath(SimpleLine(f("x1"), f("y1"), f("x2"), f("y2")))
    if tag == "polyline":
        return SvgPath(Polyline(a["points"]))
    if tag == "polygon":
        return SvgPath(Polygon(a["points"]))
    raise ValueError(f"unsupported element {tag}")


def _draw(seg) -> list[str]:
    if isinstance(seg, Line):
        return ["L " + _pt(seg.end)]
    if isinstance(seg, QuadraticBezier):
        return [f"Q {_pt(seg.control)} {_pt(seg.end)}"]
    if isinstance(seg, CubicBezier):
        return [f"C {_pt(seg.control1)} {_pt(seg.control2)} {_pt(seg.end)}"]
    if isinstance(seg, Arc):
        return [f"C {_pt(c.control1)} {_pt(c.control2)} {_pt(c.end)}"
                for c in seg.as_cubic_curves()]
    raise ValueError(f"unsupported segment {type(seg).__name__}")


def strokes_for(tag: str, attrs: dict) -> list[str]:
    """One compact path string ("M x y L x y C ...") per pen-down stroke."""
    out: list[str] = []
    cur: list[str] = []
    start = None

    def flush():
        nonlocal cur
        if len(cur) == 1 and cur[0].startswith("M "):
            cur = []                                    # a lone move draws nothing
        if cur:
            # a "dot" (M a b L a b) has zero length and would never be drawn:
            # give it a hair of length so the round pen caps show a dot
            if len(cur) == 2 and cur[0][2:] == cur[1][2:]:
                x, y = cur[0][2:].split()
                cur[1] = f"L {float(x) + 0.1:g} {y}"
            out.append(" ".join(cur))
        cur = []

    for seg in _shape(tag, attrs).segments():
        if isinstance(seg, Move):
            flush()
            start = seg.end
            cur = ["M " + _pt(seg.end)]
        elif isinstance(seg, Close):
            if cur and start is not None and seg.start != start:
                cur.append("L " + _pt(start))
            flush()
        else:
            if not cur:                       # drawing resumed after a close
                cur = ["M " + _pt(seg.start)]
                start = start if start is not None else seg.start
            cur.extend(_draw(seg))
    flush()
    # a closed rect/circle begins with M then curves back to its start: fine.
    return out


def main(pkg: Path) -> None:
    nodes = json.loads((pkg / "icon-nodes.json").read_text(encoding="utf-8"))
    tags = json.loads((pkg / "tags.json").read_text(encoding="utf-8"))
    version = json.loads((pkg / "package.json").read_text(encoding="utf-8"))["version"]
    icons = {}
    for name in sorted(nodes):
        strokes: list[str] = []
        for tag, attrs in nodes[name]:
            strokes += strokes_for(tag, attrs)
        if not strokes:
            raise SystemExit(f"{name}: no strokes")
        icons[name] = {"tags": tags.get(name, []), "strokes": strokes}
    OUT.mkdir(parents=True, exist_ok=True)
    data = {"source": "lucide-static", "version": version, "license": "ISC",
            "viewBox": [100, 100], "icons": icons}
    (OUT / "lucide.json").write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    shutil_license = (pkg / "LICENSE").read_text(encoding="utf-8")
    (OUT / "LICENSE-lucide.txt").write_text(shutil_license, encoding="utf-8")
    size = (OUT / "lucide.json").stat().st_size
    print(f"{len(icons)} icons, lucide-static {version}, {size / 1024:.0f} KB -> {OUT}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]))
