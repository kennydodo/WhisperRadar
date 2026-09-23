"""Global Auto Run / producer settings, stored in the `settings` table.

One spec drives everything: the settings page renders the fields from it, the
producer loop and the CLI read typed values through load(), and save()
validates and coerces the posted form. Per-channel overrides live on
own_channels and are NULL when the channel inherits these globals.
"""

import json
import re

# Each entry: key, label, type, default, plus type-specific extras.
# type is one of: bool | int | str | choice | time | map
SPEC: list[dict] = [
    {
        "key": "autorun_enabled", "type": "bool", "default": False,
        "label": "Enable Auto Run",
        "help": "Master switch for the unattended producer. Off = nothing "
                "runs by itself.",
    },
    {
        "key": "services_autostart", "type": "bool", "default": False,
        "label": "Start tools with the dashboard",
        "help": "Bring Renderly's backend and the Flow Driver up when the "
                "dashboard starts, so there is no .bat to run. Off = they are "
                "started on demand by the images stage.",
    },
    {
        "key": "services_managed", "type": "bool", "default": False,
        "label": "Stop services after the images stage",
        "help": "WhisperRadar starts what the images stage needs and, with this "
                "on, stops the ones it started when the stage finishes (the "
                "Flow Driver; the Renderly backend is shared and left alone). "
                "Services you started yourself are never touched.",
    },
    {
        "key": "notify_desktop", "type": "bool", "default": False,
        "label": "Desktop notification",
        "help": "Show a Windows balloon tip when an unattended run pauses or "
                "fails (and on success if enabled below).",
    },
    {
        "key": "notify_webhook_url", "type": "str", "default": "",
        "label": "Notification webhook URL",
        "help": "Optional. A Discord/Slack webhook, an ntfy URL (e.g. "
                "https://ntfy.sh/your-topic), or any endpoint that accepts a "
                "JSON POST. Empty = no webhook.",
    },
    {
        "key": "notify_webhook_kind", "type": "choice", "default": "discord",
        "choices": ["discord", "slack", "ntfy", "json"],
        "label": "Webhook format",
        "help": "How to shape the payload for the URL above. ntfy takes the "
                "message as the body; the others POST JSON.",
    },
    {
        "key": "notify_on_success", "type": "bool", "default": False,
        "label": "Notify on success too",
        "help": "Also notify when an unattended run finishes cleanly. Off = "
                "you only hear about pauses and failures.",
    },
    {
        "key": "scheduler_enabled", "type": "bool", "default": False,
        "label": "Scheduler",
        "help": "Let the running dashboard start Auto Run on its own every "
                "N minutes. It still obeys the run window and the caps above. "
                "Off = you press Produce from channels yourself.",
    },
    {
        "key": "scheduler_interval_minutes", "type": "int", "default": 60,
        "min": 5, "max": 1440,
        "label": "Scheduler interval (minutes)",
        "help": "How often the scheduler checks whether there is anything to "
                "produce. 60 = hourly.",
    },
    {
        "key": "per_day", "type": "int", "default": 1, "min": 0, "max": 50,
        "label": "Productions per day (cap)",
        "help": "Cost guard: the most productions auto-run may create in a "
                "day, across all channels. 0 = no cap.",
    },
    {
        "key": "producer_llm_provider", "type": "provider", "default": "",
        "label": "Producer LLM",
        "help": "Which configured LLM provider picks the topic and writes the "
                "title for Auto Run. Empty = studio.llm_default. Providers are "
                "declared in config.yaml (studio.llm_providers).",
    },
    {
        "key": "candidate_window_days", "type": "int", "default": 90,
        "min": 0, "max": 3650,
        "label": "Candidate window (days)",
        "help": "Only source videos published within this many days are "
                "considered for a new production. 0 = no limit.",
    },
    {
        "key": "default_engine", "type": "choice", "default": "renderly",
        "choices": ["renderly", "flowimagesgen"],
        "label": "Image engine",
        "help": "Which image pipeline new productions use. Renderly = its "
                "API + Flow driver; FlowImagesGen = the standalone Flow CLI.",
    },
    {
        "key": "default_render_mode", "type": "choice", "default": "auto",
        "choices": ["auto", "flow", "api"],
        "label": "Render mode",
        "help": "flow = drive Google Flow; api = the engine's direct API "
                "(Gemini); auto = Flow when its driver is installed, "
                "otherwise the API. Only meaningful for the Renderly engine.",
    },
    {
        "key": "default_upscale", "type": "int", "default": 2, "min": 0, "max": 4,
        "label": "Upscale tier",
        "help": "0 = off, 1-4 = upscale the rendered images.",
    },
    {
        "key": "default_voice", "type": "str", "default": "",
        "label": "Narration voice",
        "help": "OpenSpeaker voice id used for TTS unless a channel or "
                "production overrides it. Empty = the built-in default.",
    },
    {
        "key": "topic_pick", "type": "choice", "default": "newest",
        "choices": ["newest", "llm"],
        "label": "Topic pick",
        "help": "newest = the latest un-produced source video; llm = let the "
                "model choose the best topic among the un-produced ones.",
    },
    {
        "key": "run_window_start", "type": "time", "default": "09:00",
        "label": "Run window start",
        "help": "Auto-run may only start between these times (local).",
    },
    {
        "key": "run_window_end", "type": "time", "default": "23:00",
        "label": "Run window end",
        "help": "End of the daily auto-run window.",
    },
    {
        "key": "seed_dirs", "type": "map", "default": {},
        "label": "Per-genre bible/refs folders",
        "help": "One per line: genre = folder. A new production copies that "
                "folder's bible.md and refs\\ into its working directory.",
    },
]

