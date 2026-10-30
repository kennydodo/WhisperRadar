"""Explainer look as a channel setting (My channels > a channel > Explainer).

The values are the `explainer.*` entries of the Video options, so there is one
store: `own_channels.video_options` (per channel) over the global
`video_options` setting, both in the database. WhisperRadar merges them into
each production's imgtovideo.json and the explainer engine reads them there.
"""
from __future__ import annotations

from pathlib import Path

from flask import abort, send_from_directory

from . import db
from .video import settings_spec as ss

FIELDS = (("mode", "visual_mode"), ("theme", "explainer.theme"), ("hand_font", "explainer.hand_font"),
          ("heading_font", "explainer.heading_font"),
          ("body_font", "explainer.body_font"), ("heading_color", "explainer.heading_color"),
          ("text_color", "explainer.text_color"), ("background", "explainer.background"),
          ("text_weight", "explainer.text_weight"), ("letter_spacing", "explainer.letter_spacing"),
          ("pen_speed", "explainer.pen_speed"), ("scenes_per_video", "explainer.scenes_per_video"),
          ("diagram_style", "explainer.diagram_style"))
CHALK = {"white": "#F4F1E8", "yellow": "#F2D75B", "green": "#7FD08A", "red": "#F0746B",
         "orange": "#F5A04A", "blue": "#6BB6F0", "pink": "#F08CC4", "purple": "#B49BF0",
         "grey": "#9C998F"}
FONT_DIR = Path(__file__).resolve().parents[1] / "fonts"

THEME_LABELS = {
    "chalkboard": "chalkboard - dark board, chalk colours",
    "whiteboard_light": "whiteboard - light board, dark ink",
    "finance_dark": "finance dark - motion graphics",
    "nature_parchment": "nature - parchment",
    "living_warm": "living - warm dark",
}


def font_files() -> list[str]:
    if not FONT_DIR.is_dir():
        return []
    return sorted(p.name for p in FONT_DIR.iterdir()
                  if p.suffix.lower() in (".ttf", ".otf"))


MODE_LABELS = {
    "images": "normal channel - pictures and clips (image engine)",
    "whiteboard": "explainer - hand-drawn chalkboard / whiteboard",
    "motion_graphics": "explainer - motion graphics (charts, counters, text)",
}


def choices() -> dict:
    fonts = font_files()
    return {"modes": [(m, MODE_LABELS.get(m, m))
                      for m in ss.BY_PATH["visual_mode"]["choices"]],
            "themes": [(t, THEME_LABELS.get(t, t))
                       for t in ss.BY_PATH["explainer.theme"]["choices"]],
            "hand_fonts": fonts, "fonts": fonts, "colors": CHALK}


def context(conn, own_channels) -> dict:
    """Template variables: choices, each channel's saved values, the global ones."""
    glob = ss.load_global(conn)
    per = {}
    for ch in own_channels:
        saved = ss.load_channel(conn, ch["id"])
        per[ch["id"]] = {key: saved.get(path, "") for key, path in FIELDS}
    return {"explainer_choices": choices(),
            "explainer_global": {key: glob.get(path, "") for key, path in FIELDS},
            "explainer_values": per}


def save(conn, oc_id: int, form) -> str:
    """Store the posted Explainer fields for the channel; blank = inherit.
    Returns an error message ("" = saved). Other video options are untouched."""
    values = {p: v for p, v in ss.load_channel(conn, oc_id).items()
              if not p.startswith("explainer.") and p != "visual_mode"}
    errors = []
    for key, path in FIELDS:
        raw = (form.get("explainer_" + key) or "").strip()
        if not raw:
            continue
        try:
            values[path] = ss.coerce(ss.BY_PATH[path], raw)
        except ValueError as exc:
            errors.append(f"Explainer {key.replace('_', ' ')}: {exc}")
    if not errors:
        errors = ss.validate(values)
    if errors:
        return "; ".join(errors)
    ss.save_channel(conn, oc_id, values)
    return ""


def register(app, cfg) -> None:
    @app.get("/explainer/fonts/<name>")
    def explainer_font_file(name):
        """Serve a repo font so the channel form can preview it (only the
        .ttf/.otf files in fonts/, nothing else)."""
        if name not in font_files():
            abort(404)
        return send_from_directory(str(FONT_DIR), name, max_age=3600)
