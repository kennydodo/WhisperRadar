"""Built-in stroke library: simple recognizable illustrations as ordered strokes.

All assets are authored in-repo (viewBox 100x100, y down). Provenance: drawn
for this project, no external licensing. An object references them as
"asset": "builtin:<name>" (a project-relative JSON file with the same schema
is equally valid). Authoring guide: docs/explainer/user-guide.md.
"""
from __future__ import annotations

import json
import os

from .strokes import Stroke, StrokeAsset, stroke_from_path_commands

# name -> (path_commands list per stroke, width, weight, pause_after, fill)
_LIBRARY = {}


def _add(name, strokes_spec, viewBox=(100, 100)):
    _LIBRARY[name] = (strokes_spec, viewBox)


INK = ""  # role default

# person: head, body, arms, legs
_add("person", [
    (["M", 50, 10, "C", 58, 10, 58, 24, 50, 24, "C", 42, 24, 42, 10, 50, 10], 5, 1.0, 0.3, False),
    (["M", 50, 26, "L", 50, 58], 5, 1.0, 0.1, False),
    (["M", 32, 36, "L", 68, 36], 5, 0.8, 0.2, False),
    (["M", 50, 58, "L", 36, 90], 5, 0.8, 0.15, False),
    (["M", 50, 58, "L", 64, 90], 5, 0.8, 0.0, False),
])

# house: walls, roof, door
_add("house", [
    (["M", 18, 46, "L", 50, 16, "L", 82, 46], 5, 1.0, 0.25, False),
    (["M", 24, 44, "L", 24, 84, "L", 76, 84, "L", 76, 44], 5, 1.0, 0.2, False),
    (["M", 44, 84, "L", 44, 62, "L", 58, 62, "L", 58, 84], 4, 0.7, 0.0, False),
])

# bank: base, pillars, roof, sign
_add("bank", [
    (["M", 14, 84, "L", 86, 84], 5, 0.9, 0.1, False),
    (["M", 18, 40, "L", 18, 78], 5, 0.8, 0.05, False),
    (["M", 42, 40, "L", 42, 78], 5, 0.8, 0.05, False),
    (["M", 66, 40, "L", 66, 78], 5, 0.8, 0.05, False),
    (["M", 12, 40, "L", 50, 16, "L", 88, 40, "L", 12, 40], 5, 1.0, 0.2, False),
])

# coin stack: three ellipses stacked
_add("coin_stack", [
    (["M", 26, 70, "C", 26, 62, 74, 62, 74, 70, "C", 74, 78, 26, 78, 26, 70], 5, 1.0, 0.2, False),
    (["M", 26, 58, "C", 26, 50, 74, 50, 74, 58, "C", 74, 66, 26, 66, 26, 58], 5, 0.9, 0.2, False),
    (["M", 26, 46, "C", 26, 38, 74, 38, 74, 46, "C", 74, 54, 26, 54, 26, 46], 5, 0.8, 0.0, False),
])

# light bulb: bulb, filament, base
_add("bulb", [
    (["M", 38, 58, "C", 22, 46, 24, 22, 50, 18,
      "C", 76, 22, 78, 46, 62, 58, "L", 62, 66, "L", 38, 66, "L", 38, 58], 5, 1.0, 0.2, False),
    (["M", 40, 72, "L", 60, 72], 4, 0.7, 0.1, False),
    (["M", 42, 80, "L", 58, 80], 4, 0.7, 0.1, False),
    (["M", 44, 46, "C", 48, 38, 52, 38, 56, 46], 4, 0.6, 0.0, False),
])

# calendar: frame, rings, header line, marks
_add("calendar", [
    (["M", 16, 26, "L", 16, 84, "L", 84, 84, "L", 84, 26, "L", 16, 26], 5, 1.0, 0.2, False),
    (["M", 16, 42, "L", 84, 42], 4, 0.7, 0.15, False),
    (["M", 34, 16, "L", 34, 32], 4, 0.7, 0.1, False),
    (["M", 66, 16, "L", 66, 32], 4, 0.7, 0.15, False),
    (["M", 30, 56, "L", 36, 62, "L", 46, 50], 4, 0.7, 0.0, False),
])

