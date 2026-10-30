"""Channel types: what a channel makes decides which parts of the pipeline apply.

One setting, `channel_type` (a video option, so it can be set globally and per
channel), says what kind of channel this is:

  standard   pictures and clips: shotlist, visual style, character bible,
             references, image generation and image camera motion.
  explainer  drawn scenes: a scene plan instead of a shotlist. Presentation is
             a separate choice (`explainer.format`: whiteboard or motion graphics).

Every type shares the text-side stages: writing style, opening choices, script,
GPT-Claude review, accuracy checks, voice, audio, subtitles and packaging.

Adding a type later = one `register(ChannelType(...))` call (plus whatever
renders it). Nothing is ever deleted when a channel changes type: the image
settings (visual style, bible, references) stay saved and simply stop being
used, so switching back finds them as they were.

`visual_mode` (images / whiteboard / motion_graphics) is the older single
setting the render code reads. It is kept in step with the type, and a channel
that only has the old `visual_mode` is read as the matching type, so existing
channels need no migration.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelType:
    key: str
    label: str
    formats: tuple = ()               # presentation options (first = default); () = none to choose
    image_pipeline: bool = True       # visual style, bible, references, image generation, camera motion
    scene_planning: bool = False      # a scene plan replaces the image shotlist
    visual_modes: tuple = ("images",)  # the visual_mode value each format maps to (same order as formats)

    def mode_for(self, fmt: str = "") -> str:
        """The visual_mode value for a format (the default format when blank/unknown)."""
        if not self.formats:
            return self.visual_modes[0]
        if fmt in self.formats:
            return self.visual_modes[self.formats.index(fmt)]
        return self.visual_modes[0]


STANDARD = ChannelType("standard", "Normal channel - pictures and clips (image engine)")
EXPLAINER = ChannelType("explainer", "Explainer - drawn scenes (scene plan, no images)",
                        formats=("whiteboard", "motion_graphics"),
                        image_pipeline=False, scene_planning=True,
                        visual_modes=("whiteboard", "motion_graphics"))
FORMAT_LABELS = {"whiteboard": "hand-drawn chalkboard / whiteboard",
                 "motion_graphics": "graphic (charts, counters, text)"}

TYPES: dict = {}
DEFAULT = "standard"


def register(t: ChannelType) -> None:
    TYPES[t.key] = t


register(STANDARD)
register(EXPLAINER)


def keys() -> list:
    return list(TYPES)


def from_visual_mode(mode: str) -> ChannelType:
    """The type a legacy visual_mode value means."""
    for t in TYPES.values():
        if t.formats and mode in t.visual_modes:
            return t
    return TYPES[DEFAULT]


def type_of(settings: dict) -> ChannelType:
    """The channel type of effective video options (channel over global)."""
    settings = settings or {}
    key = settings.get("channel_type") or ""
    if key in TYPES:
        return TYPES[key]
    return from_visual_mode(settings.get("visual_mode") or "images")


def format_of(settings: dict) -> str:
    """The presentation format of an explainer-like type ("" when the type has none)."""
    t = type_of(settings)
    if not t.formats:
        return ""
    fmt = settings.get("explainer.format") or ""
    if fmt in t.formats:
        return fmt
    mode = settings.get("visual_mode") or ""
    if mode in t.visual_modes:
        return t.formats[t.visual_modes.index(mode)]
    return t.formats[0]


def normalize(settings: dict) -> dict:
    """Effective options with visual_mode made consistent with the channel type.
    A channel with no channel_type is left exactly as it is (the old way)."""
    out = dict(settings or {})
    if out.get("channel_type") in TYPES:
        t = TYPES[out["channel_type"]]
        out["visual_mode"] = t.mode_for(format_of(out))
    return out


def uses_images(settings: dict) -> bool:
    """True when the image pipeline (visual style, bible, references, images, camera motion) applies."""
    return type_of(settings).image_pipeline


def plans_scenes(settings: dict) -> bool:
    return type_of(settings).scene_planning


def apply_choice(values: dict, type_key: str, fmt: str = "") -> dict:
    """Store a channel-form choice in a channel's saved options, keeping every
    other saved value. type_key "" = inherit (type and legacy mode cleared)."""
    out = {k: v for k, v in values.items()
           if k not in ("channel_type", "visual_mode")}
    if fmt:
        out["explainer.format"] = fmt
    if type_key in TYPES:
        t = TYPES[type_key]
        out["channel_type"] = t.key
        out["visual_mode"] = t.mode_for(fmt or out.get("explainer.format", ""))
    return out
