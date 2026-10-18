"""Validate explainer.json into SceneSpecs with human-readable errors.

Every error names the scene/object/event so the user can fix the JSON without
reading code. Validation is strict: invalid input must fail before encoding.
"""
from __future__ import annotations

import json
import os
import re

from . import schema as sc
from . import themes
from . import strokes_library
from . import glyphs

_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,47}$")


class ExplainerError(Exception):
    """Raised with a list of human-readable error strings."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors[:5]))


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _norm_box(v, where, errors):
    if (not isinstance(v, list) or len(v) != 4
            or not all(_is_num(x) for x in v)):
        errors.append(f"{where}: box must be [x, y, w, h] numbers.")
        return None
    x, y, w, h = (float(c) for c in v)
    if w <= 0 or h <= 0:
        errors.append(f"{where}: box width/height must be positive.")
        return None
    if x < -0.01 or y < -0.01 or x + w > 1.01 or y + h > 1.01:
        errors.append(f"{where}: box leaves the canvas "
                      f"([{x:g}, {y:g}, {w:g}, {h:g}] normalized).")
        return None
    return (x, y, w, h)


def _norm_points(v, where, errors):
    if not isinstance(v, list) or len(v) < 2:
        errors.append(f"{where}: path needs at least 2 points.")
        return None
    pts = []
    for i, p in enumerate(v):
        if (not isinstance(p, list) or len(p) != 2
                or not all(_is_num(c) for c in p)):
            errors.append(f"{where}: path point {i + 1} must be [x, y].")
            return None
        x, y = float(p[0]), float(p[1])
        if x < -0.01 or x > 1.01 or y < -0.01 or y > 1.01:
            errors.append(f"{where}: path point {i + 1} leaves the canvas.")
            return None
        pts.append((x, y))
    return pts


def _parse_object(raw, scene_id, errors, project_folder):
    if not isinstance(raw, dict):
        errors.append(f"Scene {scene_id}: object entries must be JSON objects.")
        return None
    oid = raw.get("id")
    otype = raw.get("type")
    if not isinstance(oid, str) or not _ID_RE.match(oid):
        errors.append(f"Scene {scene_id}: object id must be 1-48 chars, "
                      "starting with a letter.")
        return None
    where = f"Scene {scene_id}, object {oid}"
    if otype not in sc.OBJECT_TYPES:
        errors.append(f"{where}: unknown type \"{otype}\" "
                      f"(supported: {', '.join(sc.OBJECT_TYPES)}).")
        return None
    box = _norm_box(raw.get("box"), where, errors)
    if box is None:
        return None

    obj = sc.ObjectSpec(id=oid, type=otype, box=box)
    obj.text = raw.get("text", "") if isinstance(raw.get("text"), str) else ""
    style = raw.get("style")
    obj.style = style if isinstance(style, str) and style else "body"

    if otype == sc.PATH:
        obj.points = _norm_points(raw.get("points"), where, errors)
        if obj.points is None:
            return None
        obj.closed = bool(raw.get("closed", False))
        obj.arrow = bool(raw.get("arrow", False))
        if obj.arrow and obj.closed:
            errors.append(f"{where}: an arrow head needs an open path.")
            return None

    if otype == sc.BAR:
        frac = raw.get("fraction", 1.0)
        if not _is_num(frac) or not (0.0 < float(frac) <= 1.0):
            errors.append(f"{where}: fraction must be within (0, 1].")
            return None
        obj.fraction = float(frac)
        direction = raw.get("direction", sc.BAR_HORIZONTAL)
        if direction not in sc.BAR_DIRECTIONS:
            errors.append(f"{where}: direction must be "
                          f"{sc.BAR_HORIZONTAL} or {sc.BAR_VERTICAL}.")
            return None
        obj.direction = direction

    if otype == sc.COUNTER:
        frm, to = raw.get("from", 0.0), raw.get("to", 0.0)
        if not _is_num(frm) or not _is_num(to):
            errors.append(f"{where}: from/to must be numbers.")
            return None
        obj.from_value, obj.to_value = float(frm), float(to)
        fmt = raw.get("format", sc.FMT_PLAIN)
        if fmt not in sc.FORMATTERS:
            errors.append(f"{where}: unknown format \"{fmt}\" "
                          f"(supported: {', '.join(sc.FORMATTERS)}).")
            return None
        obj.format = fmt
        dec = raw.get("decimals", 0)
        if not _is_int(dec) or not (0 <= dec <= 4):
            errors.append(f"{where}: decimals must be an integer 0..4.")
            return None
        obj.decimals = int(dec)

    if otype in (sc.GRID, sc.BLOCKS):
        cols, rows = raw.get("columns"), raw.get("rows")
        if not _is_int(cols) or not _is_int(rows) or cols < 1 or rows < 1:
            errors.append(f"{where}: columns/rows must be positive integers.")
            return None
        obj.columns, obj.rows = int(cols), int(rows)
        if otype == sc.BLOCKS:
            count, filled = raw.get("count"), raw.get("filled")
            if (not _is_int(count) or not _is_int(filled)
                    or count < 1 or filled < 0 or filled > count):
                errors.append(f"{where}: 1 <= filled <= count must hold "
                              "(integer block counts).")
                return None
            if count > cols * rows:
                errors.append(f"{where}: count {count} exceeds the "
                              f"{cols}x{rows} grid ({cols * rows} cells).")
                return None
            obj.count, obj.filled = int(count), int(filled)

    if otype == sc.IMAGE:
        asset = raw.get("asset")
        if not isinstance(asset, str) or not asset.strip():
            errors.append(f"{where}: image objects need an asset path.")
            return None
        err = _check_asset(asset.strip(), project_folder)
        if err:
            errors.append(f"{where}: {err}")
            return None
        obj.asset = asset.strip().replace("\\", "/")
        fit = raw.get("fit", "contain")
        if fit not in ("contain", "stretch"):
            errors.append(f"{where}: fit must be contain or stretch.")
            return None
        obj.fit = fit

    if otype == sc.STROKE_ASSET:
        asset = raw.get("asset")
        if not isinstance(asset, str) or not asset.strip():
            errors.append(f"{where}: stroke_asset objects need an asset "
                          "(builtin:<name> or a project-relative .json).")
            return None
        obj.asset = asset.strip().replace("\\", "/")
        try:
            parsed = strokes_library.load(obj.asset, project_folder)
        except ValueError as e:
            errors.append(f"{where}: {e}")
            return None
        obj._stroke_count = len(parsed.strokes)
        order = raw.get("stroke_order", "authored")
        if order not in ("authored", "group"):
            errors.append(f"{where}: stroke_order must be authored or group.")
            return None
        obj.stroke_order = order
        obj.fill_after_draw = bool(raw.get("fill_after_draw", False))

    if otype == sc.HANDWRITING:
        if not obj.text.strip():
            errors.append(f"{where}: handwriting objects need text.")
            return None
        obj.fallback_typed = bool(raw.get("fallback_typed", False))
        missing = glyphs.missing_glyphs(obj.text)
        if missing and not obj.fallback_typed:
            errors.append(
                f"{where}: no handwriting glyph(s) for {', '.join(repr(c) for c in missing)} "
                f"(supported: {glyphs.SUPPORTED}). Shorten the text, or set "
                "\"fallback_typed\": true to render it as typed text instead.")
            return None
        if missing:
            obj._stroke_count = 0   # typed fallback: no stroke budget needed
        else:
            obj._stroke_count = sum(
                len(glyphs.glyph_asset(c).strokes)
                for c in obj.text if not c.isspace())

    iv = raw.get("initial_visible")
    if iv is not None:
        if not isinstance(iv, bool):
            errors.append(f"{where}: initial_visible must be true or false.")
            return None
        obj.initial_visible = iv
    return obj


def _check_asset(rel, project_folder):
    """Project-relative asset path; traversal forbidden."""
    norm = os.path.normpath(rel.replace("\\", "/"))
    if os.path.isabs(norm) or norm.startswith("..") or ":" in norm:
        return "asset paths must stay inside the project folder."
    if project_folder and not os.path.isfile(os.path.join(project_folder, norm)):
        return f"asset file not found: {rel}"
    return None


def _parse_event(raw, scene_id, obj_ids, errors):
    if not isinstance(raw, dict):
        errors.append(f"Scene {scene_id}: event entries must be JSON objects.")
        return None
    target = raw.get("target")
    action = raw.get("action")
    where = f"Scene {scene_id}, event(target={target!r}, action={action!r})"
    if not isinstance(target, str) or target not in obj_ids:
        errors.append(f"{where}: target must be an object id in this scene.")
        return None
    if action not in sc.ACTIONS:
        errors.append(f"{where}: unknown action \"{action}\" "
                      f"(supported: {', '.join(sc.ACTIONS)}).")
        return None
    at = sc.CuePoint.from_dict(raw.get("at"))
    if at is None:
        errors.append(f"{where}: at must be {{\"cue\": <int>, \"edge\": \"start\"|\"end\"}}.")
        return None
    ev = sc.EventSpec(target=target, action=action, at=at)
    dur = raw.get("duration_ms", 0)
    if not _is_int(dur) or dur < 0:
        errors.append(f"{where}: duration_ms must be a non-negative integer.")
        return None
    ev.duration_ms = int(dur)
    off = raw.get("offset_ms", 0)
    if not _is_int(off):
        errors.append(f"{where}: offset_ms must be an integer (may be negative).")
        return None
    ev.offset_ms = int(off)
    allowed = sc.ACTION_TARGETS[action]
    otype = obj_ids[target]
    if otype not in allowed:
        errors.append(f"{where}: action {action} cannot target a {otype} object.")
        return None
    if action == sc.SLIDE:
        direction = raw.get("direction")
        if direction not in sc.SLIDE_DIRECTIONS:
            errors.append(f"{where}: slide needs direction "
                          f"{', '.join(sc.SLIDE_DIRECTIONS)}.")
            return None
        ev.direction = direction
        off = raw.get("offset_px", 48)
        if not _is_int(off) or off < 0:
            errors.append(f"{where}: offset_px must be a non-negative integer.")
            return None
        ev.offset_px = int(off)
    if action == sc.COUNT:
        fmt = raw.get("format")
        if fmt is not None:
            if fmt not in sc.FORMATTERS:
                errors.append(f"{where}: unknown counter format \"{fmt}\".")
                return None
            ev.format = fmt
        dec = raw.get("decimals", 0)
        if not _is_int(dec) or not (0 <= dec <= 4):
            errors.append(f"{where}: decimals must be an integer 0..4.")
            return None
        ev.decimals = int(dec)
        pre = raw.get("prefix", "")
        if not isinstance(pre, str):
            errors.append(f"{where}: prefix must be a string.")
            return None
        ev.prefix = pre
    if action == sc.EMPHASIS:
        role = raw.get("role", "default")
        if role not in sc.ROLES:
            errors.append(f"{where}: role must be one of {', '.join(sc.ROLES)}.")
            return None
        ev.role = role
    return ev


def parse_doc(data, project_folder: str = ""):
    """dict -> ExplainerDoc. Raises ExplainerError with all errors found."""
    errors = []
    if not isinstance(data, dict):
        raise ExplainerError(["explainer.json must contain a JSON object."])
    version = data.get("schema_version")
    if version not in sc.SUPPORTED_SCHEMA_VERSIONS:
        raise ExplainerError([
            f"explainer.json schema_version must be one of "
            f"{', '.join(str(v) for v in sc.SUPPORTED_SCHEMA_VERSIONS)} "
            f"(got {version!r})."])
    style_name = data.get("style")
    if style_name is None:
        style_name = sc.STYLE_MOTION
    if style_name not in sc.STYLES:
        errors.append(f"style must be one of {', '.join(sc.STYLES)} "
                      f"(got {style_name!r}).")
        style_name = sc.STYLE_MOTION
    canvas = data.get("canvas") or {}
    w, h = canvas.get("width", sc.DEFAULT_CANVAS[0]), canvas.get("height", sc.DEFAULT_CANVAS[1])
    if not _is_int(w) or not _is_int(h) or w < 2 or h < 2 or w % 2 or h % 2:
        errors.append("canvas width/height must be even integers >= 2.")
        w, h = sc.DEFAULT_CANVAS
    theme_name = data.get("theme")
    if theme_name is not None and not isinstance(theme_name, str):
        errors.append("theme must be a theme name string.")
        theme_name = None
    theme = themes.resolve(theme_name)
    if style_name == sc.STYLE_WHITEBOARD and theme_name is None:
        theme = themes.resolve("whiteboard_light")

    scene_ids = set()
    scenes = []
    raw_scenes = data.get("scenes")
    if not isinstance(raw_scenes, list) or not raw_scenes:
        raise ExplainerError(["explainer.json needs a non-empty scenes list."])
    for raw in raw_scenes:
        if not isinstance(raw, dict):
            errors.append("scenes entries must be JSON objects.")
            continue
        sid = raw.get("id")
        if not isinstance(sid, str) or not _ID_RE.match(sid):
            errors.append("a scene id must be 1-48 chars, starting with a letter.")
            continue
        if sid.lower() in scene_ids:
            errors.append(f"Scene {sid}: duplicate scene id.")
            continue
        scene_ids.add(sid.lower())
        where = f"Scene {sid}"

        shot_id = raw.get("shot_id")
        if not isinstance(shot_id, str) or not shot_id.strip():
            errors.append(f"{where}: shot_id is required "
                          "(the scene must reference an existing shot).")
            continue

        cr = raw.get("cue_range")
        if (not isinstance(cr, list) or len(cr) != 2
                or not _is_int(cr[0]) or not _is_int(cr[1])
                or cr[0] < 1 or cr[1] < cr[0]):
            errors.append(f"{where}: cue_range must be [first_cue, last_cue], "
                          "1-based, first <= last.")
            continue

        obj_ids = {}
        objects = []
        for raw_obj in raw.get("objects", []):
            obj = _parse_object(raw_obj, sid, errors, project_folder)
            if obj is None:
                continue
            if obj.id.lower() in obj_ids:
                errors.append(f"{where}: duplicate object id {obj.id}.")
                continue
            obj_ids[obj.id.lower()] = obj.type
            objects.append(obj)

        events = []
        for raw_ev in raw.get("events", []):
            ev = _parse_event(raw_ev, sid, obj_ids, errors)
            if ev is not None:
                events.append(ev)

        camera = raw.get("camera")
        if camera is not None:
            errors.append(f"{where}: camera is not supported in MVP "
                          "(diagram scenes use a static full-frame viewport).")
        # whiteboard sanity: draw_strokes durations must be feasible (doc 16.7)
        for ev in events:
            if ev.action == sc.DRAW_STROKES:
                obj = next((o for o in objects if o.id == ev.target), None)
                if obj is None:
                    continue
                count = getattr(obj, "_stroke_count", 0)
                if count and ev.duration_ms < count * 120:
                    errors.append(
                        f"{where}: {ev.target} needs {count} strokes but only "
                        f"{ev.duration_ms} ms is budgeted (~{count * 120} ms "
                        "minimum for readable drawing) - grow duration_ms or "
                        "split the drawing across cues.")
        scenes.append(sc.SceneSpec(sid, shot_id.strip(), (int(cr[0]), int(cr[1])),
                                   objects, events))
    if errors:
        raise ExplainerError(errors)
    if not scenes:
        raise ExplainerError(["explainer.json contains no valid scenes."])
    return sc.ExplainerDoc(int(version), theme, (int(w), int(h)), scenes,
                           style=style_name)


def load(path: str, project_folder: str = ""):
    with open(path, encoding="utf-8-sig") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as e:
            raise ExplainerError([f"explainer.json is not valid JSON: {e}"]) from None
    return parse_doc(data, project_folder)


def event_property_conflicts(doc):
    """Two events writing the same object property at overlapping times
    is rejected by the timing compiler; here we catch the identical-time
    same-property case early and return human-readable errors."""
    errors = []
    for scene in doc.scenes:
        seen = {}
        for ev in scene.events:
            prop = sc.ACTION_PROPERTY[ev.action]
            key = (ev.target, prop, ev.at.cue, ev.at.edge)
            if key in seen:
                errors.append(
                    f"Scene {scene.id}: two {prop} events target {ev.target} "
                    f"at the same cue point (cue {ev.at.cue} {ev.at.edge}).")
            seen[key] = True
    return errors