# folder: back, tab, front
_add("folder", [
    (["M", 14, 30, "L", 40, 30, "L", 48, 40, "L", 86, 40, "L", 86, 78, "L", 14, 78, "L", 14, 30],
     5, 1.0, 0.1, False),
    (["M", 22, 52, "L", 62, 52], 4, 0.6, 0.0, False),
])

# leaf: outline + vein
_add("leaf", [
    (["M", 50, 12, "C", 78, 26, 84, 62, 50, 88,
      "C", 16, 62, 22, 26, 50, 12], 5, 1.0, 0.2, False),
    (["M", 50, 20, "L", 50, 84], 4, 0.7, 0.1, False),
    (["M", 50, 44, "L", 66, 34], 3, 0.5, 0.05, False),
    (["M", 50, 60, "L", 34, 50], 3, 0.5, 0.0, False),
])

# tree: trunk + crown
_add("tree", [
    (["M", 46, 88, "L", 46, 56, "L", 54, 56, "L", 54, 88], 5, 0.8, 0.2, False),
    (["M", 50, 12, "C", 74, 12, 84, 30, 74, 44,
      "C", 84, 52, 72, 62, 60, 58,
      "C", 52, 66, 34, 64, 30, 54,
      "C", 16, 48, 22, 24, 50, 12], 5, 1.0, 0.0, False),
])

# wrench: simple tool
_add("wrench", [
    (["M", 30, 14, "C", 18, 18, 14, 34, 26, 40, "L", 62, 78, "C", 68, 84, 78, 80, 78, 72,
      "C", 78, 68, 74, 64, 70, 62, "L", 38, 28, "C", 34, 18, 38, 12, 30, 14], 5, 1.0, 0.0, False),
])

# piggy bank: body, snout, ear, legs, slot
_add("piggy_bank", [
    (["M", 26, 48, "C", 26, 30, 48, 20, 64, 26,
      "C", 80, 32, 84, 48, 80, 60, "L", 80, 74, "L", 70, 74, "L", 70, 68,
      "C", 60, 72, 46, 72, 36, 68, "L", 36, 74, "L", 26, 74, "L", 26, 60,
      "C", 18, 58, 18, 50, 26, 48], 5, 1.0, 0.2, False),
    (["M", 82, 44, "C", 90, 46, 90, 54, 82, 56], 4, 0.6, 0.15, False),
    (["M", 48, 32, "L", 60, 32], 4, 0.6, 0.1, False),
])

# upward chart line + axes (money grows)
_add("chart_up", [
    (["M", 18, 14, "L", 18, 84, "L", 86, 84], 5, 0.9, 0.2, False),
    (["M", 26, 70, "L", 44, 52, "L", 58, 62, "L", 80, 30], 5, 1.0, 0.15, False),
    (["M", 68, 28, "L", 81, 29, "L", 81, 42], 4, 0.7, 0.0, False),
])

# downward chart line (debt falls)
_add("chart_down", [
    (["M", 18, 14, "L", 18, 84, "L", 86, 84], 5, 0.9, 0.2, False),
    (["M", 26, 30, "L", 44, 48, "L", 58, 38, "L", 80, 70], 5, 1.0, 0.15, False),
    (["M", 68, 72, "L", 81, 71, "L", 81, 58], 4, 0.7, 0.0, False),
])

# underline (reusable annotation)
_add("underline", [
    (["M", 8, 78, "C", 30, 72, 70, 72, 92, 78], 5, 0.8, 0.0, False),
])

# circle highlight (reusable annotation)
_add("circle", [
    (["M", 50, 12, "C", 20, 14, 12, 44, 18, 62,
      "C", 26, 84, 62, 90, 78, 70,
      "C", 90, 54, 80, 22, 52, 14,
      "C", 50, 13, 49, 13, 50, 12], 5, 1.0, 0.0, False),
])

