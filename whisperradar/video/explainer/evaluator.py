"""Deterministic scene evaluator: object state at a frame index.

A pure function of (frame, compiled scene, laid-out scene). Both backends
(filtergraph and frame-pipe) consume the same state model, so a scene renders
identically no matter which backend runs it.

State model per object at frame f (scene-local):
- visible: bool
- alpha: 0..1
- offset: (dx, dy) px (slide)
- progress: 0..1 (text/type-on, path draw, bar grow, counter value, reveal count)
- value: counter display value
- emphasized: bool (emphasis window active)
"""
from __future__ import annotations

from . import schema as sc
from .timing import CompiledScene, CompiledEvent


def _window(ev: CompiledEvent, f: int):
    """(active, progress 0..1) for an event at frame f."""
    if f < ev.start_frame:
        return (False, 0.0)
    if ev.duration_frames <= 0:
        return (True, 1.0)
    if f >= ev.end_frame:
        return (True, 1.0)
    return (True, (f - ev.start_frame) / ev.duration_frames)


class ObjectState:
    __slots__ = ("visible", "alpha", "dx", "dy", "progress", "value",
                 "visible_count", "emphasized", "text_progress", "gone",
                 "drawing", "erase_progress", "pen")

    def __init__(self):
        self.visible = False
        self.alpha = 0.0
        self.dx = self.dy = 0
        self.progress = 0.0
        self.value = 0.0
        self.visible_count = 0
        self.emphasized = False
        self.text_progress = 0.0
        self.gone = False
        self.drawing = False          # pen currently down on this object
        self.erase_progress = 0.0     # 0..1 (1 = fully erased)
        self.pen = None               # (x, y) pen-tip px while drawing


def _default_visible(obj_spec) -> bool:
    """Default visibility: reveal-driven objects are hidden before their first
    event; initial_visible overrides."""
    if obj_spec.initial_visible is not None:
        return obj_spec.initial_visible
    return False


def evaluate(scene: CompiledScene, laid_out, frame: int) -> dict:
    """-> {object_id: ObjectState} at scene-local frame."""
    states = {}
    for lo in laid_out.objects:
        st = ObjectState()
        st.visible = _default_visible(lo.spec)
        st.alpha = 1.0 if st.visible else 0.0
        if lo.spec.type == sc.COUNTER:
            st.value = lo.spec.from_value
        if lo.spec.type in (sc.GRID,):
            st.visible_count = lo.spec.columns * lo.spec.rows
        if lo.spec.type == sc.BLOCKS:
            st.visible_count = lo.spec.filled if st.visible else 0
        if lo.spec.type == sc.BAR:
            st.progress = 1.0 if st.visible else 0.0
        if lo.spec.type == sc.PATH:
            st.progress = 1.0 if st.visible else 0.0
        states[lo.spec.id] = st

    for cev in scene.events:
        ev = cev.event
        st = states.get(ev.target)
        if st is None:
            continue
        active, p = _window(cev, frame)
        if not active and cev.start_frame > frame:
            continue
        a = sc.ACTION_PROPERTY[ev.action]
        if ev.action == sc.ERASE:
            # strokes/handwriting erase stroke-by-stroke (reverse draw);
            # other objects fade quickly (no fake wipe of typed text)
            if active:
                if _spec_type_of(scene, ev.target) in (sc.STROKE_ASSET, sc.HANDWRITING):
                    st.erase_progress = max(st.erase_progress, p)
                    st.alpha = min(st.alpha, 1.0)
                    if st.erase_progress >= 1.0:
                        st.gone = True
                else:
                    st.alpha = min(st.alpha, 1.0 - p)
                    if p >= 1.0:
                        st.gone = True
                if cev.is_instant:
                    st.gone = True
        elif a == "alpha":
            if ev.action == sc.FADE_IN:
                st.visible = True
                st.alpha = max(st.alpha, p)
            else:  # fade_out
                if active:
                    st.alpha = min(st.alpha, 1.0 - p)
                    if p >= 1.0:
                        st.gone = True
        elif a == "position":
            if active:
                st.visible = True
                st.alpha = max(st.alpha, p)
                dist = ev.offset_px
                eased = 1.0 - (1.0 - p) * (1.0 - p)   # ease-out slide
                d = int(round(dist * (1.0 - eased)))
                dx, dy = 0, 0
                if ev.direction == "left":
                    dx = -d
                elif ev.direction == "right":
                    dx = d
                elif ev.direction == "up":
                    dy = -d
                else:
                    dy = d
                st.dx, st.dy = dx, dy
        elif a == "text_progress":
            if active:
                st.visible = True
                st.alpha = 1.0
                st.text_progress = p
        elif a == "draw_progress":
            if active:
                st.visible = True
                st.alpha = 1.0
                st.progress = p
                st.drawing = 0.0 < p < 1.0
        elif a == "value":
            if active:
                st.visible = True
                st.alpha = 1.0
                st.value = ev.target and _lerp_value(scene, ev, p)
        elif a == "fraction":
            if active:
                st.visible = True
                st.alpha = max(st.alpha, min(1.0, p * 3.0))  # visible almost at once
                st.progress = p
        elif a == "visible_count":
            if active:
                st.visible = True
                st.alpha = 1.0
                spec = _spec_of(scene, ev.target)
                if spec is not None:
                    if spec.type == sc.BLOCKS:
                        st.visible_count = max(st.visible_count, _staggered_count(
                            spec.filled, cev, frame))
                    else:
                        st.visible_count = spec.columns * spec.rows
        elif a == "emphasis":
            if active:
                st.visible = True
                st.emphasized = True
                st.alpha = max(st.alpha, 1.0)
        elif ev.action == sc.ERASE:
            if active:
                p_erase = p
                st.erase_progress = max(st.erase_progress, p_erase)
                if st.erase_progress >= 1.0:
                    st.gone = True
                if cev.is_instant:
                    st.gone = True
    return states


def _staggered_count(filled: int, cev: CompiledEvent, frame: int) -> int:
    """Blocks light up one by one across the event window."""
    if cev.duration_frames <= 0 or filled <= 1:
        return filled
    p = min(1.0, max(0.0, (frame - cev.start_frame) / cev.duration_frames))
    return max(1, int(round(filled * p)))


def _lerp_value(scene: CompiledScene, ev, p: float) -> float:
    spec = _spec_of(scene, ev.target)
    frm = spec.from_value if spec else 0.0
    to = spec.to_value if spec else 0.0
    eased = 1.0 - (1.0 - p) * (1.0 - p)
    return frm + (to - frm) * eased


def _spec_of(scene: CompiledScene, object_id: str):
    for o in scene.spec.objects:
        if o.id == object_id:
            return o
    return None


def _spec_type_of(scene: CompiledScene, object_id: str):
    spec = _spec_of(scene, object_id)
    return spec.type if spec else None


def formatter(ev_or_spec_format: str, value: float, decimals: int = 0,
              prefix: str = "") -> str:
    fmt = ev_or_spec_format
    if fmt == sc.FMT_CURRENCY_USD:
        return "$" + format(round(value, 2), ",.2f" if decimals else ",.0f")
    if fmt == sc.FMT_PERCENT:
        return f"{value:.{decimals}f}%"
    if fmt == sc.FMT_MULTIPLIER:
        return f"{value:.{decimals}f}x" if decimals else f"{value:g}x"
    return prefix + f"{value:.{decimals}f}" if decimals else prefix + f"{value:g}"
