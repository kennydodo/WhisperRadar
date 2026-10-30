"""ImgToVideo's options as WhisperRadar settings.

One spec, built from the `ProjectOptions` dataclasses, drives the Video options
page, the validation and the merge into a production's imgtovideo.json. Values
live at two levels (global in the `video_options` setting, per own channel in
`own_channels.video_options`), both a flat JSON {"motion.easing": "ease_in", ...}.
A blank / missing value inherits (channel -> global -> the program default).
Output width/height are NOT here: the Render resolution setting owns them.
"""
from __future__ import annotations

import json
import re
from dataclasses import fields, is_dataclass
from pathlib import Path

from . import models as m
from . import options as opt

GROUP_TITLES = {
    "output": "Output", "timing": "Shot length", "motion": "Motion",
    "transitions": "Transitions", "naming": "File naming",
    "render": "Render and encoder", "scene_inference": "Scene detection",
    "shotlist": "Shotlist limits", "sound": "Sound", "text": "Text overlays",
    "explainer": "Explainer look", "": "Planner",
}
GROUP_ORDER = ["output", "timing", "motion", "transitions", "render", "sound", "text",
               "explainer", "scene_inference", "shotlist", "naming", ""]
_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
            "slow", "slower", "veryslow"]
def _font_choices() -> list:
    """Hand fonts a channel can pick: every .ttf/.otf in the repo fonts/ folder
    (read when the app starts) plus the built-in pen letters."""
    folder = Path(__file__).resolve().parents[2] / "fonts"
    names = sorted(p.name for p in folder.glob("*")
                   if p.suffix.lower() in (".ttf", ".otf")) if folder.is_dir() else []
    return names


def _theme_choices() -> list:
    from .explainer import themes
    return list(themes.THEMES)


