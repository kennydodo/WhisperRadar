"""Save and load My Channels settings as a JSON file.

Export writes every own channel (or one) with all of its settings, minus the
fields that only mean something on this machine's database or Renderly
(`id`, `renderly_channel_id`, `renderly_channel_name`). Import reads that file
back: a channel is matched by NAME (case-insensitive); an existing channel is
left alone unless `overwrite` is set, a new one is created. Values are checked
the same way the channel form checks them, anything unusable is dropped and
reported, never saved.

File shape::

    {"format": "whisperradar-channels", "version": 1,
     "exported_at": "...", "channels": [{"name": "...", ...}, ...]}
"""
from __future__ import annotations

import json
from datetime import datetime

from . import briefs, db, settings, studio

FORMAT = "whisperradar-channels"
VERSION = 1

# machine-specific: never exported, never imported
_SKIP = {"id", "renderly_channel_id", "renderly_channel_name"}

_INT = {"active": (0, 1), "default_upscale": (0, 4),
        "candidate_window_days": (0, 3650), "script_max_attempts": (1, 10),
        "shotlist_max_attempts": (1, 8), "per_day": (0, 50),
        "generate_references": (0, 1), "autorun_enabled": (0, 1),
        "brief_reveal": (0, 2)}
_FLOAT = {"script_min_rating": (1.0, 10.0), "script_max_overlap": (0.0, 1.0),
          "shotlist_min_alignment": (0.0, 1.0), "brief_min_hold": (0.0, 600.0),
          "brief_max_hold": (0.0, 600.0)}
_JSON = {"watched_channels", "brief_custom", "brief_types"}
_PROVIDER = {"producer_llm_provider", "script_judge_provider",
             "shotlist_judge_provider"}
_MULTILINE = {"style", "bible", "brief_presentation", "description"}


def exportable_fields() -> list[str]:
    return sorted(db._OWN_CHANNEL_FIELDS - _SKIP)


def _export_row(row) -> dict:
    out = {"name": row["name"]}
    for key in exportable_fields():
        if key == "name":
            continue
        try:
            value = row[key]
        except (IndexError, KeyError):
            continue
        if value is None:
            continue
        if key in _JSON:
            try:
                value = json.loads(value) if isinstance(value, str) else value
            except ValueError:
                continue
        out[key] = value
    return out


def export_channels(conn, keys=None) -> dict:
    """The export document for the channels in `keys` (ids or names; a single
    id/name is fine) or, with none given, for all of them."""
    if keys in (None, "", []):
        rows = db.list_own_channels(conn)
    else:
        if not isinstance(keys, (list, tuple)):
            keys = [keys]
        rows, seen = [], set()
        for key in keys:
            row = db.get_own_channel(conn, key)
            if row is not None and row["id"] not in seen:
                seen.add(row["id"])
                rows.append(row)
    return {"format": FORMAT, "version": VERSION,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "channels": [_export_row(r) for r in rows]}


def _clean(key, value, known_watched, provider_names, notes, label):
    """One incoming value -> what to store (None = store NULL = inherit), or
    _DROP when the value is unusable. Reasons go into `notes`."""
    if value is None or value == "":
        return None
    try:
        if key in _INT:
            lo, hi = _INT[key]
            if isinstance(value, bool):
                value = int(value)
            return max(lo, min(hi, int(value)))
        if key in _FLOAT:
            lo, hi = _FLOAT[key]
            return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        notes.append(f"{label}: {key} ignored (not a number)")
        return _DROP
    if key == "watched_channels":
        if not isinstance(value, list):
            notes.append(f"{label}: watched_channels ignored (not a list)")
            return _DROP
        keep = [str(v) for v in value if str(v) in known_watched]
        gone = len(value) - len(keep)
        if gone:
            notes.append(f"{label}: {gone} watched channel(s) not monitored "
                         "here were dropped")
        return json.dumps(keep) if keep else None
    if key == "brief_custom":
        spec = briefs.normalize_custom(value) if isinstance(value, dict) else None
        if not spec or briefs.custom_error(spec):
            notes.append(f"{label}: custom motion mix ignored (invalid)")
            return _DROP
        return json.dumps(spec)
    if key == "brief_types":
        spec = briefs.normalize_types(value) if isinstance(value, dict) else None
        if not spec:
            notes.append(f"{label}: shot-type caps ignored (invalid)")
            return _DROP
        return json.dumps(spec)
    if not isinstance(value, str):
        notes.append(f"{label}: {key} ignored (not text)")
        return _DROP
    value = value.strip("\r\n") if key in _MULTILINE else value.strip()
    if not value:
        return None
    if key in _PROVIDER:
        if value not in provider_names:
            notes.append(f"{label}: {key} '{value}' is not configured here, "
                         "so it inherits")
            return None
        return value
    if key == "render_target":
        value = value.lower()
        return value if value in studio.RENDER_TARGETS else _bad(
            notes, label, key, value)
    if key == "render_resolution":
        value = value.lower()
        return value if value in studio.RENDER_RESOLUTIONS else _bad(
            notes, label, key, value)
    if key == "flow_native_upscale":
        value = value.lower()
        return value if value in settings.FLOW_NATIVE_TIERS else _bad(
            notes, label, key, value)
    if key == "brief_motion":
        value = value.lower()
        return value if (value in briefs.MOTION_PRESETS
                         or value == briefs.CUSTOM_KEY) else _bad(
            notes, label, key, value)
    if key in ("run_window_start", "run_window_end"):
        import re
        return value if re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", value) else _bad(
            notes, label, key, value)
    return value


