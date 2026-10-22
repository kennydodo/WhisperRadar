"""Port of ImgToVideo.Core.Motion.MotionEngine."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from . import models as m
from .options import MotionOptions, OutputOptions

# a PU/PD "land" must leave at least this share of the full tilt, or it is ignored
LAND_MIN_TRAVEL = 0.25
MIN_TRAVEL_PIXELS = 32
AUTO_POOL = (m.STATIC, m.ZOOM_IN, m.ZOOM_OUT, m.PAN_LEFT, m.PAN_RIGHT)


@dataclass
class MotionContext:
    explicit_code: Optional[str]
    is_video_start: bool
    is_video_end: bool
    is_scene_start: bool
    previous: Optional[str]
    shots_since_static: int


@dataclass
class MotionDecision:
    motion: str
    source: str


@dataclass
class ClipMotionPlan:
    motion: str
    source: str
    start: m.Rect
    end: m.Rect
    warnings: list = field(default_factory=list)


def _is_pan(motion) -> bool:
    return motion in (m.PAN_LEFT, m.PAN_RIGHT, m.PAN_UP, m.PAN_DOWN)


def _pick_first_not_previous(candidates, previous):
    for c in candidates:
        if c != previous:
            return c
    return candidates[0]


def _round_away(x: float) -> int:
    """Math.Round(x, MidpointRounding.AwayFromZero)."""
    if x >= 0:
        f = math.floor(x)
        return f + 1 if x - f >= 0.5 else f
    return -_round_away(-x)


def _even(value: float) -> int:
    return _round_away(value / 2) * 2


def _canvas_rect(image_width, image_height, out_aspect) -> m.Rect:
    if image_width / image_height < out_aspect:
        canvas_height = image_height
        canvas_width = _even(image_height * out_aspect)
    else:
        canvas_width = image_width
        canvas_height = _even(image_width / out_aspect)
    return m.Rect((canvas_width - image_width) / 2.0, (canvas_height - image_height) / 2.0,
                  canvas_width, canvas_height)


def _shrink_around_center(rect: m.Rect, factor: float) -> m.Rect:
    return m.Rect(rect.x + rect.width * (1 - factor) / 2.0,
                  rect.y + rect.height * (1 - factor) / 2.0,
                  rect.width * factor, rect.height * factor)


class MotionEngine:
    def __init__(self, motion: MotionOptions, output: OutputOptions):
        self._motion = motion
        self._output = output

    def select_motion(self, ctx: MotionContext) -> MotionDecision:
        if ctx.explicit_code is not None:
            return MotionDecision(ctx.explicit_code, m.EXPLICIT_CODE)
        if ctx.is_video_start:
            return MotionDecision(m.ZOOM_IN, m.AUTO_SELECTED)
        if ctx.is_video_end:
            return MotionDecision(
                _pick_first_not_previous([m.STATIC, m.ZOOM_OUT], ctx.previous), m.AUTO_SELECTED)
        if ctx.is_scene_start:
            return MotionDecision(
                _pick_first_not_previous([m.ZOOM_IN, m.STATIC], ctx.previous), m.AUTO_SELECTED)

        if (ctx.shots_since_static >= max(1, self._motion.static_every_max_shots)
                or (ctx.shots_since_static >= self._motion.static_every_min_shots
                    and _is_pan(ctx.previous))):
            return MotionDecision(m.STATIC, m.AUTO_SELECTED)

        p = ctx.previous
        if p == m.PAN_LEFT:
            preferred = [m.STATIC, m.PAN_RIGHT, m.ZOOM_IN]
        elif p == m.PAN_RIGHT:
            preferred = [m.STATIC, m.PAN_LEFT, m.ZOOM_OUT]
        elif p in (m.PAN_UP, m.PAN_DOWN):
            preferred = [m.STATIC, m.ZOOM_IN, m.ZOOM_OUT]
        elif p == m.ZOOM_IN:
            preferred = [m.STATIC, m.PAN_RIGHT, m.ZOOM_OUT]
        elif p == m.ZOOM_OUT:
            preferred = [m.STATIC, m.ZOOM_IN, m.PAN_LEFT]
        else:
            preferred = [m.ZOOM_IN, m.PAN_LEFT, m.PAN_RIGHT, m.ZOOM_OUT]
        for c in preferred:
            if c in AUTO_POOL and c != ctx.previous:
                return MotionDecision(c, m.AUTO_SELECTED)
        return MotionDecision(_pick_first_not_previous(AUTO_POOL, ctx.previous), m.AUTO_SELECTED)

    def _paced_travel(self, out_span: float, img_per_out: float,
                      spare: float, duration_seconds: Optional[float]) -> float:
        """Auto-selected pan travel, in IMAGE pixels: an accent, capped at
        pan_max_travel_percent of the OUTPUT dimension (converted through the
        image's own fit-to-output scale, so a 1:1 PU canvas and a 16:9 one
        move the same number of screen pixels). Without a duration the legacy
        behavior stands, byte-compatible with the C# engine."""
        mo = self._motion
        if duration_seconds and duration_seconds > 0:
            target = (mo.pan_target_speed_px_per_second
                      * max(duration_seconds, mo.pan_travel_seconds_floor)) * img_per_out
            cap = mo.pan_max_travel_percent / 100.0 * out_span * img_per_out
            travel = min(target, cap, spare)
        else:
            travel = min(mo.pan_max_travel_percent / 100.0 * out_span, spare)
        return max(travel, 2.0)

    def plan_clip(self, motion: str, source: str, pan_right: bool,
                  image_width: int, image_height: int,
                  duration_seconds: Optional[float] = None,
                  land: Optional[float] = None) -> ClipMotionPlan:
        warnings: list = []
        out_aspect = self._output.width / self._output.height
        image_aspect = image_width / image_height
        full = m.Rect(0, 0, image_width, image_height)

        fit_width = image_height * out_aspect if image_aspect > out_aspect else image_width
        fit_height = image_height if image_aspect > out_aspect else image_width / out_aspect
        spare_x = image_width - fit_width
        spare_y = image_height - fit_height
        mo = self._motion

        if motion == m.STATIC:
            return ClipMotionPlan(motion, source, full, full, warnings)

        if motion in (m.ZOOM_IN, m.ZOOM_OUT):
            base = _canvas_rect(image_width, image_height, out_aspect)
            start_pct = mo.push_in_start_percent if motion == m.ZOOM_IN else mo.zoom_out_start_percent
            end_pct = mo.push_in_end_percent if motion == m.ZOOM_IN else mo.zoom_out_end_percent
            start = _shrink_around_center(base, 100.0 / start_pct)
            end = _shrink_around_center(base, 100.0 / end_pct)
            if motion == m.ZOOM_OUT:
                start, end = end, start
            return ClipMotionPlan(motion, source, start, end, warnings)

        if motion in (m.PAN_LEFT, m.PAN_RIGHT, m.PAN_REVEAL):
            if spare_x < MIN_TRAVEL_PIXELS:
                return self._push_in_fallback(
                    f"No horizontal overscan for {_motion_name(motion)} (image {image_width}x{image_height}); "
                    "using a gentle push-in instead — generate a 150%-wide image (2880x1296) to pan.",
                    pan_right, image_width, image_height)
            y = (image_height - fit_height) / 2.0
            if motion == m.PAN_REVEAL:
                start_x = 0 if pan_right else spare_x
                end_x = spare_x if pan_right else 0
            elif source == m.EXPLICIT_CODE:
                # filename-coded pans sweep the full band edge-to-edge: the
                # image is composed to travel out of frame; easing decides
                # the speed profile (short pans ease out: fast start,
                # decelerating to rest)
                start_x = 0 if motion == m.PAN_RIGHT else spare_x
                end_x = spare_x if motion == m.PAN_RIGHT else 0
                if land is not None:
                    # the shotlist says where the subject sits (percent across the
                    # picture): the pan ends with that point centred, not on the edge
                    want = min(max(land, 0.0), 100.0) / 100.0 * image_width - fit_width / 2.0
                    want = min(max(want, 0.0), spare_x)
                    if abs(want - start_x) >= LAND_MIN_TRAVEL * spare_x:
                        end_x = want
                    else:
                        warnings.append(
                            f"land {land:g} leaves almost no {_motion_name(motion)} travel "
                            "(the subject sits where the move starts); sweeping the whole "
                            "picture instead.")
            else:
                travel = self._paced_travel(self._output.width,
                                            fit_width / self._output.width,
                                            spare_x, duration_seconds)
                center = spare_x / 2.0
                if motion == m.PAN_RIGHT:
                    start_x, end_x = center - travel / 2.0, center + travel / 2.0
                else:
                    start_x, end_x = center + travel / 2.0, center - travel / 2.0
            return ClipMotionPlan(motion, source, m.Rect(start_x, y, fit_width, fit_height),
                                  m.Rect(end_x, y, fit_width, fit_height), warnings)

        if motion in (m.PAN_UP, m.PAN_DOWN):
            if spare_y < MIN_TRAVEL_PIXELS:
                return self._push_in_fallback(
                    f"No vertical overscan for {_motion_name(motion)} (image {image_width}x{image_height}); "
                    "using a gentle push-in instead — generate a 200%-tall image (2304x2160) to pan.",
                    pan_right, image_width, image_height)
            x = (image_width - fit_width) / 2.0
            # A filename-coded pan sweeps the whole overscan band edge-to-edge
            # - the composition is meant to travel out of frame. Easing shapes
            # the speed: short pans ease out (start fast, decelerate to rest);
            # the old mid-shot surge came from ease_in_out, not the distance.
            # Auto-selected pans are accents: paced travel capped at
            # pan_max_travel_percent of the output dimension.
            center = spare_y / 2.0
            if source == m.EXPLICIT_CODE:
                start_y = spare_y if motion == m.PAN_UP else 0
                end_y = 0 if motion == m.PAN_UP else spare_y
                if land is not None:
                    # the shotlist says where the subject sits (percent down the
                    # picture): the tilt ends with that point centred, not on the edge
                    want = min(max(land, 0.0), 100.0) / 100.0 * image_height - fit_height / 2.0
                    want = min(max(want, 0.0), spare_y)
                    if abs(want - start_y) >= LAND_MIN_TRAVEL * spare_y:
                        end_y = want
                    else:
                        warnings.append(
                            f"land {land:g} leaves almost no {_motion_name(motion)} travel "
                            "(the subject sits where the move starts); sweeping the whole "
                            "picture instead.")
            else:
                travel = self._paced_travel(self._output.height,
                                            fit_height / self._output.height,
                                            spare_y, duration_seconds)
                if motion == m.PAN_UP:
                    start_y, end_y = center + travel / 2.0, center - travel / 2.0
                else:
                    start_y, end_y = center - travel / 2.0, center + travel / 2.0
            return ClipMotionPlan(motion, source, m.Rect(x, start_y, fit_width, fit_height),
                                  m.Rect(x, end_y, fit_width, fit_height), warnings)

        return ClipMotionPlan(motion, source, full, full, warnings)

    def _push_in_fallback(self, warning, pan_right, image_width, image_height) -> ClipMotionPlan:
        fb = self.plan_clip(m.ZOOM_IN, m.AUTO_SELECTED, pan_right, image_width, image_height)
        return ClipMotionPlan(fb.motion, fb.source, fb.start, fb.end, [warning] + list(fb.warnings))


def _motion_name(motion: str) -> str:
    """C# enum member name (e.g. PanLeft), as printed by string interpolation."""
    return "".join(p.capitalize() for p in motion.split("_"))
