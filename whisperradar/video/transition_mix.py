"""Transition mix: how many of the cuts inside a scene are a hard cut, a
crossfade or a slide left / right / up / down - every value comes from the
video options (transitions.cut_percent, crossfade_percent, slide_left_percent,
slide_right_percent, slide_up_percent, slide_down_percent), global or per
channel. Nothing is fixed here.

Only the cuts INSIDE a scene take part. Left alone: the first shot, the cut at
a scene boundary (scene_boundary_kind), a cut the editor set by hand, a
transition the shotlist asked for, and reveal steps.

A slide never repeats the direction of the slide before it (a slide up is never
followed by another slide up) as long as more than one direction has a share.

The "crossfade" share keeps whatever transitions.kind already put there. Only
the cut and slide shares change a clip. Slides go on shots that also move
(never a still shot) and are not placed back to back unless the slides add up
to more than half of the cuts.
"""
from __future__ import annotations

from . import models as m

SLIDE_KINDS = ("slide_left", "slide_right", "slide_up", "slide_down")
_KINDS = ("cut", "soft") + SLIDE_KINDS      # also the tie-break order (slides first)
_TIE = {"slide_left": 0, "slide_right": 1, "slide_up": 2, "slide_down": 3,
        "cut": 4, "soft": 5}


def shares(tr) -> dict:
    """{'cut','soft','slide_left',...} as fractions of 1 (scaled to add up)."""
    raw = {"cut": tr.cut_percent, "soft": tr.crossfade_percent,
           "slide_left": tr.slide_left_percent, "slide_right": tr.slide_right_percent,
           "slide_up": tr.slide_up_percent, "slide_down": tr.slide_down_percent}
    raw = {k: max(0.0, float(v)) for k, v in raw.items()}
    total = sum(raw.values())
    if total <= 0:
        return {**{k: 0.0 for k in _KINDS}, "soft": 1.0}
    return {k: v / total for k, v in raw.items()}


def _slide_ok(clip) -> bool:
    return (clip.motion != m.STATIC and clip.reveal is None
            and not clip.video_path and not clip.explainer_clip)


def apply(timeline, options) -> dict:
    """Set the cut / slide transitions on the timeline's clips in place.
    Returns {'cut': n, 'soft': n, 'slide': n, 'candidates': n}."""
    tr = options.transitions
    want = shares(tr)
    slide_share = sum(want[k] for k in SLIDE_KINDS)
    counts = {"cut": 0, "soft": 0, "slide": 0, "candidates": 0}
    if not tr.enabled or (want["cut"] == 0 and slide_share == 0):
        return counts
    clips = [c for s in timeline.scenes for c in s.clips]
    frames = round(tr.duration_seconds * timeline.fps)
    assigned = {k: 0 for k in _KINDS}
    last_slide = -10
    last_kind = None
    several = sum(1 for k in SLIDE_KINDS if want[k] > 0) > 1
    n = 0
    for i, clip in enumerate(clips):
        if (i == 0 or clip.scene_id != clips[i - 1].scene_id
                or clip.transition_fixed or clip.reveal is not None):
            continue
        n += 1
        deficit = {k: want[k] * n - assigned[k] for k in _KINDS}
        choice = "soft"
        for k in sorted(deficit, key=lambda k: (-deficit[k], _TIE[k])):
            if want[k] <= 0:
                continue
            if k in SLIDE_KINDS:
                if not _slide_ok(clip) or frames < 2:
                    continue
                if i - last_slide == 1 and slide_share <= 0.5:
                    continue
                if several and k == last_kind:
                    continue            # never the same slide direction twice in a row
            choice = k
            break
        assigned[choice] += 1
        if choice == "cut":
            clip.transition = None
        elif choice in SLIDE_KINDS:
            clip.transition = m.TransitionIn(choice, frames)
            last_slide = i
            last_kind = choice
    counts.update(cut=assigned["cut"], soft=assigned["soft"],
                  slide=sum(assigned[k] for k in SLIDE_KINDS), candidates=n)
    counts["by_kind"] = {k: assigned[k] for k in SLIDE_KINDS}
    return counts