SPEC_BY_KEY = {entry["key"]: entry for entry in SPEC}

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def defaults() -> dict:
    return {entry["key"]: entry["default"] for entry in SPEC}


def _coerce(entry: dict, raw):
    """Turn one posted/stored raw value into its typed form. Invalid input
    falls back to the spec default (save() reports it separately)."""
    kind = entry["type"]
    if raw is None:
        return entry["default"]
    text = raw if isinstance(raw, str) else str(raw)
    text = text.strip()
    if kind == "bool":
        return text.lower() in ("1", "true", "on", "yes")
    if kind == "int":
        if text == "":
            return entry["default"]
        try:
            value = int(float(text))
        except ValueError:
            return entry["default"]
        if "min" in entry:
            value = max(entry["min"], value)
        if "max" in entry:
            value = min(entry["max"], value)
        return value
    if kind == "choice":
        return text if text in entry["choices"] else entry["default"]
    if kind == "time":
        return text if _TIME_RE.match(text) else entry["default"]
    if kind == "map":
        return parse_seed_dirs(text)
    return text


def parse_seed_dirs(text) -> dict:
    """'genre = folder' lines -> {genre: folder}. A stored JSON object is
    also accepted (settings are persisted as JSON)."""
    if isinstance(text, dict):
        return {str(k): str(v) for k, v in text.items()}
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        genre, sep, folder = line.partition("=")
        genre, folder = genre.strip(), folder.strip()
        if sep and genre and folder:
            out[genre] = folder
    return out


def format_seed_dirs(mapping) -> str:
    if isinstance(mapping, str):
        mapping = parse_seed_dirs(mapping)
    return "\n".join(f"{k} = {v}" for k, v in sorted((mapping or {}).items()))


def load(conn) -> dict:
    """Typed settings: stored values with the spec defaults filled in."""
    from . import db

    stored = db.all_settings(conn)
    out = {}
    for entry in SPEC:
        out[entry["key"]] = _coerce(entry, stored.get(entry["key"]))
    return out


def row_get(row, key, default=None):
    """sqlite3.Row lookup that tolerates a missing column or a NULL."""
    if row is None:
        return default
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None or value == "" else value


def for_production(conn, prod) -> dict:
    """Effective values for a production, most specific wins:
    global settings <- the production's own channel <- the production.

    Per-channel fields are NULL/empty when the channel inherits the global,
    so a value only overrides when it is actually set. Returns the resolved
    dict plus the own-channel row and its Renderly mirror name.
    """
    from . import db

    glob = load(conn)
    own = db.get_own_channel(conn, row_get(prod, "own_channel_id")) \
        if prod is not None else None
    own_upscale = row_get(own, "default_upscale", glob["default_upscale"])
    own_per_day = row_get(own, "per_day")
    return {
        "voice": (row_get(prod, "voice") or row_get(own, "default_voice")
                  or glob["default_voice"] or None),
        "engine": row_get(own, "default_engine", glob["default_engine"]),
        "render_mode": (row_get(prod, "render_mode")
                        or row_get(own, "default_render_mode")
                        or glob["default_render_mode"]),
        "upscale": int(own_upscale),
        "per_day": (int(own_per_day) if own_per_day is not None
                    else int(glob["per_day"])),
        "topic_pick": row_get(own, "topic_pick", glob["topic_pick"]),
        # the rest of the auto-run criteria are per channel too
        "run_window_start": row_get(own, "run_window_start",
                                    glob["run_window_start"]),
        "run_window_end": row_get(own, "run_window_end",
                                  glob["run_window_end"]),
        "candidate_window_days": int(row_get(own, "candidate_window_days",
                                             glob["candidate_window_days"]) or 0),
        "producer_llm_provider": (row_get(own, "producer_llm_provider")
                                  or glob["producer_llm_provider"] or None),
        "autorun_enabled": bool(glob["autorun_enabled"])
                           and bool(row_get(own, "autorun_enabled", 1)),
        "bible_dir": row_get(own, "bible_dir"),
        "refs_dir": row_get(own, "refs_dir"),
        # text defaults seeded into a new production's style.md / bible.md
        "style": row_get(own, "style"),
        "bible": row_get(own, "bible"),
        # Google Flow project URL for the FlowImagesGen engine, when the
        # channel sets one (callers fall back to the global config value).
        "flow_project_url": row_get(own, "flow_project_url"),
        "own_channel": own,
        "own_channel_name": row_get(own, "name"),
        "renderly_channel_name": (row_get(own, "renderly_channel_name")
                                  or row_get(own, "name") or "whisperradar"),
    }


def save(conn, form: dict) -> tuple[dict, list[str]]:
    """Validate + persist a posted form. Returns (values, warnings).

    Absent keys are left untouched, so a partial form never wipes settings.
    """
    from . import db

    values: dict = {}
    warnings: list[str] = []
    for entry in SPEC:
        key = entry["key"]
        if key not in form:
            continue
        raw = form[key]
        values[key] = _coerce(entry, raw)
        if entry["type"] == "time" and not _TIME_RE.match((raw or "").strip()):
            warnings.append(f"{entry['label']}: expected HH:MM - kept "
                            f"{entry['default']}")
        elif entry["type"] == "int" and (raw or "").strip() != "":
            try:
                int(float(raw))
            except ValueError:
                warnings.append(f"{entry['label']}: not a number - kept "
                                f"{entry['default']}")
    for key, value in values.items():
        entry = SPEC_BY_KEY[key]
        stored = (json.dumps(value, ensure_ascii=False)
                  if entry["type"] == "map" else
                  ("1" if value else "0") if entry["type"] == "bool" else
                  str(value))
        db.set_setting(conn, key, stored)
    return values, warnings