_DROP = object()


def _bad(notes, label, key, value):
    notes.append(f"{label}: {key} '{value}' is not valid here, so it inherits")
    return None


def import_channels(conn, cfg, payload, overwrite: bool = False) -> dict:
    """Apply an export document. Returns
    {"created": [...], "updated": [...], "skipped": [...], "notes": [...],
     "error": str|None}. Nothing is written when the file is not a channels
    export."""
    res = {"created": [], "updated": [], "skipped": [], "notes": [],
           "error": None}
    if isinstance(payload, dict) and payload.get("format") == FORMAT:
        items = payload.get("channels")
        version = payload.get("version")
        if isinstance(version, int) and version > VERSION:
            res["error"] = (f"This file is version {version}; this app reads "
                            f"up to version {VERSION}. Update WhisperRadar "
                            "first.")
            return res
    elif isinstance(payload, dict) and "channels" in payload:
        items = payload["channels"]
    elif isinstance(payload, list):
        items = payload
    else:
        res["error"] = "Not a WhisperRadar channels file."
        return res
    if not isinstance(items, list) or not items:
        res["error"] = "The file contains no channels."
        return res

    full_export = isinstance(payload, dict) and payload.get("format") == FORMAT
    known_watched = {c["channel_id"] for c in db.list_channels(conn)}
    provider_names = {p["name"] for p in studio.providers(cfg)}
    allowed = set(db._OWN_CHANNEL_FIELDS) - _SKIP
    for item in items:
        if not isinstance(item, dict):
            res["notes"].append("An entry that is not a channel was skipped")
            continue
        name = item.get("name")
        name = name.strip() if isinstance(name, str) else ""
        if not name:
            res["notes"].append("An entry without a name was skipped")
            continue
        existing = db.get_own_channel(conn, name)
        if existing is not None and not overwrite:
            res["skipped"].append(name)
            continue
        fields = {}
        for key, value in item.items():
            if key == "name" or key not in allowed:
                continue
            cleaned = _clean(key, value, known_watched, provider_names,
                             res["notes"], name)
            if cleaned is _DROP:
                continue
            if cleaned is None and existing is None:
                continue          # a new channel already starts on inherit
            fields[key] = cleaned
        if existing is None:
            if fields.get("genre") in (None, ""):
                fields.pop("genre", None)
            db.create_own_channel(conn, name, **fields)
            res["created"].append(name)
        else:
            if full_export:
                # a real export lists every setting that is not on inherit,
                # so overwriting restores it exactly: the rest goes back to
                # inherit instead of keeping a stale local value
                for key in allowed - {"name", "description", "genre",
                                      "active"}:
                    fields.setdefault(key, None)
            if fields.get("genre", "x") is None:
                fields["genre"] = "general"
            if fields.get("active", 1) is None:
                fields.pop("active")
            if fields.get("description", "x") is None:
                fields["description"] = ""
            db.update_own_channel(conn, existing["id"], **fields)
            res["updated"].append(name)
    return res