# checkmark
_add("check", [
    (["M", 16, 54, "L", 40, 76, "L", 84, 22], 6, 0.9, 0.0, False),
])

# process arrow (right)
_add("arrow_right", [
    (["M", 10, 50, "L", 76, 50], 6, 0.8, 0.1, False),
    (["M", 60, 34, "L", 78, 50, "L", 60, 66], 6, 0.8, 0.0, False),
])

# big dollar note (bill)
_add("bill", [
    (["M", 12, 30, "L", 88, 30, "L", 88, 70, "L", 12, 70, "L", 12, 30], 5, 1.0, 0.15, False),
    (["M", 38, 50, "C", 38, 42, 62, 42, 62, 50, "C", 62, 58, 38, 58, 38, 50], 4, 0.7, 0.1, False),
    (["M", 50, 44, "L", 50, 56], 3, 0.5, 0.0, False),
])


class _AssetObj:
    pass


def names() -> list:
    return sorted(_LIBRARY)


def has(name: str) -> bool:
    return name in _LIBRARY


def load_builtin(name: str) -> StrokeAsset:
    spec, viewBox = _LIBRARY[name]
    strokes = []
    for cmds, width, weight, pause_after, fill in spec:
        strokes.append(stroke_from_path_commands(
            cmds, width=width, weight=weight, pause_after=pause_after,
            fill=fill, tol=0.5))
    return StrokeAsset(strokes=strokes, viewBox=viewBox, name=f"builtin:{name}")


def load_file(path: str) -> StrokeAsset:
    """Project-relative stroke JSON:
    {"viewBox": [w, h], "strokes": [{"path": [...cmds], "width": 4,
     "weight": 1.0, "pause_after": 0.1, "fill": false}, ...]}
    Rejects anything unsupported (no scripts, no external refs by design)."""
    with open(path, encoding="utf-8-sig") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as e:
            raise ValueError(f"stroke asset {path} is not valid JSON: {e}") from None
    if not isinstance(data, dict) or not isinstance(data.get("strokes"), list) \
            or not data["strokes"]:
        raise ValueError(f"stroke asset {path} needs a non-empty strokes list.")
    vb = data.get("viewBox")
    if not isinstance(vb, list) or len(vb) != 2:
        raise ValueError(f"stroke asset {path} needs viewBox [w, h].")
    strokes = []
    for i, s in enumerate(data["strokes"]):
        if not isinstance(s, dict) or not isinstance(s.get("path"), list):
            raise ValueError(f"stroke asset {path}: strokes[{i}] needs a path list.")
        strokes.append(stroke_from_path_commands(
            s["path"], width=float(s.get("width", 4.0)),
            weight=float(s.get("weight", 1.0)),
            pause_after=float(s.get("pause_after", 0.0)),
            fill=bool(s.get("fill", False))))
    return StrokeAsset(strokes=strokes, viewBox=(float(vb[0]), float(vb[1])),
                       name=path)


def load(ref: str, project_folder: str = "") -> StrokeAsset:
    """builtin:<name> or a project-relative JSON path."""
    if ref.startswith("builtin:"):
        name = ref[len("builtin:"):]
        if not has(name):
            raise ValueError(f"unknown builtin stroke asset {name!r} "
                             f"(available: {', '.join(names())})")
        return load_builtin(name)
    path = os.path.join(project_folder, ref) if project_folder else ref
    if not os.path.isfile(path):
        raise ValueError(f"stroke asset file not found: {ref}")
    return load_file(path)


def schedule(ref: str, project_folder: str, start_frame: int,
             duration_frames: int):
    """Load + budget strokes over the event duration (last guard)."""
    asset = load(ref, project_folder)
    return asset, strokes_schedule(asset, start_frame, duration_frames)


from .strokes import schedule as strokes_schedule  # noqa: E402  (bottom import)
