"""Font style presets for explainer scenes (one setting instead of three font pickers).

video1       clean bold sans for labels and text, marker headline for scene titles
             (the look of the reference explainer video). DEFAULT.
clean        the same sans everywhere, titles included.
handwritten  the earlier look: hand fonts that write on.
"""
from __future__ import annotations

DEFAULT_STYLE = "video1"

STYLES = {
    "video1": {"hand": "Inter-Bold.ttf", "heading": "Inter-Bold.ttf", "body": "Inter-Regular.ttf",
               "title": "CaveatBrush-Regular.ttf"},
    "clean": {"hand": "Inter-Bold.ttf", "heading": "Inter-Bold.ttf", "body": "Inter-Regular.ttf",
              "title": ""},
    "handwritten": {"hand": "Neucha-Regular.ttf", "heading": "Delius-Regular.ttf",
                    "body": "Delius-Regular.ttf", "title": ""},
}

LABELS = {
    "video1": "Video 1 - clean sans, marker headlines (default)",
    "clean": "Clean - one sans everywhere",
    "handwritten": "Handwritten - hand fonts that write on",
}


def resolve(look):
    """The preset a channel look asks for, or None to leave fonts to the theme.

    An explicit font_style wins. With none set, a channel that already picked
    its own hand/heading/body font keeps those; otherwise the default style."""
    if not isinstance(look, dict):
        return None            # no channel look at all (a bare document): theme fonts
    name = str(look.get("font_style") or "").strip().lower()
    if name:
        return STYLES.get(name)
    if any(str(look.get(k) or "").strip() for k in ("hand_font", "heading_font", "body_font")):
        return None
    return STYLES[DEFAULT_STYLE]


TITLE_SIZES = {"small": 0.85, "normal": 1.0, "large": 1.2}
MARKER_BOOST = 1.18          # the marker headline is drawn a little bigger than the sans one


def title_scale(look):
    """Headline size factor: channel "title_size" (small | normal | large) times the
    marker boost when the style's headline font is the marker."""
    if not isinstance(look, dict):
        return 1.0
    pick = TITLE_SIZES.get(str(look.get("title_size") or "").strip().lower(), 1.0)
    preset = resolve(look)
    return round(pick * (MARKER_BOOST if (preset or {}).get("title") else 1.0), 3)