CHOICES = {
    "explainer.theme": _theme_choices(),
    "explainer.hand_font": ["strokes"] + _font_choices(),
    "explainer.text_weight": ["thin", "normal", "bold"],
    "explainer.letter_spacing": ["tight", "normal", "wide"],
    "explainer.pen_speed": ["slow", "normal", "fast"],
    "explainer.scenes_per_video": ["few", "normal", "many"],
    "explainer.diagram_style": ["balanced", "icons", "minimal"],
    "explainer.scene_fade": ["none", "short", "long"],
    "explainer.heading_font": _font_choices(),
    "explainer.body_font": _font_choices(),
    "motion.easing": list(m.EASINGS),
    "transitions.kind": list(m.TRANSITION_KINDS),
    "transitions.scene_boundary_kind": list(m.TRANSITION_KINDS),
    "transitions.alignment": list(m.TRANSITION_ALIGNMENTS),
    "render.preview_preset": _PRESETS, "render.final_preset": _PRESETS,
    "render.encoder": ["auto", "libx264", "h264_nvenc", "h264_amf", "h264_qsv"],
    "sound.default_pop": [p[0] for p in opt.POP_VARIANTS],
    "planner": ["v1", "v2"],
    "visual_mode": list(opt.VISUAL_MODES),
    "channel_type": __import__("whisperradar.channel_types", fromlist=["keys"]).keys(),
    "explainer.format": ["whiteboard", "motion_graphics"],
    "text.auto_style": ["caption", "stat", "title", "label", "lower-third"],
    "text.auto_position": ["top-left", "top-center", "top-right", "middle-left", "center",
                           "middle-right", "bottom-left", "bottom-center", "bottom-right"],
}
HELP = {
    "explainer.theme": "Board and chalk colours of explainer videos. chalkboard = dark board, "
                       "chalk colours (default look for new work); whiteboard_light = light board, "
                       "dark ink. Blank = whatever the production's explainer.json says.",
    "explainer.hand_font": "Handwriting in explainer scenes. Pick a hand font from the repo fonts/ "
                           "folder (text writes on left to right) or strokes (the built-in pen "
                           "letters, limited character set). Blank = the theme's default.",
    "explainer.heading_font": "Font of typed headings and labels in explainer scenes. Blank = the theme's.",
    "explainer.body_font": "Font of typed body text in explainer scenes. Blank = the theme's.",
    "explainer.heading_color": "Colour of scene titles: white, yellow, green, red, orange, blue, pink, "
                               "purple, grey, or #RRGGBB. Blank = chalk yellow.",
    "explainer.text_color": "Colour of labels, points and footers: a chalk colour name or #RRGGBB. "
                            "Blank = chalk white.",
    "explainer.text_weight": "How heavy the hand-written text looks: thin (the font as designed), normal "
                             "or bold (thickened like chalk). Blank = thin.",
    "explainer.letter_spacing": "Space between the letters of hand-written text: tight, normal or wide. "
                                "Blank = normal.",
    "explainer.pen_speed": "How fast the pen draws: slow, normal or fast. Blank = normal. A scene with a lot "
                           "to draw still speeds up on its own to fit the narration.",
    "explainer.scenes_per_video": "How many scenes the planner aims for: few (longer scenes), normal or many "
                                  "(a new picture more often). Blank = normal.",
    "explainer.diagram_style": "How picture-heavy the scenes are: balanced, icons (an icon with most points) "
                               "or minimal (words and numbers, few icons). Blank = balanced.",
    "explainer.scene_fade": "A soft dip to the board colour where one pen-drawn scene gives way to the next: "
                            "none (a hard cut), short (0.25 s) or long (0.5 s). Scenes keep their length. Blank = none.",
    "explainer.background": "Colour of the board as #RRGGBB (for example #0F2A24 for a green chalkboard). "
                            "Blank = the theme's board.",
    "text.auto_values": "Show the numbers, years and amounts the narration says on screen "
                        "(found in the subtitles, no LLM). Each appears when its cue starts and "
                        "goes when the sentence is over. A channel whose brief has 'text drawn "
                        "by the renderer' turns this on by itself unless you set it here.",
    "text.auto_style": "Look of the automatic value text (stat = big number).",
    "text.auto_position": "Where the automatic value text sits.",
    "text.auto_min_seconds": "An automatic value stays on screen at least this long.",
    "text.auto_max_seconds": "An automatic value never stays longer than this.",
    "output.fps": "Frames per second of the final video.",
    "channel_type": "What the channel makes: standard (pictures and clips) or explainer (drawn scenes from "
                    "a scene plan). Explainers skip the visual style, character bible, references, image "
                    "generation and image camera motion; their saved values are kept. Blank = decided by "
                    "the older visual mode below.",
    "explainer.format": "How an explainer channel is presented: whiteboard (hand-drawn) or motion_graphics "
                        "(straight lines, charts, counters). Blank = the visual mode below, else whiteboard.",
    "visual_mode": "images = stills/clips (default). motion_graphics or "
                   "whiteboard = the production is an explainer: scenes come "
                   "from explainer.json anchored to subtitles.srt.",
    "timing.min_image_seconds": "No image is held shorter than this when the narration allows.",
    "timing.preferred_image_seconds": "The hold the planner aims for.",
    "timing.max_image_seconds": "The longest an image is held.",
    "timing.floor_image_seconds": "Hard floor; below this a shot is a fault.",
    "timing.warn_hold_seconds": "Holds longer than this are warned about.",
    "motion.auto_motion_enabled": "Pick push-in / zoom-out / pan for shots without a motion code.",
    "motion.static_every_min_shots": "A still shot comes back at least every this many shots...",
    "motion.static_every_max_shots": "...and at most every this many.",
    "motion.motion_duration_ms": "0 = the whole shot.",
    "transitions.cut_percent": "Share of the cuts inside a scene that are a hard cut (no transition). "
                               "The three shares are scaled to add up to 100.",
    "transitions.crossfade_percent": "Share of the cuts inside a scene that use the transition kind above (crossfade).",
    "transitions.slide_left_percent": "Share of the cuts inside a scene that slide left. Only shots that also "
                                      "zoom or pan get a slide, never a still shot.",
    "transitions.slide_right_percent": "Share of the cuts inside a scene that slide right.",
    "transitions.slide_up_percent": "Share of the cuts inside a scene that slide up.",
    "transitions.slide_down_percent": "Share of the cuts inside a scene that slide down.",
    "transitions.alignment": "late = the transition ends on the cue, centered = straddles it.",
    "render.preview_bframes": "Keep at 0 for previews (more B-frames made the zoom shimmer).",
    "render.encoder": "auto picks NVENC / AMF / QSV when present, else libx264.",
    "render.supersample_target_width": "Zoom smoothing grid; 0 turns it off.",
    "shotlist.prompt_max_chars": "0 = no limit.",
    "shotlist.master_prompt_max_chars": "0 = no limit.",
    "naming.image_extensions": "Comma separated, with the dot (.png, .jpg).",
}
_SKIP = {"schema_version", "output.width", "output.height"}


