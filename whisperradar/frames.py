"""Picture frame of a production: 16:9 (landscape) or 9:16 (vertical, Shorts).

The frame is a channel setting. It is copied onto the production when the
channel is assigned and stays changeable until the reference/image stages
start; after that it is locked, because pictures, motion and the Premiere
project all depend on the shape.
"""
from __future__ import annotations

LANDSCAPE = "16:9"
VERTICAL = "9:16"
FRAMES = (LANDSCAPE, VERTICAL)
LABELS = {LANDSCAPE: "16:9 landscape (normal video)",
          VERTICAL: "9:16 vertical (Shorts)"}
# stages from which the frame can no longer change
LOCKED_FROM = "refs"
# Flow's 9:16 master and the project size used in Premiere (upscale there)
VERTICAL_FLOW_NATIVE = (768, 1376)
VERTICAL_PROJECT = (1080, 1920)
VERTICAL_PREVIEW = (360, 640)
# the only camera moves that make sense on a tall picture
VERTICAL_MOTIONS = ("ZI", "ZO", "ST")


def normalize(value) -> str:
    v = str(value or "").strip().replace("x", ":").replace("/", ":")
    return VERTICAL if v == VERTICAL else LANDSCAPE


def _get(row, key):
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None


def effective(prod, own=None) -> str:
    """The production's own frame, else its channel's, else 16:9."""
    return normalize(_get(prod, "frame") or _get(own, "frame"))


def is_vertical(prod, own=None) -> bool:
    return effective(prod, own) == VERTICAL


def is_locked(prod) -> bool:
    from . import db
    stage = _get(prod, "stage")
    if stage not in db.STAGES:
        return False
    return db.STAGES.index(stage) >= db.STAGES.index(LOCKED_FROM)


def stamp_from_channel(conn, pid, own) -> None:
    """Copy the channel's frame onto the production unless it is locked."""
    from . import db
    prod = db.get_production(conn, pid)
    if prod is None or is_locked(prod):
        return
    db.update_production(conn, pid, frame=normalize(_get(own, "frame")))
