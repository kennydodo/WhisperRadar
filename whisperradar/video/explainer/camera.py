"""Camera and persistent canvas for pen-drawn explainer scenes.

A scene may carry
  camera  {"from": {"zoom": 1, "center": [0.5, 0.5]}, "to": {"zoom": 2, "center": [0.25, 0.25]},
           "move_ms": 1200}   the view slides from "from" to "to" at the start of the scene, then holds
  base    the id of an EARLIER scene whose finished drawing is already on the board when this
          scene starts (the master diagram comes back; new things are added to it)

The world is the ordinary frame [0,1]x[0,1]; a view is a crop of it (zoom >= 1). Scenes are drawn
at `supersample(...)` times the output size, so a zoomed view is cut from pixels, not enlarged.
Scenes without a camera or base render exactly as before.
"""
from __future__ import annotations

import math

MAX_ZOOM = 3.0
DEFAULT_MOVE_MS = 1200
OVERVIEW = {"zoom": 1.0, "center": [0.5, 0.5]}


def _view(v):
    z = max(1.0, min(MAX_ZOOM, float(v.get("zoom", 1.0))))
    c = v.get("center") or [0.5, 0.5]
    return {"zoom": z, "center": [float(c[0]), float(c[1])]}


def clamp_center(zoom, cx, cy):
    half = 0.5 / zoom
    return (min(max(cx, half), 1 - half), min(max(cy, half), 1 - half))


def max_zoom(cam) -> float:
    if not cam:
        return 1.0
    return max(_view(cam.get("from") or OVERVIEW)["zoom"], _view(cam.get("to") or OVERVIEW)["zoom"])


def supersample(cam) -> int:
    """Pixel multiplier the scene is drawn at so the deepest zoom still has full resolution."""
    return max(1, min(3, math.ceil(max_zoom(cam) - 1e-9)))


def view_at(cam, frame: int, fps: float):
    """(zoom, cx, cy) of the camera at a frame of its scene (eased move, then hold)."""
    if not cam:
        return 1.0, 0.5, 0.5
    a, b = _view(cam.get("from") or OVERVIEW), _view(cam.get("to") or OVERVIEW)
    move = max(1, int(round(float(cam.get("move_ms", DEFAULT_MOVE_MS)) / 1000.0 * fps)))
    t = min(1.0, max(0.0, frame / move))
    t = t * t * (3 - 2 * t)                                   # ease in-out
    # zoom changes geometrically so the apparent speed is even
    z = a["zoom"] * (b["zoom"] / a["zoom"]) ** t
    cx = a["center"][0] + (b["center"][0] - a["center"][0]) * t
    cy = a["center"][1] + (b["center"][1] - a["center"][1]) * t
    cx, cy = clamp_center(z, cx, cy)
    return z, cx, cy


def crop(img, view, out_size):
    """The part of the (supersampled) world image the view sees, at the output size."""
    from PIL import Image
    z, cx, cy = view
    W, H = img.size
    w, h = W / z, H / z
    x0 = cx * W - w / 2
    y0 = cy * H - h / 2
    box = (x0, y0, x0 + w, y0 + h)
    return img.resize(out_size, Image.LANCZOS, box=box)


def region_view(region, margin=1.0):
    """Camera view that fits a [x, y, w, h] region of the world."""
    x, y, w, h = [float(v) for v in region]
    z = min(MAX_ZOOM, max(1.0, margin / max(w, h)))
    return {"zoom": z, "center": [x + w / 2, y + h / 2]}