def _label(path: str) -> str:
    name = path.split(".")[-1]
    text = name.replace("_", " ")
    text = re.sub(r"\bpercent\b", "(%)", text)
    text = re.sub(r"\bseconds\b", "(s)", text)
    text = re.sub(r"\bms\b", "(ms)", text)
    text = re.sub(r"\bcrf\b", "CRF", text)
    text = re.sub(r"\bfps\b", "FPS", text)
    text = re.sub(r"\bbframes\b", "B-frames", text)
    text = re.sub(r"\bchars\b", "characters", text)
    text = re.sub(r"\bpop\b", "pop sound", text)
    return text[:1].upper() + text[1:]


def _kind(default) -> str:
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, int):
        return "int"
    if isinstance(default, float):
        return "float"
    if isinstance(default, list):
        return "list"
    return "text"


def build_spec() -> list[dict]:
    """[{path, group, label, kind, default, choices, help}], grouped order."""
    base = opt.ProjectOptions()
    out = []

    def walk(obj, prefix):
        for f in fields(obj):
            val = getattr(obj, f.name)
            path = f"{prefix}{f.name}"
            if is_dataclass(val):
                walk(val, path + ".")
                continue
            if path in _SKIP:
                continue
            group = prefix.rstrip(".") if prefix else ""
            kind = "choice" if path in CHOICES else _kind(val)
            out.append({"path": path, "group": group,
                        "title": GROUP_TITLES.get(group, group.title()),
                        "label": _label(path), "kind": kind, "default": val,
                        "choices": CHOICES.get(path), "help": HELP.get(path, "")})

    walk(base, "")
    out.sort(key=lambda s: GROUP_ORDER.index(s["group"])
             if s["group"] in GROUP_ORDER else 99)
    return out


SPEC = build_spec()
BY_PATH = {s["path"]: s for s in SPEC}


def coerce(spec: dict, raw):
    """Typed value from posted text, or raise ValueError."""
    kind = spec["kind"]
    if kind == "bool":
        if raw in (True, False):
            return bool(raw)
        low = str(raw).strip().lower()
        if low in ("1", "true", "on", "yes"):
            return True
        if low in ("0", "false", "off", "no"):
            return False
        raise ValueError("yes or no")
    if kind == "int":
        return int(str(raw).strip())
    if kind == "float":
        return float(str(raw).strip().replace(",", "."))
    if kind == "list":
        items = [x.strip() for x in str(raw).split(",") if x.strip()]
        if not items:
            raise ValueError("at least one entry")
        return items
    if kind == "choice":
        val = str(raw).strip()
        if val not in spec["choices"]:
            raise ValueError("one of " + ", ".join(spec["choices"]))
        return val
    return str(raw).strip()


def parse_form(form) -> tuple[dict, list[str]]:
    """Posted fields `v::<path>` -> ({path: value}, errors). Blank = inherit."""
    values, errors = {}, []
    for s in SPEC:
        raw = form.get("v::" + s["path"])
        if s["kind"] == "bool":
            if raw in (None, "", "inherit"):
                continue
        elif raw is None or str(raw).strip() == "":
            continue
        try:
            values[s["path"]] = coerce(s, raw)
        except (ValueError, TypeError) as exc:
            errors.append(f"{s['title']} - {s['label']}: {exc}")
    return values, errors


def to_nested(values: dict) -> dict:
    out: dict = {}
    for path, val in values.items():
        node = out
        parts = path.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = val
    return out


