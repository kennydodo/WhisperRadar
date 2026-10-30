"""Explainer look as a channel setting (My channels > a channel > Explainer).

The values are the `explainer.*` entries of the Video options, so there is one
store: `own_channels.video_options` (per channel) over the global
`video_options` setting, both in the database. WhisperRadar merges them into
each production's imgtovideo.json and the explainer engine reads them there.
"""
from __future__ import annotations

from pathlib import Path

from flask import abort, send_from_directory

from . import channel_types, db
from .video import settings_spec as ss

FIELDS = (("channel_type", "channel_type"), ("format", "explainer.format"), ("mode", "visual_mode"), ("theme", "explainer.theme"), ("hand_font", "explainer.hand_font"),
          ("heading_font", "explainer.heading_font"),
          ("body_font", "explainer.body_font"), ("heading_color", "explainer.heading_color"),
          ("text_color", "explainer.text_color"), ("background", "explainer.background"),
          ("text_weight", "explainer.text_weight"), ("letter_spacing", "explainer.letter_spacing"),
          ("pen_speed", "explainer.pen_speed"), ("scenes_per_video", "explainer.scenes_per_video"),
          ("diagram_style", "explainer.diagram_style"), ("scene_fade", "explainer.scene_fade"))
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
            "types": [(t.key, t.label) for t in channel_types.TYPES.values()],
            "type_formats": {t.key: [(f, channel_types.FORMAT_LABELS.get(f, f)) for f in t.formats]
                             for t in channel_types.TYPES.values()},
            "type_caps": {t.key: {"images": t.image_pipeline, "scenes": t.scene_planning}
                          for t in channel_types.TYPES.values()},
            "themes": [(t, THEME_LABELS.get(t, t))
                       for t in ss.BY_PATH["explainer.theme"]["choices"]],
            "hand_fonts": fonts, "fonts": fonts, "colors": CHALK}


def context(conn, own_channels) -> dict:
    """Template variables: choices, each channel's saved values, the global ones."""
    glob = ss.load_global(conn)
    per = {}
    for ch in own_channels:
        saved = ss.load_channel(conn, ch["id"])
        vals = {key: saved.get(path, "") for key, path in FIELDS}
        if not vals["channel_type"] and saved.get("visual_mode"):
            # a channel set up before channel types: read its visual mode as the matching type
            t = channel_types.from_visual_mode(saved["visual_mode"])
            vals["channel_type"] = t.key
            if t.formats and not vals["format"]:
                vals["format"] = channel_types.format_of(saved)
        per[ch["id"]] = vals
    gv = {key: glob.get(path, "") for key, path in FIELDS}
    gv["type"] = channel_types.type_of(glob).key        # what "inherit" means right now
    return {"explainer_choices": choices(),
            "explainer_global": gv,
            "explainer_values": per}


def save(conn, oc_id: int, form) -> str:
    """Store the posted Explainer fields for the channel; blank = inherit.
    Returns an error message ("" = saved). Other video options are untouched."""
    values = {p: v for p, v in ss.load_channel(conn, oc_id).items()
              if not p.startswith("explainer.") and p not in ("visual_mode", "channel_type")}
    errors = []
    old = ss.load_channel(conn, oc_id)
    keep = {p: old[p] for p in ("channel_type", "visual_mode", "explainer.format") if p in old}
    typed = {}
    for key, path in FIELDS:
        if key == "mode":
            continue                     # the old visual-mode field is derived from type + format below
        raw = (form.get("explainer_" + key) or "").strip()
        if key in ("channel_type", "format"):
            typed[key] = raw
            continue
        if not raw:
            continue
        try:
            values[path] = ss.coerce(ss.BY_PATH[path], raw)
        except ValueError as exc:
            errors.append(f"Explainer {key.replace('_', ' ')}: {exc}")
    legacy = (form.get("explainer_mode") or "").strip()      # the pre-type form field
    if legacy and not typed.get("channel_type"):
        if legacy in ss.BY_PATH["visual_mode"]["choices"]:
            t = channel_types.from_visual_mode(legacy)
            typed["channel_type"] = t.key
            if t.formats:
                typed["format"] = t.formats[t.visual_modes.index(legacy)]
        else:
            errors.append("Explainer mode: one of " + ", ".join(ss.BY_PATH["visual_mode"]["choices"]))
    if typed.get("channel_type") and typed["channel_type"] not in channel_types.TYPES:
        errors.append("Explainer channel type: one of " + ", ".join(channel_types.keys()))
    if typed.get("format") and typed["format"] not in ss.BY_PATH["explainer.format"]["choices"]:
        errors.append("Explainer format: one of " + ", ".join(ss.BY_PATH["explainer.format"]["choices"]))
    if not errors:
        # blank type = inherit. A format picked while the type is "standard" is remembered, not used.
        fmt = typed.get("format") or keep.get("explainer.format", "")
        values = channel_types.apply_choice(values, typed.get("channel_type", ""), fmt)
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
