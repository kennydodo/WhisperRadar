"""Explainer presets: a named bundle of the look settings, picked with one dropdown on a channel.

A channel's effective explainer settings are layered, lowest first:

    global Video options  <  the channel's preset  <  the channel's own explicit fields

so a field set on the channel still wins ("Customize"), and a channel with no preset behaves exactly as
before. A preset is a flat {setting path: value} dict over the `explainer.*` settings (theme, video type
(`explainer.scene_genre`), text size and weight, fonts, colours, pen speed ...). Built-in presets live
here; the user's own are stored in the database setting `explainer_presets` and edited on /explainer/presets.
The chosen preset's key is the channel setting `explainer.preset`; it is resolved in
`settings_spec.effective` and never written into a production's imgtovideo.json.
"""
from __future__ import annotations

import json
import re

from .video import settings_spec as ss

SETTING = "explainer_presets"
PRESET_PATH = "explainer.preset"

# the settings a preset may carry: the look and the scene set, nothing about channel type or format
LOOK_PATHS = (
    "explainer.theme", "explainer.scene_genre", "explainer.font_style", "explainer.title_size",
    "explainer.hand_font", "explainer.heading_font", "explainer.body_font",
    "explainer.heading_color", "explainer.text_color", "explainer.background",
    "explainer.text_weight", "explainer.text_size", "explainer.letter_spacing", "explainer.icon_weight",
    "explainer.pen_speed", "explainer.scenes_per_video", "explainer.diagram_style", "explainer.scene_fade",
    "explainer.motion_min_share", "explainer.text_run_max", "explainer.text_run_seconds",
)
PATH_NAMES = {"explainer.scene_genre": "video type"}

BUILTIN = {
    "chalkboard": {
        "label": "Chalkboard explainer",
        "help": "Dark chalkboard, normal text. For general explainers.",
        "values": {"explainer.theme": "chalkboard", "explainer.scene_genre": "tech"},
    },
    "whiteboard": {
        "label": "Whiteboard explainer",
        "help": "Light board, dark ink.",
        "values": {"explainer.theme": "whiteboard_light", "explainer.scene_genre": "tech"},
    },
    "coding_lesson": {
        "label": "Coding lesson",
        "help": "Dark chalkboard with small, thin text so typed code stays the star. The planner may use the "
                "code lesson and the LLM pictures.",
        "values": {"explainer.theme": "chalkboard", "explainer.scene_genre": "tech",
                   "explainer.text_size": "small", "explainer.text_weight": "thin"},
    },
    "finance_dark": {
        "label": "Finance (dark, graphic)",
        "help": "Finance motion graphics; the planner leaves out tech-only scenes.",
        "values": {"explainer.theme": "finance_dark", "explainer.scene_genre": "finance"},
    },
}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).strip().lower()).strip("_")[:40]


def _clean_values(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {p: v for p, v in raw.items() if p in LOOK_PATHS and p in ss.BY_PATH and v not in (None, "")}


def custom(conn) -> dict:
    """The user's presets: {key: {"label", "values"}} (built-in keys are never shadowed)."""
    from . import db
    try:
        data = json.loads(db.get_setting(conn, SETTING) or "{}")
    except ValueError:
        data = {}
    out = {}
    if isinstance(data, dict):
        for key, item in data.items():
            if key in BUILTIN or not isinstance(item, dict) or not slug(key):
                continue
            out[key] = {"label": str(item.get("label") or key)[:60], "help": "",
                        "values": _clean_values(item.get("values"))}
    return out


def all_presets(conn) -> dict:
    """Built-in first, then the user's: {key: {"label", "help", "values", "builtin"}}."""
    out = {k: dict(v, builtin=True) for k, v in BUILTIN.items()}
    out.update({k: dict(v, builtin=False) for k, v in custom(conn).items()})
    return out


def values_of(conn, key) -> dict:
    """The look settings of a preset ({} for a blank or unknown key)."""
    if not key:
        return {}
    return dict(all_presets(conn).get(key, {}).get("values", {}))


def _store(conn, data: dict) -> None:
    from . import db
    db.set_setting(conn, SETTING, json.dumps(data))


def save_custom(conn, label: str, values: dict, key: str = "") -> str:
    """Create or update a user preset; returns its key. Raises ValueError with a readable message."""
    label = str(label or "").strip()
    if not label:
        raise ValueError("Give the preset a name.")
    key = key or slug(label)
    if not key:
        raise ValueError("The name needs letters or numbers.")
    if key in BUILTIN:
        raise ValueError("That name is a built-in preset; choose another.")
    clean = _clean_values(values)
    if not clean:
        raise ValueError("Set at least one value in the preset.")
    problems = ss.validate(clean)
    if problems:
        raise ValueError("; ".join(problems))
    data = {k: {"label": v["label"], "values": v["values"]} for k, v in custom(conn).items()}
    data[key] = {"label": label[:60], "values": clean}
    _store(conn, data)
    return key


def delete_custom(conn, key: str) -> bool:
    data = {k: {"label": v["label"], "values": v["values"]} for k, v in custom(conn).items()}
    if key not in data:
        return False
    del data[key]
    _store(conn, data)
    return True


def used_by(conn, key: str) -> list:
    """Names of the own channels that currently use this preset."""
    from . import db
    return [ch["name"] for ch in db.list_own_channels(conn)
            if ss.load_channel(conn, ch["id"]).get(PRESET_PATH) == key]


def summary(values: dict) -> str:
    """'theme chalkboard, video type tech, text size small' for display."""
    parts = []
    for path in LOOK_PATHS:
        if path in values:
            parts.append(f"{PATH_NAMES.get(path, path.split('.')[-1].replace('_', ' '))} {values[path]}")
    return ", ".join(parts)