def validate(values: dict) -> list[str]:
    """Problems of the options these values would produce (all others default)."""
    opts = opt.ProjectOptions()
    opt._apply(opts, to_nested(values))
    return opts.validate()


def _clean(raw) -> dict:
    try:
        data = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    except ValueError:
        data = {}
    return {k: v for k, v in data.items() if k in BY_PATH} if isinstance(data, dict) else {}


def load_global(conn) -> dict:
    from .. import db
    return _clean(db.get_setting(conn, "video_options"))


def save_global(conn, values: dict) -> None:
    from .. import db
    db.set_setting(conn, "video_options", json.dumps(values))


def load_channel(conn, oc_id) -> dict:
    from .. import db
    row = db.get_own_channel(conn, oc_id)
    try:
        return _clean(row["video_options"]) if row is not None else {}
    except (IndexError, KeyError):
        return {}


def save_channel(conn, oc_id, values: dict) -> None:
    from .. import db
    db.update_own_channel(conn, int(oc_id),
                          video_options=json.dumps(values) if values else None)


def effective(conn, own_channel_id) -> dict:
    """channel overrides on top of the global ones."""
    from .. import channel_types
    vals = load_global(conn)
    if own_channel_id not in (None, ""):
        channel = load_channel(conn, own_channel_id)
        # An explicit legacy channel mode is still a channel-level choice. Resolve
        # its type/format before overlaying it, or a newer global type wins instead.
        if not channel.get("channel_type") and channel.get("visual_mode"):
            channel = dict(channel)
            channel["channel_type"] = channel_types.from_visual_mode(channel["visual_mode"]).key
            if channel_types.type_of(channel).formats and not channel.get("explainer.format"):
                channel["explainer.format"] = channel_types.format_of(channel)
        vals.update(channel)
    return channel_types.normalize(vals)


# ---- merge into the production's imgtovideo.json ----------------------------

SIDECAR = "imgtovideo.wr.json"


def _get(data: dict, path: str):
    node = data
    for p in path.split("."):
        if not isinstance(node, dict) or p not in node:
            return None, False
        node = node[p]
    return node, True


def _set(data: dict, path: str, val) -> None:
    node = data
    parts = path.split(".")
    for p in parts[:-1]:
        if not isinstance(node.get(p), dict):
            node[p] = {}
        node = node[p]
    node[parts[-1]] = val


def _del(data: dict, path: str) -> None:
    parts = path.split(".")
    chain = [data]
    for p in parts[:-1]:
        if not isinstance(chain[-1].get(p), dict):
            return
        chain.append(chain[-1][p])
    chain[-1].pop(parts[-1], None)
    for i in range(len(parts) - 1, 0, -1):          # drop emptied groups
        if not chain[i]:
            chain[i - 1].pop(parts[i - 1], None)


def apply_to_project(pdir, values: dict) -> list[str]:
    """Write the settings into <pdir>/imgtovideo.json without touching a value
    somebody else set: a key is ours only if it is absent from the file or
    still holds what we wrote last time (recorded in imgtovideo.wr.json).
    Returns the paths changed."""
    pdir = Path(pdir)
    f = pdir / "imgtovideo.json"
    side = pdir / SIDECAR
    try:
        data = json.loads(f.read_text(encoding="utf-8-sig")) if f.exists() else {}
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    try:
        managed = json.loads(side.read_text(encoding="utf-8")).get("managed", {})
    except (OSError, ValueError, AttributeError):
        managed = {}
    changed = []
    for path, val in values.items():
        cur, present = _get(data, path)
        if not present or (path in managed and cur == managed[path]):
            if not present or cur != val:
                _set(data, path, val)
                changed.append(path)
            managed[path] = val
    for path in [p for p in managed if p not in values]:
        cur, present = _get(data, path)
        if present and cur == managed[path]:
            _del(data, path)
            changed.append(path)
        managed.pop(path, None)
    if changed:
        data.setdefault("schema_version", opt.OPTIONS_SCHEMA_VERSION)
        f.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if managed or side.exists():
        side.write_text(json.dumps({"managed": managed}, indent=1), encoding="utf-8")
    return changed
