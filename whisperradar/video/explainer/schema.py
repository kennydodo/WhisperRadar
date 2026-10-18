"""Explainer scene contract: strict enums and parsed models for explainer.json.

Design rules (docs/explainer/architecture.md):
- declarative JSON only, no executable expressions;
- coordinates normalized to the scene canvas;
- cue ids are 1-based SRT indices (repo convention, parsing.cue_index);
- timing in integer frames, end frames exclusive (repo convention).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

SUPPORTED_SCHEMA_VERSIONS = (1, 2)
SCHEMA_VERSION = 2
FILE_NAME = "explainer.json"

# ---- object types -------------------------------------------------------------
TEXT, LABEL, PATH, BAR, COUNTER, GRID, BLOCKS, CALLOUT, IMAGE = (
    "text", "label", "path", "bar", "counter", "grid", "blocks", "callout", "image")
STROKE_ASSET, HANDWRITING = "stroke_asset", "handwriting"
OBJECT_TYPES = (TEXT, LABEL, PATH, BAR, COUNTER, GRID, BLOCKS, CALLOUT, IMAGE,
                STROKE_ASSET, HANDWRITING)

# ---- actions -------------------------------------------------------------------
FADE_IN, FADE_OUT, SLIDE, TYPE_ON, DRAW, COUNT, GROW, REVEAL, EMPHASIS = (
    "fade_in", "fade_out", "slide", "type_on", "draw", "count", "grow", "reveal", "emphasis")
DRAW_STROKES, ERASE = "draw_strokes", "erase"
ACTIONS = (FADE_IN, FADE_OUT, SLIDE, TYPE_ON, DRAW, COUNT, GROW, REVEAL, EMPHASIS,
           DRAW_STROKES, ERASE)

# which actions may target which object types
ACTION_TARGETS = {
    FADE_IN: (TEXT, LABEL, PATH, BAR, COUNTER, GRID, BLOCKS, CALLOUT, IMAGE),
    FADE_OUT: (TEXT, LABEL, PATH, BAR, COUNTER, GRID, BLOCKS, CALLOUT, IMAGE),
    SLIDE: (TEXT, LABEL, CALLOUT, IMAGE),
    TYPE_ON: (TEXT, LABEL),
    DRAW: (PATH,),
    COUNT: (COUNTER,),
    GROW: (BAR,),
    REVEAL: (GRID, BLOCKS),
    EMPHASIS: (TEXT, LABEL, BAR, COUNTER, CALLOUT),
    DRAW_STROKES: (STROKE_ASSET, HANDWRITING),
    ERASE: (STROKE_ASSET, HANDWRITING, TEXT, LABEL, PATH, BAR, COUNTER, GRID,
            BLOCKS, CALLOUT, IMAGE),
}

# object property each action owns (conflicting simultaneous writers rejected)
ACTION_PROPERTY = {
    FADE_IN: "alpha", FADE_OUT: "alpha", ERASE: "alpha",
    SLIDE: "position", TYPE_ON: "text_progress",
    DRAW: "draw_progress", DRAW_STROKES: "draw_progress",
    COUNT: "value",
    GROW: "fraction", REVEAL: "visible_count",
    EMPHASIS: "emphasis",
}

EDGES = ("start", "end")

# ---- visual styles (mode) ------------------------------------------------------------
STYLE_MOTION, STYLE_WHITEBOARD = "motion_graphics", "whiteboard"
STYLES = (STYLE_MOTION, STYLE_WHITEBOARD)

# ---- slide directions -----------------------------------------------------------
SLIDE_DIRECTIONS = ("left", "right", "up", "down")

# ---- bar direction ----------------------------------------------------------------
BAR_HORIZONTAL, BAR_VERTICAL = "horizontal", "vertical"
BAR_DIRECTIONS = (BAR_HORIZONTAL, BAR_VERTICAL)

# ---- number formatters --------------------------------------------------------------
FMT_PLAIN = "plain"
FMT_CURRENCY_USD = "currency_usd"
FMT_PERCENT = "percent"
FMT_MULTIPLIER = "multiplier"
FORMATTERS = (FMT_PLAIN, FMT_CURRENCY_USD, FMT_PERCENT, FMT_MULTIPLIER)

# ---- emphasis kinds ---------------------------------------------------------------------
EMPH_NONE, EMPHASIS_OUTLINE, EMPHASIS_GLOW = "none", "outline", "glow"

# ---- roles (accent colors from the theme) --------------------------------------------------
ROLES = ("default", "accent", "positive", "negative", "muted")

# ---- canvas default ---------------------------------------------------------------------
DEFAULT_CANVAS = (1920, 1080)


@dataclass(frozen=True)
class CuePoint:
    cue: int
    edge: str = "start"

    def to_dict(self) -> dict:
        return {"cue": self.cue, "edge": self.edge}

    @staticmethod
    def from_dict(d) -> Optional["CuePoint"]:
        if not isinstance(d, dict):
            return None
        cue = d.get("cue")
        edge = d.get("edge", "start")
        if not isinstance(cue, int) or isinstance(cue, bool) or edge not in EDGES:
            return None
        return CuePoint(cue, edge)


@dataclass
class EventSpec:
    target: str
    action: str
    at: CuePoint
    duration_ms: int = 0
    offset_ms: int = 0                   # manual offset from the cue anchor
    direction: Optional[str] = None       # slide direction
    offset_px: int = 48                   # slide travel
    format: str = FMT_PLAIN               # counter format
    decimals: int = 0                     # counter decimals
    prefix: str = ""                      # counter prefix (e.g. currency symbol override)
    role: str = "default"                 # emphasis/accent role

    def to_dict(self) -> dict:
        d = {"target": self.target, "action": self.action, "at": self.at.to_dict()}
        if self.duration_ms:
            d["duration_ms"] = self.duration_ms
        if self.offset_ms:
            d["offset_ms"] = self.offset_ms
        if self.direction is not None:
            d["direction"] = self.direction
        if self.action == SLIDE:
            d["offset_px"] = self.offset_px
        if self.action == COUNT:
            d["format"] = self.format
            if self.decimals:
                d["decimals"] = self.decimals
            if self.prefix:
                d["prefix"] = self.prefix
        if self.action == EMPHASIS:
            d["role"] = self.role
        return d


@dataclass
class ObjectSpec:
    id: str
    type: str
    box: tuple                            # normalized (x, y, w, h)
    text: str = ""
    style: str = "body"                   # theme style name
    # path
    points: Optional[list] = None         # normalized [[x, y], ...]
    closed: bool = False
    arrow: bool = False
    # bar
    fraction: float = 1.0                 # final fill fraction 0..1
    direction: str = BAR_HORIZONTAL
    # counter
    from_value: float = 0.0
    to_value: float = 0.0
    format: str = FMT_PLAIN
    decimals: int = 0
    # grid/blocks
    columns: int = 0
    rows: int = 0
    count: int = 0                        # blocks total
    filled: int = 0                       # blocks filled
    # image / stroke asset
    asset: str = ""
    fit: str = "contain"                  # contain | stretch
    stroke_order: str = "authored"        # authored | group
    fill_after_draw: bool = False
    # handwriting
    fallback_typed: bool = False          # disclosed typed-text fallback
    initial_visible: Optional[bool] = None  # None = type default

    def to_dict(self) -> dict:
        d = {"id": self.id, "type": self.type, "box": list(self.box)}
        if self.text:
            d["text"] = self.text
        if self.style != "body":
            d["style"] = self.style
        if self.points is not None:
            d["points"] = [list(p) for p in self.points]
            if self.closed:
                d["closed"] = True
            if self.arrow:
                d["arrow"] = True
        if self.type == BAR:
            d["fraction"] = self.fraction
            d["direction"] = self.direction
        if self.type == COUNTER:
            d["from"] = self.from_value
            d["to"] = self.to_value
            d["format"] = self.format
            if self.decimals:
                d["decimals"] = self.decimals
        if self.type in (GRID, BLOCKS):
            d["columns"] = self.columns
            d["rows"] = self.rows
            if self.type == BLOCKS:
                d["count"] = self.count
                d["filled"] = self.filled
        if self.type in (IMAGE, STROKE_ASSET):
            d["asset"] = self.asset
            if self.type == IMAGE and self.fit != "contain":
                d["fit"] = self.fit
            if self.type == STROKE_ASSET:
                if self.stroke_order != "authored":
                    d["stroke_order"] = self.stroke_order
                if self.fill_after_draw:
                    d["fill_after_draw"] = True
        if self.type == HANDWRITING and self.fallback_typed:
            d["fallback_typed"] = True
        if self.initial_visible is not None:
            d["initial_visible"] = self.initial_visible
        return d


@dataclass
class SceneSpec:
    id: str
    shot_id: str
    cue_range: tuple                      # [first_cue, last_cue], inclusive, 1-based
    objects: list = field(default_factory=list)   # ObjectSpec
    events: list = field(default_factory=list)    # EventSpec
    camera: Optional[str] = None          # None only in MVP (static viewport)

    def to_dict(self) -> dict:
        d = {"id": self.id, "shot_id": self.shot_id, "cue_range": list(self.cue_range),
             "objects": [o.to_dict() for o in self.objects],
             "events": [e.to_dict() for e in self.events]}
        if self.camera is not None:
            d["camera"] = self.camera
        return d


@dataclass
class ThemeSpec:
    name: str
    background: str
    foreground: str
    muted: str
    accent: str
    positive: str
    negative: str
    heading_font: str
    body_font: str
    mono_font: str
    heading_size: int = 72
    body_size: int = 44
    label_size: int = 36
    stroke_width: int = 4
    subtitle_safe: float = 0.12           # bottom fraction reserved for captions

    def to_dict(self) -> dict:
        return {
            "name": self.name, "background": self.background,
            "foreground": self.foreground, "muted": self.muted,
            "accent": self.accent, "positive": self.positive, "negative": self.negative,
            "heading_font": self.heading_font, "body_font": self.body_font,
            "mono_font": self.mono_font, "heading_size": self.heading_size,
            "body_size": self.body_size, "label_size": self.label_size,
            "stroke_width": self.stroke_width, "subtitle_safe": self.subtitle_safe}


@dataclass
class ExplainerDoc:
    schema_version: int
    theme: ThemeSpec
    canvas: tuple                         # (width, height)
    scenes: list                          # SceneSpec
    style: str = STYLE_MOTION             # motion_graphics | whiteboard

    def to_dict(self) -> dict:
        d = {
            "schema_version": self.schema_version,
            "theme": self.theme.to_dict(),
            "canvas": {"width": self.canvas[0], "height": self.canvas[1]},
            "scenes": [s.to_dict() for s in self.scenes]}
        if self.style != STYLE_MOTION:
            d["style"] = self.style
        return d


def default_theme(name: str = "finance_dark") -> ThemeSpec:
    return ThemeSpec(
        name=name,
        background="#101418", foreground="#F2F4F6", muted="#8A929A",
        accent="#3B82F6", positive="#22C55E", negative="#EF4444",
        heading_font="C:/Windows/Fonts/segoeuib.ttf",
        body_font="C:/Windows/Fonts/segoeui.ttf",
        mono_font="C:/Windows/Fonts/consolab.ttf")


def default_whiteboard_theme(name: str = "whiteboard_light") -> ThemeSpec:
    """Light board, dark marker strokes (doc 16.3): a drawing behavior, not a
    recolored motion-graphics theme - marker ink draws stroke by stroke."""
    return ThemeSpec(
        name=name,
        background="#F7F5F0", foreground="#2B2B28", muted="#9A968C",
        accent="#1F6FB8", positive="#2E8B57", negative="#C0392B",
        heading_font="C:/Windows/Fonts/segoeuib.ttf",
        body_font="C:/Windows/Fonts/segoeui.ttf",
        mono_font="C:/Windows/Fonts/consolab.ttf",
        stroke_width=5)
