"""Explainer look as a channel setting (My channels > a channel > Explainer).

The values are the `explainer.*` entries of the Video options, so there is one
store: `own_channels.video_options` (per channel) over the global
`video_options` setting, both in the database. WhisperRadar merges them into
each production's imgtovideo.json and the explainer engine reads them there.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from flask import abort, redirect, render_template, request, send_from_directory

from . import channel_types, db, explainer_presets
from .video import settings_spec as ss
from .video.explainer import font_styles

FIELDS = (("preset", "explainer.preset"), ("channel_type", "channel_type"), ("format", "explainer.format"), ("mode", "visual_mode"), ("theme", "explainer.theme"), ("font_style", "explainer.font_style"), ("title_size", "explainer.title_size"), ("footers", "explainer.footers"), ("camera", "explainer.camera"), ("hand_font", "explainer.hand_font"),
          ("heading_font", "explainer.heading_font"),
          ("body_font", "explainer.body_font"), ("heading_color", "explainer.heading_color"),
          ("text_color", "explainer.text_color"), ("background", "explainer.background"),
          ("text_weight", "explainer.text_weight"), ("letter_spacing", "explainer.letter_spacing"), ("icon_weight", "explainer.icon_weight"), ("text_size", "explainer.text_size"),
          ("pen_speed", "explainer.pen_speed"), ("scenes_per_video", "explainer.scenes_per_video"),
          ("diagram_style", "explainer.diagram_style"), ("scene_fade", "explainer.scene_fade"),
          ("scene_genre", "explainer.scene_genre"), ("motion_min_share", "explainer.motion_min_share"),
          ("text_run_max", "explainer.text_run_max"), ("text_run_seconds", "explainer.text_run_seconds"))
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
            "font_styles": [(k, font_styles.LABELS[k]) for k in font_styles.STYLES], "hand_fonts": fonts, "fonts": fonts, "colors": CHALK}


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
    presets = explainer_presets.all_presets(conn)
    overrides = {}
    for ch in own_channels:
        saved = ss.load_channel(conn, ch["id"])
        pv = presets.get(saved.get("explainer.preset", ""), {}).get("values", {})
        overrides[ch["id"]] = [
            {"label": explainer_presets.PATH_NAMES.get(p, p.split(".")[-1].replace("_", " ")),
             "own": saved[p], "preset": pv[p]}
            for p in explainer_presets.LOOK_PATHS
            if pv and p in pv and saved.get(p) not in (None, "") and saved[p] != pv[p]]
    return {"explainer_choices": choices(),
            "explainer_global": gv,
            "explainer_values": per,
            "explainer_presets": [{"key": k, "label": v["label"], "help": v.get("help", ""),
                                   "summary": explainer_presets.summary(v["values"]), "builtin": v["builtin"]}
                                  for k, v in presets.items()],
            "explainer_overrides": overrides}


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
    preset = values.get("explainer.preset", "")
    if preset and preset not in explainer_presets.all_presets(conn):
        errors.append(f"Explainer preset: {preset!r} does not exist (see Explainer presets).")
    if not errors and form.get("explainer_reset_custom") and preset:
        # "use the preset as it is": drop the channel's own look fields so the preset shows through
        values = {p: v for p, v in values.items() if p not in explainer_presets.LOOK_PATHS}
    if not errors:
        # blank type = inherit. A format picked while the type is "standard" is remembered, not used.
        # An absent field preserves the remembered format (older/partial forms).
        # An explicitly blank field means the user selected default/inherit.
        fmt = typed.get("format", "") if "explainer_format" in form else keep.get("explainer.format", "")
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


    # ---- the presets page (rarely needed: pick a preset on the channel itself) ----------------

    def _fields():
        return [{"path": p, "label": explainer_presets.PATH_NAMES.get(p, ss.BY_PATH[p]["label"].lower()),
                 "choices": ss.BY_PATH[p]["choices"]}
                for p in explainer_presets.LOOK_PATHS]

    @app.get("/explainer/presets")
    def explainer_presets_page():
        copy_from = (request.args.get("from") or "").strip()
        conn = db.connect(cfg.db_path)
        try:
            db.init_db(conn)
            allp = explainer_presets.all_presets(conn)
            rows = [{"key": k, "label": v["label"], "help": v.get("help", ""), "builtin": v["builtin"],
                     "summary": explainer_presets.summary(v["values"]), "used_by": explainer_presets.used_by(conn, k)}
                    for k, v in allp.items()]
        finally:
            conn.close()
        base = allp.get(copy_from)
        return render_template("explainer_presets.html", presets=rows, fields=_fields(), copy_from=copy_from,
                               form_values=dict(base["values"]) if base else {},
                               form_label=("" if not base or base["builtin"] else base["label"]),
                               msg=request.args.get("msg"), error=request.args.get("error"))

    @app.post("/explainer/presets/save")
    def explainer_presets_save():
        values, errors = {}, []
        for p in explainer_presets.LOOK_PATHS:
            raw = (request.form.get("v::" + p) or "").strip()
            if raw:
                try:
                    values[p] = ss.coerce(ss.BY_PATH[p], raw)
                except (ValueError, TypeError) as exc:
                    errors.append(f"{p.split('.')[-1].replace('_', ' ')}: {exc}")
        conn = db.connect(cfg.db_path)
        try:
            db.init_db(conn)
            if not errors:
                try:
                    key = explainer_presets.save_custom(conn, request.form.get("label", ""), values)
                except ValueError as exc:
                    errors.append(str(exc))
        finally:
            conn.close()
        if errors:
            return redirect("/explainer/presets?error=" + quote("; ".join(errors)))
        return redirect("/explainer/presets?msg=" + quote("Saved preset " + key + ". Pick it on a channel (My channels > Explainer)."))

    @app.post("/explainer/presets/delete")
    def explainer_presets_delete():
        key = (request.form.get("key") or "").strip()
        conn = db.connect(cfg.db_path)
        try:
            db.init_db(conn)
            ok = explainer_presets.delete_custom(conn, key)
        finally:
            conn.close()
        return redirect("/explainer/presets?" + ("msg=" + quote("Deleted.") if ok else "error=" + quote("No such preset of yours.")))
