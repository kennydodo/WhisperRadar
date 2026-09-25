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
        "key": "llm_default", "type": "provider", "default": "",
        "label": "Default LLM",
        "help": "Which provider writes the style, script and shotlist when a "
                "production or its channel does not set one. Saved in the "
                "database; empty falls back to studio.llm_default in "
                "config.yaml. A channel's producer_llm_provider and a "
                "production's own choice both override this.",
    },
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
        "key": "autorun_resume", "type": "bool", "default": True,
        "label": "Resume unfinished productions",
        "help": "Before creating new productions, continue the auto-run ones "
                "that stopped mid-pipeline - images left over from the "
                "Images-per-run limit, or a Flow refusal that has since "
                "cleared. Only productions the producer created are touched; "
                "anything you are building by hand is left alone.",
    },
    {
        "key": "resume_cooldown_minutes", "type": "int", "default": 60,
        "min": 5, "max": 1440,
        "label": "Resume cooldown (minutes)",
        "help": "Do not re-attempt a production until this long after its last "
                "attempt, so a Flow refusal is not hammered.",
    },
    {
        "key": "resume_per_run", "type": "int", "default": 2, "min": 1, "max": 10,
        "label": "Resumes per run",
        "help": "At most this many unfinished productions are continued in one "
                "auto-run, so a single run cannot sprawl.",
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
        "key": "shotlist_min_alignment", "type": "float", "default": 0.90,
        "min": 0.0, "max": 1.0,
        "label": "Shotlist: minimum prompt detail",
        "help": "Share of shots whose image prompt must state EVERY element "
                "the narration at its cues requires (who, what they are doing, "
                "where, which props, the specific information). A prompt that "
                "under-specifies its scene means the image gets regenerated by "
                "hand later. Checked after planning and BEFORE any image is "
                "rendered. 0.90 = 90% of shots.",
    },
    {
        "key": "shotlist_max_attempts", "type": "int", "default": 4,
        "min": 1, "max": 8,
        "label": "Shotlist: max attempts",
        "help": "How many times to re-plan the shotlist with the under-specified "
                "prompts, the pacing faults and their missing elements fed back. "
                "Structural faults (coverage, order, orphan assets, duplicates) "
                "must always be zero.",
    },
    {
        "key": "shotlist_judge_provider", "type": "provider", "default": "",
        "label": "Shotlist: judge LLM",
        "help": "Which provider audits prompt detail. Empty = automatically a "
                "different provider from the planner.",
    },
    {
        "key": "shotlist_max_hold_seconds", "type": "float", "default": 12.0,
        "min": 4.0, "max": 12.0,
        "label": "Shotlist: max seconds per image",
        "help": "The hard maximum hold for any image - 12s is the ceiling, so this "
                "only lets you TIGHTEN it (4-12s). Checked from the SRT cue "
                "timings BEFORE any image renders. The number of images is NEVER "
                "fixed: the cues drive it, so an image may cover one cue or many "
                "and a dense passage can run to 7-8 images a minute. A plan with "
                "a longer hold re-plans with instructions to split at a meaning "
                "boundary and renumber the scene (a new image takes the next "
                "unused sub-beat). The brief's other rules (no long STATIC hold, "
                "ST only on short holds and ~10% of shots, no motion code above "
                "~40%) are enforced too, and one-image-per-cue is a fault.",
    },
    {
        "key": "script_min_rating", "type": "float", "default": 9.0,
        "min": 1.0, "max": 10.0,
        "label": "Script: minimum rating",
        "help": "The script stage rates each draft 1-10 by rubric and only "
                "accepts one at or above this. Below it, the draft is "
                "regenerated with the judge's feedback.",
    },
    {
        "key": "script_max_overlap", "type": "float", "default": 0.12,
        "min": 0.0, "max": 1.0,
        "label": "Script: target overlap",
        "help": "Share of the script's 5-word sequences allowed to also "
                "appear in the source transcript. 0.12 = 12%. Facts and names "
                "set a floor, so 0 is not realistic.",
    },
    {
        "key": "script_hard_overlap", "type": "float", "default": 0.20,
        "min": 0.0, "max": 1.0,
        "label": "Script: hard overlap limit",
        "help": "A draft above this is regenerated no matter how well it "
                "rated - at this level large runs are copied verbatim.",
    },
    {
        "key": "script_max_attempts", "type": "int", "default": 3,
        "min": 1, "max": 10,
        "label": "Script: max attempts",
        "help": "How many drafts to generate before settling for the best "
                "one. Each attempt is one script call plus one rating call.",
    },
    {
        "key": "script_judge_provider", "type": "provider", "default": "",
        "label": "Script: judge LLM",
        "help": "Which provider rates the script. Empty = automatically a "
                "DIFFERENT provider from the one that wrote it, to avoid "
                "self-preference bias.",
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
        "help": "Delivered image size. 0 = off (native 1K, 1376x768 - below "
                "ImgToVideo's 2304x1296 canvas spec), 1 = HD 1920x1080 (also "
                "below spec), 2 = 2K 2560x1440 (recommended - meets the spec "
                "and matches ImgToVideo's default output), 3 = 2K, "
                "4 = 4K 3840x2160 (over-spec, slower).",
    },
    {
        "key": "default_voice", "type": "str", "default": "",
        "label": "Narration voice",
        "help": "OpenSpeaker voice id used for TTS unless a channel or "
                "production overrides it. Empty = the built-in default.",
    },
    {
        "key": "images_chunk_size", "type": "int", "default": 60,
        "min": 0, "max": 500,
        "label": "Images per run",
        "help": "Render at most this many images in one batch, then stop and "
                "leave the rest to the next run. Flow tolerates roughly 80-100 "
                "automated generations on an account before it starts refusing "
                "(and BOTH engines share the account), so chunking a big "
                "shotlist spreads it across sessions. 0 = no limit.",
    },
    {
        "key": "images_stop_on_failure", "type": "bool", "default": True,
        "label": "Stop the batch when images start failing",
        "help": "Stop instead of grinding through the remaining cards. "
                "FlowImagesGen uses --fail-fast (stops at the first failed "
                "item); the Flow Driver stops after 3 consecutive failed cards. "
                "Everything rendered is kept and the production stays "
                "resumable either way.",
    },
    {
        "key": "generate_references", "type": "bool", "default": True,
        "label": "Generate missing references",
        "help": "Before rendering the shotlist images, generate the reference "
                "images it needs but has no supplied file for (using each "
                "ref's prompt) and put them where the engine can use them. "
                "Refs you DO supply are used as-is, with whatever name they "
                "have; generated ones are named per the CH_/BG_/OBJ_ "
                "convention, capped at 20 per production. Off = the refs "
                "stage is skipped and those refs are simply not attached.",
    },
    {
        "key": "render_resolution", "type": "choice", "default": "2k",
        "choices": ["1080p", "2k", "4k"],
        "choice_labels": {"1080p": "1920x1080 (Full HD)",
                          "2k": "2560x1440 (2K)",
                          "4k": "3840x2160 (4K)"},
        "label": "Render resolution",
        "help": "Output resolution written into the production's "
                "imgtovideo.json (output.width/height) for the preview build "
                "and the NLE export. 2K is ImgToVideo's own default. The "
                "preview draft stays at 960x540 for speed.",
    },
    {
        "key": "render_target", "type": "choice", "default": "premiere",
        "choices": ["premiere", "capcut"],
        "choice_labels": {"premiere": "Premiere Pro",
                          "capcut": "Final Cut (CapCut)"},
        "label": "Render target",
        "help": "What the merge stage exports to. Both targets first build a "
                "fast preview draft (out\\preview.mp4) you can watch in the "
                "dashboard, then write an NLE project: Premiere Pro = an FCP7 "
                "XML to import (File > Import); Final Cut (CapCut) = a CapCut "
                "draft folder to copy into CapCut's draft root.",
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

# The settings page renders these groups in order; a key missing from every
# group lands in "Other", so adding a SPEC entry can never hide it.
GROUPS: list[tuple[str, list[str]]] = [
    ("LLM", ["llm_default", "producer_llm_provider",
             "script_judge_provider", "shotlist_judge_provider"]),
    ("Auto Run", [
        "autorun_enabled", "per_day", "run_window_start", "run_window_end",
        "candidate_window_days", "topic_pick", "producer_llm_provider",
        "autorun_resume", "resume_cooldown_minutes", "resume_per_run",
    ]),
    ("Script quality gate", [
        "script_min_rating", "script_max_overlap", "script_hard_overlap",
        "script_max_attempts", "script_judge_provider",
    ]),
    ("Shotlist gate", [
        "shotlist_min_alignment", "shotlist_max_attempts",
        "shotlist_max_hold_seconds",
        "shotlist_judge_provider",
    ]),
    ("Production defaults", [
        "default_engine", "default_render_mode", "default_upscale",
        "default_voice", "seed_dirs",
    ]),
    ("Image rendering", ["images_chunk_size", "images_stop_on_failure",
                         "generate_references"]),
    ("Video render", ["render_target", "render_resolution"]),
    ("Scheduler", ["scheduler_enabled", "scheduler_interval_minutes"]),
    ("Notifications", [
        "notify_desktop", "notify_webhook_url", "notify_webhook_kind",
        "notify_on_success",
    ]),
    ("Service handling", ["services_autostart", "services_managed"]),
]


def grouped_spec() -> list[tuple[str, list[dict]]]:
    """[(group name, [spec entries])] for the settings page."""
    seen: set[str] = set()
    out: list[tuple[str, list[dict]]] = []
    for name, keys in GROUPS:
        entries = [SPEC_BY_KEY[k] for k in keys if k in SPEC_BY_KEY]
        seen.update(k for k in keys if k in SPEC_BY_KEY)
        if entries:
            out.append((name, entries))
    rest = [e for e in SPEC if e["key"] not in seen]
    if rest:
        out.append(("Other", rest))
    return out

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
    if kind == "float":
        if text == "":
            return entry["default"]
        try:
            value = float(text)
        except ValueError:
            return entry["default"]
        if "min" in entry:
            value = max(entry["min"], value)
        if "max" in entry:
            value = min(entry["max"], value)
        return round(value, 3)
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
        # script quality gate (see the SPEC help text for the semantics)
        "script_min_rating": float(row_get(own, "script_min_rating",
                                           glob["script_min_rating"])),
        "script_max_overlap": float(row_get(own, "script_max_overlap",
                                            glob["script_max_overlap"])),
        "script_hard_overlap": float(glob["script_hard_overlap"]),
        "script_max_attempts": int(row_get(own, "script_max_attempts",
                                           glob["script_max_attempts"])),
        "script_judge_provider": (row_get(own, "script_judge_provider")
                                  or glob["script_judge_provider"] or None),
        # shotlist gate
        "shotlist_min_alignment": float(row_get(own, "shotlist_min_alignment",
                                                glob["shotlist_min_alignment"])),
        "shotlist_max_attempts": int(row_get(own, "shotlist_max_attempts",
                                             glob["shotlist_max_attempts"])),
        "shotlist_judge_provider": (row_get(own, "shotlist_judge_provider")
                                    or glob["shotlist_judge_provider"] or None),
        "shotlist_max_hold_seconds": float(row_get(
            own, "shotlist_max_hold_seconds",
            glob["shotlist_max_hold_seconds"])),
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
        # global-only: which NLE the merge stage exports to (premiere|capcut)
        "render_target": row_get(own, "render_target", glob["render_target"]),
        "render_resolution": row_get(own, "render_resolution",
                                     glob["render_resolution"]),
        # image-batch guards (global only - operational, not per production)
        "images_chunk_size": int(glob["images_chunk_size"]),
        "images_stop_on_failure": bool(glob["images_stop_on_failure"]),
        # the refs stage is per channel (a channel may have no refs at all)
        "generate_references": bool(row_get(own, "generate_references",
                                            int(glob["generate_references"]))),
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
