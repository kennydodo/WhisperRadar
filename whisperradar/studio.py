"""Studio: human-in-the-loop YouTube production pipeline logic.

Every stage can be completed by an automated tool (if its hook is configured)
or by hand (paste text / upload files) - the human stays in charge.
"""

import json
import logging
import os
import random
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

from pathlib import Path

log = logging.getLogger("whisperradar")

# random directives so regenerating a script produces a genuinely fresh take
VARIATION_ANGLES = [
    "Take a slightly different narrative angle than any previous version.",
    "Open with a different hook pattern than a question.",
    "Lead with the most surprising fact and restructure the beats around it.",
    "Use a more story-driven approach built on one concrete anecdote.",
    "Emphasize the practical steps more than the theory.",
    "Frame the topic as a mistake people make and reverse-engineer the fix.",
]


def variation_nudge(attempt: int = 1, overlap: float | None = None,
                    runs: list[str] | None = None,
                    feedback: list[str] | None = None) -> str:
    """The variation instruction. On a retry it carries the previous
    attempt's measured overlap, the passages that were lifted, and the
    judge's notes - a bare 'be more original' changes nothing."""
    angle = random.choice(VARIATION_ANGLES)
    parts = [f"[VARIATION {random.randint(1000, 9999)}] {angle}",
             "Produce a fresh take: different wording, sentence order and "
             "rhythm from any earlier attempt."]
    if attempt > 1:
        parts.append(f"This is attempt {attempt}; earlier drafts were rejected.")
    if overlap is not None:
        parts.append(f"Your previous draft shared {overlap:.1%} of its "
                     f"5-word sequences with the source transcript.")
    if runs:
        shown = "\n".join(f'  - "{r}"' for r in runs[:8])
        parts.append("These passages were lifted almost verbatim - rewrite "
                     f"them from scratch with new wording and structure:\n{shown}")
    if feedback:
        notes = "\n".join(f"  - {f}" for f in feedback[:6])
        parts.append(f"The editor asked for these fixes:\n{notes}")
    parts.append("Reuse only facts, names and numbers - never the source's "
                 "phrasing or sentence structure.")
    return " ".join(parts)
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".flac", ".ogg")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def prod_dir(cfg, pid: int) -> Path:
    """The production's working folder: a user-selected directory when the
    production has one, otherwise data\\studio\\<id>.

    Folders the app creates (or adopts while empty) get a marker file so
    delete knows they are safe to remove."""
    work_dir = None
    try:
        import sqlite3

        conn = sqlite3.connect(cfg.db_path)
        try:
            row = conn.execute(
                "SELECT work_dir FROM productions WHERE id = ?", (pid,)
            ).fetchone()
            if row and row[0]:
                work_dir = row[0]
        finally:
            conn.close()
    except Exception:
        pass
    d = (Path(work_dir).expanduser() if work_dir
         else cfg.studio_dir / str(pid))
    created = not d.exists()
    d.mkdir(parents=True, exist_ok=True)
    if created or not any(d.iterdir()):
        _write_marker(d)
    return d


MARKER = ".whisperradar-production"


def _write_marker(d: Path) -> None:
    try:
        (d / MARKER).touch(exist_ok=True)
    except OSError:
        pass


def is_managed_dir(cfg, pdir: Path) -> bool:
    """True when the production folder was created/adopted by WhisperRadar."""
    try:
        pdir.resolve().relative_to(cfg.studio_dir.resolve())
        return True
    except ValueError:
        return (pdir / MARKER).exists()


def validate_work_dir(cfg, new_dir: str | Path) -> Path:
    """Reject work_dir values that would let production delete wipe
    unrelated folders (drive roots, the program folder, non-empty
    folders WhisperRadar did not create)."""
    p = Path(new_dir).expanduser().resolve()
    if p.parent == p:
        raise RuntimeError(
            f"{p} is a drive root - pick a folder inside it instead")
    base = cfg.base_dir.resolve()
    if p == base:
        raise RuntimeError("work_dir cannot be the WhisperRadar folder")
    try:
        base.relative_to(p)
        raise RuntimeError(
            f"{p} contains the WhisperRadar program folder - not allowed")
    except ValueError:
        pass
    if p.exists() and any(p.iterdir()) and not (p / MARKER).exists():
        raise RuntimeError(
            f"{p} is not empty and is not a WhisperRadar production folder - "
            "pick an empty folder (or one WhisperRadar created before)")
    return p


MOVE_ITEMS = ["script.md", "style.md", "bible.md", "source_transcript.txt",
              "subtitles.srt", "shotlist.json", "shotlist.json.bak",
              "imgtovideo.json", "prompts.txt", "batch_sheet.txt", "final.mp4",
              "audio", "audio_previous", "images", "refs", "out", "versions"]


def move_production_dir(cfg, prod, new_dir: str | None) -> tuple[Path, int]:
    """Move a production's files to new_dir (None = default). Returns
    (final directory, number of items moved). Aborts with an error when
    the destination already contains any production artifact."""
    old = prod_dir(cfg, prod["id"])
    if new_dir:
        dest = Path(new_dir).expanduser().resolve()
        if dest.resolve() != old.resolve():
            dest = validate_work_dir(cfg, dest)
        else:
            _write_marker(dest)  # adopt the current folder as managed
    else:
        dest = cfg.studio_dir / str(prod["id"])
    dest.mkdir(parents=True, exist_ok=True)
    _write_marker(dest)
    if old.resolve() == dest.resolve():
        return dest, 0
    # refuse partial moves: if any artifact already exists at the destination,
    # abort with the full list instead of silently stranding items
    collisions = [item for item in MOVE_ITEMS
                  if (old / item).exists() and (dest / item).exists()]
    if collisions:
        raise RuntimeError(
            f"{dest} already contains: {', '.join(collisions)} - "
            "remove or rename those first, then move again")
    moved = 0
    for item in MOVE_ITEMS:
        src = old / item
        d = dest / item
        if src.exists():
            shutil.move(str(src), str(d))
            moved += 1
    return dest, moved


def find_audio(pid_dir: Path) -> Path | None:
    for ext in AUDIO_EXTS:
        p = pid_dir / f"audio{ext}"
        if p.exists():
            return p
    return None


def find_srt(pid_dir: Path) -> Path | None:
    p = pid_dir / "subtitles.srt"
    return p if p.exists() else None


def find_script(pid_dir: Path) -> Path | None:
    p = pid_dir / "script.md"
    return p if p.exists() else None


def find_style(pid_dir: Path) -> Path | None:
    p = pid_dir / "style.md"
    return p if p.exists() else None


def find_source_transcript(pid_dir: Path) -> Path | None:
    p = pid_dir / "source_transcript.txt"
    return p if p.exists() else None


def find_prompts(pid_dir: Path) -> Path | None:
    p = pid_dir / "prompts.txt"
    return p if p.exists() else None


def find_bible(pid_dir: Path) -> Path | None:
    p = pid_dir / "bible.md"
    return p if p.exists() else None


def seed_production(cfg, conn, prod, log=None) -> dict:
    """Seed a production folder with the channel's art direction.

    Text: the channel's `bible` and `style` are written straight into
    bible.md / style.md. Folders: `bible_dir` (holding bible.md) and
    `refs_dir` (copied into refs\\), else the global per-genre `seed_dirs`
    entry for the production's genre. This is what keeps an unattended run
    from pausing at the shots stage, whose planning brief refuses to plan
    without a bible.

    Idempotent: existing files are never overwritten. Returns
    {bible, style, refs, source} - source is '' when there is nothing to seed.
    """
    from . import settings

    log = log or (lambda m: None)
    if prod is None:
        return {"bible": False, "style": False, "refs": 0, "source": ""}
    pdir = prod_dir(cfg, prod["id"])
    eff = settings.for_production(conn, prod)
    genre = (prod["genre"] if "genre" in prod.keys() else None) or "general"
    seed_map = settings.load(conn).get("seed_dirs") or {}
    seed = seed_map.get(genre)
    if seed is None:  # genres are free text: fall back to a case-insensitive hit
        seed = next((v for k, v in seed_map.items()
                     if k.lower() == genre.lower()), None)

    bible_dir: Path | None = None
    refs_dir: Path | None = None
    if eff["bible_dir"] or eff["refs_dir"] or eff["bible"] or eff["style"]:
        source = eff["own_channel_name"] or "own channel"
        if eff["bible_dir"]:
            bible_dir = Path(eff["bible_dir"]).expanduser()
        if eff["refs_dir"]:
            refs_dir = Path(eff["refs_dir"]).expanduser()
    elif seed:
        # a per-genre seed folder holds bible.md and a refs\ subfolder; leave
        # refs_dir unset so the normalization below picks up seed\refs
        bible_dir = Path(seed).expanduser()
        source = f"seed_dirs[{genre}]"
    else:
        return {"bible": False, "style": False, "refs": 0, "source": ""}

    # a single seed folder keeps the refs in a refs\ subfolder
    if refs_dir is None and bible_dir is not None \
            and (bible_dir / "refs").is_dir():
        refs_dir = bible_dir / "refs"

    copied_bible = False
    copied_style = False
    # a channel's text bible/style is written straight into the production,
    # so an unattended run is not left without the art direction
    if eff["bible"] and not (pdir / "bible.md").exists():
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "bible.md").write_text(str(eff["bible"]).strip() + "\n",
                                       encoding="utf-8")
        copied_bible = True
        log("seeded bible.md from the channel's bible text")
    if eff["style"] and not (pdir / "style.md").exists():
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "style.md").write_text(str(eff["style"]).strip() + "\n",
                                       encoding="utf-8")
        copied_style = True
        log("seeded style.md from the channel's style text")
    elif eff["bible"] and not eff["style"] and not (pdir / "style.md").exists():
        # a channel that only wrote one art bible should not have to repeat it:
        # the shots stage reads style.md for art direction and bible.md for
        # consistency rules, so seed both from the same text
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "style.md").write_text(str(eff["bible"]).strip() + "\n",
                                       encoding="utf-8")
        copied_style = True
        log("seeded style.md from the channel's bible text")
    bible_src = bible_dir / "bible.md" if bible_dir else None
    if bible_src and bible_src.is_file() and not (pdir / "bible.md").exists():
        pdir.mkdir(parents=True, exist_ok=True)
        shutil.copy(bible_src, pdir / "bible.md")
        copied_bible = True
        log(f"seeded bible.md from {bible_src}")

    copied_refs = 0
    if refs_dir and refs_dir.is_dir():
        dest = pdir / "refs"
        for src in sorted(refs_dir.iterdir()):
            if not src.is_file():
                continue
            target = dest / src.name
            if target.exists():
                continue
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, target)
            copied_refs += 1
        if copied_refs:
            log(f"seeded {copied_refs} ref image(s) from {refs_dir}")

    return {"bible": copied_bible, "style": copied_style,
            "refs": copied_refs, "source": source}


def find_images(pid_dir: Path) -> list[Path]:
    img_dir = pid_dir / "images"
    if not img_dir.exists():
        return []
    return sorted(p for p in img_dir.iterdir()
                  if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


def find_final(pid_dir: Path) -> Path | None:
    for candidate in (pid_dir / "out" / "final" / "final.mp4",
                      pid_dir / "final.mp4"):
        if candidate.exists():
            return candidate
    return None


# ------------------------------------------------- Renderly / ImgToVideo --

def renderly_ready(url: str, timeout: int = 2) -> bool:
    def probe() -> bool:
        try:
            with urllib.request.urlopen(f"{url}/api/channels", timeout=timeout):
                return True
        except Exception:
            return False
    return _cached_probe(f"renderly:{url}", probe)


# readiness probes hit services that are usually DOWN; on some machines a
# refused loopback connect costs seconds, so results are cached and refreshed
# in the background - page loads never wait on a probe (except the first)
_PROBE_TTL = 60.0
_probe_cache: dict = {}


def _cached_probe(key: str, fn):
    now = time.monotonic()
    hit = _probe_cache.get(key)
    if hit is None:
        value = fn()
        _probe_cache[key] = (value, now)
        return value
    value, stamped = hit
    if now - stamped >= _PROBE_TTL:
        def refresh():
            try:
                _probe_cache[key] = (fn(), time.monotonic())
            except Exception:
                _probe_cache[key] = (value, time.monotonic())
        threading.Thread(target=refresh, daemon=True).start()
    return value


def ensure_renderly_channel(cfg) -> int:
    """Find or create the legacy 'whisperradar' channel in Renderly.

    Kept for productions with no own channel; own channels resolve their own
    mirror through resolve_renderly_channel(). Returns the Renderly id."""
    if cfg.renderly_channel:
        return int(cfg.renderly_channel)
    channel_id = resolve_renderly_channel(cfg, None, create=True)
    if channel_id is None:
        raise RuntimeError("could not resolve the Renderly 'whisperradar' "
                           "channel - is Renderly running?")
    return channel_id


def renderly_channels(cfg, timeout: int = 10) -> list[dict]:
    """Every Renderly channel (id + name) - the mirror lookup source."""
    with urllib.request.urlopen(f"{cfg.renderly_url}/api/channels",
                                timeout=timeout) as r:
        data = json.loads(r.read())
    return data if isinstance(data, list) else []


# The My Channels page needs the channel list ONCE per load, not once per
# channel. It is also refreshed in the background (stale-while-revalidate) so
# an unreachable or slow Renderly can never stall a page render.
_channel_cache: dict = {"at": 0.0, "data": [], "error": None, "loading": False}


def _refresh_channel_cache(cfg, timeout: int = 4) -> None:
    try:
        data = renderly_channels(cfg, timeout=timeout)
        _channel_cache.update({"at": time.monotonic(), "data": data,
                               "error": None})
    except Exception as exc:  # noqa: BLE001 - status display is best effort
        _channel_cache.update({"at": time.monotonic(), "data": [],
                               "error": str(exc)[:120]})
    finally:
        _channel_cache["loading"] = False


def renderly_channel_list(cfg, ttl: int = 30):
    """(channels, error, known) from the cache, kicking off a background
    refresh when stale. Never blocks: on the first call it returns
    known=False while the refresh runs."""
    now = time.monotonic()
    known = bool(_channel_cache["data"] or _channel_cache["error"])
    if not known or now - _channel_cache["at"] >= ttl:
        if not _channel_cache["loading"]:
            _channel_cache["loading"] = True
            try:
                threading.Thread(target=_refresh_channel_cache, args=(cfg,),
                                 daemon=True).start()
            except Exception as exc:  # noqa: BLE001
                log.warning("could not start the Renderly channel probe: %s",
                            exc)
                _channel_cache["loading"] = False
    return _channel_cache["data"], _channel_cache["error"], known


def _renderly_create_channel(cfg, name: str, description: str = "") -> dict:
    req = urllib.request.Request(
        f"{cfg.renderly_url}/api/channels",
        data=json.dumps({"name": name, "description": description}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _own_channel_name(own_channel) -> str:
    return ((own_channel["renderly_channel_name"] if own_channel else None)
            or (own_channel["name"] if own_channel else None)
            or "whisperradar").strip()


def resolve_renderly_channel(cfg, own_channel=None, create: bool = False):
    """The Renderly channel id for an own channel.

    Identity is the NAME; the stored id is only a cache, so this self-heals
    a stale id, a channel renamed in Renderly, and a channel created there by
    hand. Order: stored id still exists -> adopt by name -> create (only when
    create=True). Returns None when Renderly is unreachable or the channel is
    absent and create is False. Never deletes anything in Renderly.
    """
    if cfg.renderly_channel and own_channel is None:
        return int(cfg.renderly_channel)
    name = _own_channel_name(own_channel)
    stored_id = own_channel["renderly_channel_id"] if own_channel else None
    try:
        channels = renderly_channels(cfg)
    except Exception as exc:  # noqa: BLE001 - never block on a down service
        log.info("Renderly unreachable (%s) - channel '%s' not resolved",
                 exc, name)
        return None
    if stored_id and any(c.get("id") == stored_id for c in channels):
        return stored_id
    match = next((c for c in channels
                  if (c.get("name") or "").lower() == name.lower()), None)
    if match:
        return match["id"]
    if not create:
        return None
    try:
        return _renderly_create_channel(
            cfg, name, "WhisperRadar own channel")["id"]
    except Exception as exc:  # noqa: BLE001
        log.warning("could not create Renderly channel '%s': %s", name, exc)
        return None


def renderly_channel_status(cfg, own_channel, channels=None, error=None,
                            known: bool = True) -> dict:
    """Read-only link state for My Channels - never creates, never blocks.

    `channels`/`error`/`known` come from renderly_channel_list() so one page
    render makes at most one (background) Renderly request."""
    if own_channel is None:
        return {"linked": False, "detail": "no own channel"}
    if channels is None and known:
        channels, error, known = renderly_channel_list(cfg)
    name = _own_channel_name(own_channel)
    stored_id = own_channel["renderly_channel_id"]
    if error:
        if stored_id:
            return {"linked": True, "id": stored_id, "name": name,
                    "detail": f"linked (id {stored_id}) - Renderly "
                              f"unreachable, not verified"}
        return {"linked": False,
                "detail": f"Renderly unreachable ({error[:60]})"}
    if not known:
        if stored_id:
            return {"linked": True, "id": stored_id, "name": name,
                    "detail": f"linked (id {stored_id}) - checking Renderly..."}
        return {"linked": False, "detail": "checking Renderly..."}
    if stored_id and any(c.get("id") == stored_id for c in channels):
        return {"linked": True, "id": stored_id, "name": name,
                "detail": f"linked (id {stored_id})"}
    match = next((c for c in channels
                  if (c.get("name") or "").lower() == name.lower()), None)
    if match:
        return {"linked": False, "id": match["id"], "name": name,
                "detail": f"exists in Renderly (id {match['id']}) - click "
                          f"Link to adopt it"}
    return {"linked": False, "detail": "not in Renderly yet - click Link to "
                                       "create it"}


def sync_renderly_channel(cfg, conn, own_channel, create: bool = True) -> dict:
    """Resolve the own channel's Renderly mirror and persist the link.

    Returns {ok, id, name, created, error}. Safe to call repeatedly; it is
    idempotent and adopts an existing channel of the same name.
    """
    if own_channel is None:
        return {"ok": False, "error": "unknown own channel"}
    name = _own_channel_name(own_channel)
    try:
        channels = renderly_channels(cfg)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Renderly unreachable: {exc}"}
    stored_id = own_channel["renderly_channel_id"]
    if stored_id and any(c.get("id") == stored_id for c in channels):
        return {"ok": True, "id": stored_id, "name": name, "created": False}
    match = next((c for c in channels
                  if (c.get("name") or "").lower() == name.lower()), None)
    created = False
    if match:
        channel_id = match["id"]
    elif create:
        try:
            channel_id = _renderly_create_channel(
                cfg, name, "WhisperRadar own channel")["id"]
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"could not create channel: {exc}"}
        created = True
    else:
        return {"ok": False, "error": "not linked"}
    from . import db

    db.update_own_channel(conn, own_channel["id"],
                          renderly_channel_id=channel_id,
                          renderly_channel_name=name)
    return {"ok": True, "id": channel_id, "name": name, "created": created}


def renderly_upscale(value) -> int:
    """Map WhisperRadar's 0-4 upscale tier onto Renderly's upscale API.

    Renderly accepts only scale 2 or 4 - asking for 1 or 3 returns
    HTTP 400 {"detail":"Scale must be 2 or 4"} and the upscale is silently
    skipped for every image, so map the odd tiers onto the nearest valid one.
    """
    try:
        tier = max(0, min(4, int(value or 0)))
    except (TypeError, ValueError):
        return 0
    if tier == 0:
        return 0
    return 2 if tier <= 2 else 4


def run_imagegen(cfg, pid_dir: Path, channel=None, upscale=None) -> int:
    """Render the production's shotlist images through ImgToVideo.ImageGen
    in Renderly mode. `channel` is the target Renderly channel id (the
    production's own channel mirror); None falls back to the legacy
    'whisperradar' channel. Returns how many new images landed in images\\."""
    repo = cfg.imgtovideo_repo
    if not repo or not Path(repo, "src", "ImgToVideo.ImageGen").exists():
        raise RuntimeError("Set studio.imgtovideo_repo in config.yaml")
    if channel is None:
        channel = ensure_renderly_channel(cfg)
    if upscale is None:
        upscale = cfg.renderly_upscale
    before = {p.name for p in (pid_dir / "images").iterdir()} \
        if (pid_dir / "images").exists() else set()
    cmd = [
        "dotnet", "run", "--project",
        str(Path(repo, "src", "ImgToVideo.ImageGen")),
        "-c", "Release", "--",
        str(pid_dir),
        "--renderly", cfg.renderly_url,
        "--channel", str(channel),
        "--image-size", "1K",
        "--upscale", str(renderly_upscale(upscale if upscale is not None
                                         else cfg.renderly_upscale)),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "")[-500:]
        raise RuntimeError(f"ImageGen failed (exit {result.returncode}): {tail}")
    img_dir = pid_dir / "images"
    new = [p.name for p in img_dir.iterdir() if p.name not in before]
    return len(new)


def flow_driver_dir(cfg) -> Path | None:
    """The Renderly extension-v2 folder (Playwright driver for Google Flow)."""
    if not cfg.flow_driver_dir:
        return None
    p = Path(cfg.flow_driver_dir)
    return p if p.is_absolute() else cfg.base_dir / p


def flow_driver_ready(cfg) -> bool:
    d = flow_driver_dir(cfg)
    return bool(d) and (d / "flow.js").exists() \
        and (d / "node_modules" / "playwright").exists()


def missing_flow_images(cfg, pid_dir: Path) -> int:
    """How many shotlist images are still missing from images\\.

    The Flow Driver reads shotlist.json itself (config `shotlistPath`) and
    resolves per-image ref names on its own, so there is nothing to prepare
    here - this used to also write a `flow_batch.json` that nothing consumed
    (flow.js only ever saw shotlist.json). flow.js has no skip-existing, so the
    caller needs the count to know how much is left."""
    shotlist_path = pid_dir / "shotlist.json"
    if not shotlist_path.exists():
        raise RuntimeError("Generate the shotlist first")
    data = json.loads(shotlist_path.read_text(encoding="utf-8"))
    img_dir = pid_dir / "images"
    img_dir.mkdir(exist_ok=True)
    existing = {p.name for p in img_dir.iterdir() if p.is_file()}
    todo = [i for i in data.get("images", [])
            if isinstance(i, dict) and i.get("file") and i.get("prompt")
            and i["file"] not in existing]
    if not todo:
        raise RuntimeError(
            "All shotlist images already exist - nothing to render")
    return len(todo)


def flow_service_url(cfg) -> str:
    return (cfg.flow_driver_url or "http://127.0.0.1:8030").rstrip("/")


# ------------------------------------------- FlowImagesGen (2nd engine) --
# The standalone Playwright Flow CLI, consumed in place from its own
# checkout (like ImgToVideo and Renderly - never vendored: its Google
# session lives in a gitignored profile\ folder). Invoked as:
#   node src/cli.js generate --job <job.json> --output <dir> ...

# WhisperRadar's upscale 0-4 -> FlowImagesGen's resolution tier names.
FLOWIMAGESGEN_TIERS = {0: "off", 1: "1k", 2: "2k", 3: "3k", 4: "4k"}

# Flow refuses prompts over roughly 2450 characters with the SAME message it
# uses for rate limiting, so the job-wide style is only sent when it fits.
FLOWIMAGESGEN_MAX_PROMPT_CHARS = 2420


def flowimagesgen_dir(cfg) -> Path | None:
    if not cfg.flowimagesgen_repo:
        return None
    d = Path(cfg.flowimagesgen_repo).expanduser()
    return d if d.exists() else None


def flowimagesgen_ready(cfg) -> bool:
    d = flowimagesgen_dir(cfg)
    return bool(d) and (d / "src" / "cli.js").exists() \
        and (d / "node_modules" / "playwright").exists() \
        and shutil.which("node") is not None


def flow_project_url_for(cfg, pid: int,
                         override: str | None = None) -> tuple[str | None, str]:
    """(url, source) for a production's Flow project.

    Precedence: production row -> channel default -> global config. The DB is
    the truth so that regenerating the job cannot lose the URL and make Flow
    create a second project for the same video (see AGENTS.md)."""
    from . import db, settings

    if override and str(override).strip():
        return str(override).strip(), "explicit"
    try:
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
            if prod is not None:
                url = (settings.row_get(prod, "flow_project_url")
                       or "").strip() or None
                if url:
                    return url, "production"
                eff = settings.for_production(conn, prod)
                url = (eff["flow_project_url"] or "").strip() or None
                if url:
                    return url, "channel"
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - never block generation on this
        log.debug("could not read the production's Flow project: %s", exc)
    url = (cfg.flowimagesgen_project_url or "").strip() or None
    return url, "config" if url else "none"


def prepare_flowimagesgen_job(cfg, pid_dir: Path, pid: int,
                              project_url: str | None = None) -> tuple[Path, list[str]]:
    """Build a FlowImagesGen job from the production's shotlist: only the
    images still missing from images\\.

    Per-image refs stay as NAMES and the shotlist's refs registry becomes the
    job's name -> path map, so FlowImagesGen's default refMode "reuse" attaches
    existing Flow project assets by name instead of re-uploading (its own
    README: repeated uploads duplicate project assets). Files in the
    production's refs\\ folder are exposed under their stem, matching how the
    shotlist references them.
    Returns (job file, the output file names to expect)."""
    shotlist_path = pid_dir / "shotlist.json"
    if not shotlist_path.exists():
        raise RuntimeError("Generate the shotlist first")
    data = json.loads(shotlist_path.read_text(encoding="utf-8"))
    registry = {k: v for k, v in (data.get("refs") or {}).items()
                if isinstance(v, str)} if isinstance(data.get("refs"), dict) \
        else {}
    refs_dir = pid_dir / "refs"
    if refs_dir.exists():
        for p in sorted(refs_dir.iterdir()):
            if p.is_file():
                registry.setdefault(p.stem, str(p))
    img_dir = pid_dir / "images"
    img_dir.mkdir(exist_ok=True)
    existing = {p.name for p in img_dir.iterdir() if p.is_file()}
    todo = []
    for item in data.get("images", []):
        if not (isinstance(item, dict) and item.get("file")
                and item.get("prompt") and item["file"] not in existing):
            continue
        entry = {"file": item["file"], "prompt": item["prompt"]}
        if item.get("refs"):
            entry["refs"] = [str(r) for r in item["refs"]]
        todo.append(entry)
    if not todo:
        raise RuntimeError(
            "All shotlist images already exist - nothing to render")

    out_dir = pid_dir / "flow_images"
    job: dict = {
        "name": f"wr-{pid}",
        "outputsDir": str(out_dir),
        "refMode": "reuse",
        "refs": registry,
        "defaults": {"mode": "image", "agent": False, "aspectRatio": "16:9",
                     "outputs": 1, "refMode": "reuse"},
        "images": todo,
    }
    url, source = flow_project_url_for(cfg, pid, project_url)
    if url:
        job["projectUrl"] = url
        log.info("FlowImagesGen: Flow project from %s", source)
    else:
        # Never silent: without a URL Flow opens its landing page and uses the
        # most recent project, which may belong to another video.
        log.warning("FlowImagesGen: no Flow project URL for production %s - "
                    "Flow will fall back to its most recent project, which may "
                    "be the wrong one. Set one on the channel, or run the "
                    "prepare step to create a project for this production.",
                    pid)
        from . import db, settings as _settings

        try:
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            try:
                if db.get_production(conn, pid) is not None:
                    db.update_production(
                        conn, pid,
                        warning="no Flow project URL - Flow may have used the "
                                "wrong project for this production's images")
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass
    # The shotlist's `style` is a Flow "master prompt" for the Renderly
    # driver; here the per-image prompts already carry the art direction and
    # the style alone can exceed Flow's limit, so it is only sent when the
    # whole prompt would still fit.
    style = (data.get("style") or "").strip()
    longest = max((len(i["prompt"]) for i in todo), default=0)
    if style and longest + len(style) + 1 <= FLOWIMAGESGEN_MAX_PROMPT_CHARS:
        job["style"] = style
    elif style:
        log.info("FlowImagesGen: omitting the %d-char style - prompts would "
                 "exceed Flow's %d-char limit (longest prompt %d)",
                 len(style), FLOWIMAGESGEN_MAX_PROMPT_CHARS, longest)
    job_path = pid_dir / "flowimagesgen.json"
    job_path.write_text(json.dumps(job, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    return job_path, [i["file"] for i in todo]


def _flowimagesgen_cmd(args: list[str]) -> list[str]:
    return ["node", "src/cli.js", *args]


def set_flowimagesgen_tier(cfg, upscale: int) -> str | None:
    """Remember the upscale tier in FlowImagesGen's own local config (the
    same thing its web UI does). Returns the tier name."""
    d = flowimagesgen_dir(cfg)
    if not d:
        return None
    tier = FLOWIMAGESGEN_TIERS.get(int(upscale or 0), "off")
    try:
        subprocess.run(_flowimagesgen_cmd(["upscale", "--set-tier", tier]),
                       cwd=str(d), capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("FlowImagesGen: could not set upscale tier %s: %s",
                    tier, exc)
    return tier


def _adopt_flowimagesgen_outputs(pdir: Path, names: list[str],
                                 tier: str) -> list[str]:
    """Copy FlowImagesGen's results into images\\ under the shotlist's own
    file names, preferring the upscaled <stem>_<tier>.png over the master
    (both are written, so the pipeline would otherwise see duplicates)."""
    out_dir = pdir / "flow_images"
    img_dir = pdir / "images"
    img_dir.mkdir(exist_ok=True)
    adopted = []
    for name in names:
        stem, dot, ext = name.rpartition(".")
        stem = stem or name
        ext = ext if dot else ""
        candidates = []
        if tier and tier != "off":
            candidates += [f"{stem}_{tier}.{ext}" if ext else f"{stem}_{tier}"]
        candidates += [name]
        candidates += sorted(p.name for p in out_dir.glob(f"{stem}*")
                             if p.is_file()) if out_dir.exists() else []
        for cand in candidates:
            src = out_dir / cand
            if src.is_file():
                shutil.copy(src, img_dir / name)
                adopted.append(name)
                break
    return adopted


def _safe_log(log, message) -> None:
    """Log a child process line without ever raising. FlowImagesGen prints
    non-ASCII (checkmarks, box drawing) and a cp1252 console raises
    UnicodeEncodeError on write - which used to kill the whole stage and hide
    the real error."""
    if not log:
        return
    try:
        log(message)
    except Exception:  # noqa: BLE001
        try:
            log(str(message).encode("ascii", "replace").decode("ascii"))
        except Exception:  # noqa: BLE001
            pass


def run_imagegen_flowimagesgen(cfg, pid_dir: Path, pid: int,
                               upscale: int | None = None, log=None,
                               cancel=None, project_url: str | None = None) -> int:
    """Render the production's missing shotlist images with FlowImagesGen.

    Returns how many new images landed in images\\. Long-running by design:
    Flow rate-limits automation and the CLI waits it out, so the timeout is
    generous and `cancel` kills the whole process tree."""
    if not flowimagesgen_ready(cfg):
        raise RuntimeError("Set studio.flowimagesgen_repo in config.yaml to "
                           "your FlowImagesGen checkout (and run npm install)")
    repo = flowimagesgen_dir(cfg)
    job_path, names = prepare_flowimagesgen_job(cfg, pid_dir, pid, project_url)
    tier = set_flowimagesgen_tier(cfg, cfg.renderly_upscale if upscale is None
                                  else upscale)
    cmd = _flowimagesgen_cmd(["generate", "--job", str(job_path),
                              "--output", str(pid_dir / "flow_images"),
                              "--no-color"])
    resolved_url = project_url or cfg.flowimagesgen_project_url
    if resolved_url:
        cmd += ["--project-url", resolved_url]
    if log:
        _safe_log(log, f"FlowImagesGen: {len(names)} image(s), "
                       f"upscale tier {tier}")
        _safe_log(log, "$ " + " ".join(cmd))
    proc = subprocess.Popen(cmd, cwd=str(repo), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    tail: list[str] = []
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            tail.append(line)
            del tail[:-40]
            _safe_log(log, line)
            if cancel is not None and cancel():
                raise RuntimeError("stopped by user")
    finally:
        if proc.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               capture_output=True)
            else:
                proc.kill()
        proc.wait(timeout=30)
    if proc.returncode != 0:
        text = " ".join(tail).lower()
        if "rate limit" in text or "refused the generation" in text:
            raise RuntimeError(
                "Flow refused the generation (a reCAPTCHA score on this "
                "browser profile, not a temporary limit). FlowImagesGen stops "
                "the batch on purpose rather than lowering the score further. "
                "Wait a while, then Resume - finished images are kept in its "
                f"state\\wr-{pid}.json. Raising delayBetweenItemsMs in "
                "FlowImagesGen's config/settings.json lowers the risk.")
        if "already in use" in text or "existing browser session" in text:
            raise RuntimeError(
                "FlowImagesGen could not open its browser profile because "
                "another Chrome is already using it - close the FlowImagesGen "
                "UI / that Chrome window, then Resume. (state is kept in its "
                f"state\\wr-{pid}.json)")
        if "executable doesn't exist" in text and "ms-playwright" in text:
            raise RuntimeError(
                "Playwright's browser is not installed - run "
                f"`npx playwright install chromium` in {repo}, or configure "
                "FlowImagesGen to use your system Chrome. (exit "
                f"{proc.returncode})")
        raise RuntimeError(
            f"FlowImagesGen failed (exit {proc.returncode}): "
            + " | ".join(tail[-4:])[:400]
            + f" - state is kept in its state\\wr-{pid}.json so a re-run "
              f"resumes")
    adopted = _adopt_flowimagesgen_outputs(pid_dir, names, tier or "off")
    if not adopted:
        raise RuntimeError("FlowImagesGen produced no images - check the log "
                           "and its debug\\ folder")
    # Flow refuses some prompts on content policy. It retries, then skips the
    # item and carries on, so the batch "succeeds" while images are missing -
    # and the merge would run on an incomplete set. Adopt what exists first
    # (nothing is lost, and a resume only re-renders the gaps), then report.
    missing = [n for n in names if not (pid_dir / "images" / n).exists()]
    if missing:
        raise RuntimeError(
            f"{len(missing)} of {len(names)} image(s) were not produced - "
            f"Flow refused them (usually content policy; see the "
            f"debug\\error-*.png captures). The {len(adopted)} that succeeded "
            f"are kept. Rewrite those prompts (or the shotlist), then Resume: "
            f"only the gaps are re-rendered. Missing: "
            + ", ".join(missing[:12])
            + (" …" if len(missing) > 12 else ""))
    return len(adopted)

def _driver_api(cfg, path: str, method: str = "GET", body=None,
                timeout: int = 8):
    """Call the Flow Driver service (extension-v2\\server.js).

    On an error response the body is included: the driver explains rejections
    in JSON (`{"error": "channel ... does not exist - available: ..."}`), and
    discarding it turned a precise message into a bare "HTTP Error 400"."""
    req = urllib.request.Request(
        flow_service_url(cfg) + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            raw = exc.read().decode("utf-8", "replace").strip()
            if raw:
                try:
                    detail = json.loads(raw).get("error") or raw
                except ValueError:
                    detail = raw
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"HTTP {exc.code} from {path}"
                           + (f": {detail[:300]}" if detail else "")) from exc


def flow_service_status(cfg, timeout: int = 4) -> dict | None:
    """Service status dict, or None when the service is not reachable."""
    try:
        return _driver_api(cfg, "/api/status", timeout=timeout)
    except Exception:
        return None


def _backend_up(cfg) -> bool:
    try:
        with urllib.request.urlopen(cfg.renderly_url + "/api/channels",
                                    timeout=2):
            return True
    except Exception:
        return False


def _spawn_detached(cmd: list, cwd: Path, logfile: str | None = None) -> None:
    flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags |= subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    out = open(cwd / logfile, "ab") if logfile else subprocess.DEVNULL
    subprocess.Popen(cmd, cwd=str(cwd), stdout=out, stderr=out,
                     stdin=subprocess.DEVNULL, creationflags=flags,
                     close_fds=True)


def ensure_flow_services(cfg, log=print) -> None:
    """Make sure the Flow Driver service (8030) is up, and - because imports
    and upscales need it - the Renderly backend (8022).

    Delegates to the service manager, which starts only what is missing and
    tracks what it started so the images stage can stop it again."""
    from . import services

    services.MANAGER.ensure(cfg, ["renderly", "flow-driver"], log_fn=log)


class BatchCancelled(RuntimeError):
    """Raised when a caller-requested cancel interrupts a long batch."""


def run_imagegen_flow(cfg, pid_dir: Path, refs=None, channel: str = "whisperradar",
                      project: str = "", upscale: int | None = None,
                      master: str = "", log=print, cancel=None) -> int:
    """Render missing shotlist images through Google Flow via the Flow Driver
    service. Results land in images\\ under the exact shotlist names; upscaled
    copies produced via Renderly are adopted as the shotlist files.

    The batch is always built from the CURRENT shotlist.json - and if the
    shotlist is edited while the batch runs, the batch is stopped and
    re-prepared from the new plan instead of rendering stale prompts.
    Returns the new image count."""
    d = flow_driver_dir(cfg)
    if not d or not (d / "flow.js").exists():
        raise RuntimeError(
            "Set studio.flow_driver_dir in config.yaml to the Renderly "
            "extension-v2 folder")
    if not (d / "node_modules" / "playwright").exists():
        raise RuntimeError(
            f"Playwright not installed - run: cd {d} && npm install")
    img_dir = pid_dir / "images"
    img_dir.mkdir(exist_ok=True)
    before = {p.name for p in img_dir.iterdir() if p.is_file()}
    ensure_flow_services(cfg, log=log)
    if flow_service_status(cfg) is None:
        raise RuntimeError("Flow Driver service is not reachable on "
                           + flow_service_url(cfg))
    refs = [str(r).strip() for r in (refs or []) if str(r).strip()]
    project = str(project or "").strip()
    config = {
        "shotlistPath": str(pid_dir / "shotlist.json"),
        "outPath": str(img_dir),
        "channel": str(channel or "whisperradar").strip(),
        "project": project,
        "refs": ",".join(refs),
        "master": (master or "").strip(),
        "upscale": renderly_upscale(upscale if upscale is not None
                                    else (cfg.renderly_upscale or 0)),
    }
    shotlist_file = pid_dir / "shotlist.json"
    todo = 0
    rounds = 0
    while True:  # batch rounds - re-prepared whenever shotlist.json changes
        rounds += 1
        try:
            todo = missing_flow_images(cfg, pid_dir)
        except RuntimeError as exc:
            if "nothing to render" in str(exc):
                if rounds > 1:
                    todo = 0  # the edited shotlist is fully rendered already
                    break
                # Every image already exists - that is success, not failure.
                # This is the path taken after filling gaps by hand (uploading
                # the missing images) or re-running a finished production, and
                # it used to fail the stage instead of moving on to merge.
                log("Flow Driver: every shotlist image already exists - "
                    "nothing to render")
                return 0
            raise
        if rounds > 1:
            log(f"Flow Driver: batch re-read from the current shotlist - "
                f"{todo} image(s) left to render")
        log(f"Flow Driver: rendering {todo} missing image(s) via Google Flow "
            f"(a Chrome window will open - leave it running)")
        # The driver owns the batch state: if one is already running (e.g. a
        # previous poller died but Chrome kept rendering) attach to it instead
        # of posting config and starting a second one, which it rejects with
        # "a batch is already running". Do not touch a running batch's config.
        status = flow_service_status(cfg) or {}
        if status.get("running"):
            counts = status.get("counts") or {}
            log(f"Flow Driver: attaching to the batch already running "
                f"({counts.get('ok', 0)}/{counts.get('total', todo)} done, "
                f"{counts.get('failed', 0)} failed) - not starting another")
        else:
            _driver_api(cfg, "/api/config", method="POST", body=config,
                        timeout=15)
            try:
                _driver_api(cfg, "/api/start", method="POST", body={},
                            timeout=15)
            except Exception as exc:
                raise RuntimeError(f"Flow Driver rejected the batch: {exc}")
        seen = 0
        deadline = time.monotonic() + 14400
        restarted = False
        shotlist_mtime = shotlist_file.stat().st_mtime
        while True:
            if cancel and cancel():
                log("cancel requested - stopping the Flow Driver batch")
                flow_stop(cfg)
                raise BatchCancelled(
                    "stop requested during the images stage")
            if time.monotonic() > deadline:
                # do not abandon the batch: it would keep spending credits on
                # cards nobody is waiting for anymore
                flow_stop(cfg)
                raise RuntimeError("Flow Driver batch timed out after 4h")
            time.sleep(3)
            try:
                mtime_now = shotlist_file.stat().st_mtime
            except OSError:
                mtime_now = shotlist_mtime
            if mtime_now != shotlist_mtime:
                # the user re-planned mid-batch: drop this batch and read
                # the current shotlist instead of rendering stale prompts
                log("shotlist.json changed since the batch started - "
                    "stopping the batch and re-reading the current plan "
                    "(delete an image file to force its re-render)")
                flow_stop(cfg)
                restarted = True
                break
            st = flow_service_status(cfg, timeout=10)
            if st is None:
                continue
            lines = st.get("log") or []
            if len(lines) < seen:  # service log window wrapped
                seen = 0
            while seen < len(lines):
                _safe_log(log, lines[seen])
                seen += 1
            # collapse duplicates as they appear (flow.js versions before the
            # in-place upscale wrote "-upscaled.png" copies alongside)
            for up in img_dir.glob("*-upscaled.png"):
                base = up.with_name(up.name.replace("-upscaled.png", ".png"))
                try:
                    up.replace(base)
                except OSError:
                    pass
            if not st.get("running"):
                break
        if restarted:
            continue
        break
    upscaled = 0
    for up in img_dir.glob("*-upscaled.png"):
        base = up.with_name(up.name.replace("-upscaled.png", ".png"))
        up.replace(base)  # keep the ImgToVideo naming contract
        upscaled += 1
    if upscaled:
        log(f"Adopted {upscaled} upscaled image(s) as the shotlist files")
    new = len({p.name for p in img_dir.iterdir() if p.is_file()} - before)
    if not new:
        raise RuntimeError("Flow Driver finished but produced no new images "
                           "- check the log")
    if new < todo:
        # Everything rendered is already in images\, so nothing is lost. Stop
        # here rather than reporting success: the merge would otherwise run on
        # an incomplete set with no visible reason (this is what a stalled
        # batch - "Flow still busy" timeouts - looks like).
        try:
            shotlist = json.loads((pid_dir / "shotlist.json")
                                  .read_text(encoding="utf-8"))
            expected = [str(i.get("file")) for i in (shotlist.get("images") or [])
                        if i.get("file")]
        except (OSError, ValueError):
            expected = []
        missing = [n for n in expected if not (img_dir / n).exists()] or \
            [f"{todo - new} unnamed card(s)"]
        raise RuntimeError(
            f"{len(missing)} of {len(expected) or todo} image(s) were not "
            f"produced - the batch was stopped or cards failed (Flow reports "
            f"'still busy' timeouts when this happens). The {new} that "
            f"succeeded are kept. Fill the gaps - render them, or upload them "
            f"on the production page (filenames must match) - then Resume. "
            f"Missing: " + ", ".join(missing[:12])
            + (" ..." if len(missing) > 12 else ""))
    return new


def flow_stop(cfg) -> bool:
    """Stop a running Flow Driver batch. Returns True when one was stopped."""
    try:
        result = _driver_api(cfg, "/api/stop", method="POST", body={},
                             timeout=8)
        return bool(result.get("stopped"))
    except Exception:
        return False


def sanitize_shotlist(pid_dir: Path) -> int:
    """Drop shotlist images/shots whose files were never generated
    (e.g. failed on API quota) so the render can proceed with what exists.
    Returns how many shots were dropped."""
    path = pid_dir / "shotlist.json"
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    img_dir = pid_dir / "images"
    existing = {p.stem.lower() for p in img_dir.iterdir()
                if p.is_file()} if img_dir.exists() else set()
    imgs = [i for i in data.get("images", []) if isinstance(i, dict)]
    images = [i for i in imgs
              if Path(i.get("file", "")).stem.lower() in existing]
    dropped = {Path(i.get("file", "")).stem.lower() for i in imgs
               if Path(i.get("file", "")).stem.lower() not in existing}
    shots = [s for s in data.get("shots", []) if isinstance(s, dict)
             and Path(s.get("asset", "")).stem.lower() in existing]
    if not shots:
        raise RuntimeError("sanitizing the shotlist would remove every shot")
    removed = len(data.get("shots", [])) - len(shots)
    if removed == 0 and len(images) == len(imgs):
        return 0
    backup = path.with_suffix(".json.bak")
    if not backup.exists():  # keep the user's full plan recoverable
        shutil.copy(path, backup)
    data["images"] = images
    data["shots"] = shots
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    log.warning("shotlist sanitized: dropped %d shot(s) with missing images %s "
                "(original kept at shotlist.json.bak)",
                removed, sorted(dropped))
    return removed


def run_merge_render(cfg, pid_dir: Path) -> Path:
    """Render the final video with ImgToVideo.Cli (headless). Returns final path."""
    repo = cfg.imgtovideo_repo
    cli = Path(repo, "src", "ImgToVideo.Cli") if repo else None
    if not cli or not cli.exists():
        raise RuntimeError("Set studio.imgtovideo_repo in config.yaml")

    # project layout expectations: audio\narration.<ext>, *.srt at root.
    # Always refresh narration from the current audio artifact - a stale copy
    # here would shadow the real audio (the loader prefers audio\).
    audio_dir = pid_dir / "audio"
    audio_dir.mkdir(exist_ok=True)
    for old in audio_dir.glob("narration.*"):
        old.unlink()
    audio = find_audio(pid_dir)
    if audio:
        shutil.copy(audio, audio_dir / f"narration{audio.suffix}")
    sanitize_shotlist(pid_dir)

    cmd = [
        "dotnet", "run", "--project", str(cli),
        "-c", "Release", "--", "render-final", str(pid_dir),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=14400)
    if result.returncode != 0:
        tail = ((result.stderr or "") + (result.stdout or ""))[-600:]
        raise RuntimeError(f"ImgToVideo.Cli failed (exit {result.returncode}): {tail}")
    final = find_final(pid_dir)
    if not final:
        raise RuntimeError("render finished but no final.mp4 found")
    return final


def prepare_project_folder(cfg, pid: int) -> Path:
    """Make the production folder a valid ImgToVideo project folder."""
    pdir = prod_dir(cfg, pid)
    options_file = pdir / "imgtovideo.json"
    if not options_file.exists():
        options_file.write_text(json.dumps({
            "schema_version": 1,
            "naming": {"image_extensions": [".png", ".jpg", ".jpeg", ".webp"]},
        }, indent=2), encoding="utf-8")
    return pdir


def _resolve_provider(cfg, name: str | None = None) -> dict:
    if cfg.studio_llm_providers:
        name = name or cfg.studio_llm_default
        for p in cfg.studio_llm_providers:
            if p["name"] == name:
                return p
        raise RuntimeError(f"Unknown LLM provider '{name}'")
    # legacy single-LLM config
    if cfg.studio_llm == "openai":
        return {"name": "llm", "base_url": cfg.studio_llm_base_url,
                "api_key": cfg.studio_llm_api_key,
                "model": cfg.studio_llm_model, "api": "openai",
                "env_key": "WR_LLM_API_KEY"}
    raise RuntimeError("No LLM providers configured (studio.llm_providers)")


def _provider_key(p: dict) -> str | None:
    import os

    key = (p.get("api_key") or os.environ.get(p.get("env_key") or "")
           or os.environ.get("WR_LLM_API_KEY") or "").strip()
    return key or None


def provider_ready(cfg, name: str | None = None) -> bool:
    try:
        p = _resolve_provider(cfg, name)
    except RuntimeError:
        return False
    return bool(p["base_url"] and p["model"] and _provider_key(p)
                and provider_api_ready(p))


def openai_chat(p: dict, prompt: str, timeout: int = 600) -> str:
    import http.client

    key = _provider_key(p)
    if not key:
        raise RuntimeError(
            f"No API key for '{p['name']}' (set api_key in config.yaml or "
            f"{p['env_key']} env variable)"
        )
    if not p["base_url"] or not p["model"]:
        raise RuntimeError(f"Provider '{p['name']}' needs base_url and model")
    url = p["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": p["model"],
        "stream": True,  # streaming keeps gateways from timing out long completions
        "temperature": 1.0,  # creative writing; regenerations must differ
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "Accept": "text/event-stream",
        },
        method="POST",
    )

    last_exc = None
    for attempt in range(2):
        try:
            parts = []
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ctype = r.headers.get("Content-Type", "")
                if "event-stream" not in ctype:
                    data = json.loads(r.read())
                    return data["choices"][0]["message"]["content"].strip()
                for raw in r:
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        delta = json.loads(chunk)["choices"][0].get("delta", {})
                    except (ValueError, KeyError, IndexError):
                        continue
                    parts.append(delta.get("content") or "")
            text = "".join(parts).strip()
            if text:
                return text
            raise RuntimeError("LLM returned an empty response")
        except RuntimeError:
            raise
        except (urllib.error.URLError, http.client.HTTPException,
                ConnectionError, TimeoutError, OSError) as exc:
            last_exc = exc
            log.warning("LLM connection error (attempt %d): %s", attempt + 1, exc)
    raise RuntimeError(f"LLM connection failed after retry: {last_exc}")


# Wire protocols an LLM provider can speak, selected per provider with the
# `api` config key. Only OpenAI-compatible /chat/completions is implemented
# today; adding Claude's Messages API or Gemini's generateContent later is a
# new function plus one entry here - no caller changes.
CHAT_APIS = {"openai": openai_chat}


def llm_generate(cfg, prompt: str, timeout: int = 1800,
                 provider: str | None = None) -> str:
    p = _resolve_provider(cfg, provider)
    api = (p.get("api") or "openai").lower()
    fn = CHAT_APIS.get(api)
    if fn is None:
        raise RuntimeError(
            f"LLM provider '{p['name']}' uses api '{api}', which is not "
            f"implemented yet (available: {', '.join(sorted(CHAT_APIS))}). "
            f"Add an adapter to CHAT_APIS in studio.py.")
    return fn(p, prompt, timeout=timeout)


def provider_api_ready(p: dict) -> bool:
    """True when the provider's wire protocol has an adapter (so the UI can
    disable a provider whose api is not implemented yet)."""
    return (p.get("api") or "openai").lower() in CHAT_APIS


def llm_label(cfg, provider: str | None = None) -> str:
    try:
        p = _resolve_provider(cfg, provider)
        return f"{p['name']} ({p['model']})"
    except RuntimeError:
        return "LLM not configured"


# ------------------------------------------------------ "not a copycat" ----

def _ngrams(text: str, n: int = 5) -> set:
    words = re.findall(r"\w+", text.lower())
    return {tuple(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def overlap_ratio(script: str, source: str) -> float:
    """Share of the script's 5-grams that also appear in the source text."""
    a, b = _ngrams(script), _ngrams(source or "")
    if not a:
        return 0.0
    return len(a & b) / len(a)


def overlap_runs(script: str, source: str, n: int = 5,
                 limit: int = 12) -> list[str]:
    """The longest shared 5-gram runs, so a retry can be told exactly which
    passages were lifted instead of just 'be more original'."""
    src = _ngrams(source or "", n)
    if not src:
        return []
    words = re.findall(r"\w+", (script or "").lower())
    runs: list[str] = []
    current: list[str] = []
    for i in range(max(0, len(words) - n + 1)):
        gram = tuple(words[i:i + n])
        if gram in src:
            current.append(words[i + n - 1])
            if len(current) == 1:
                current[:0] = list(gram[:n - 1])
        elif current:
            runs.append(" ".join(current))
            current = []
    if current:
        runs.append(" ".join(current))
    runs.sort(key=len, reverse=True)
    return runs[:limit]


RATING_RUBRIC = [
    ("hook", "Does the first 15 seconds earn attention without clickbait?"),
    ("originality", "Is it a genuine rewrite, not a reworded copy?"),
    ("accuracy", "Are the claims supported by the source and not invented?"),
    ("structure", "Clear beats, logical order, no filler or repetition?"),
    ("pacing", "Does it hold attention to the end at a spoken pace?"),
    ("style_fit", "Does it obey the channel's style guide and tone?"),
    ("ending", "Does it land a payoff rather than trailing off?"),
]


def rating_prompt(title: str, genre: str, script: str, source: str,
                  style_guide: str, overlap: float) -> str:
    rubric = "\n".join(f"- {name}: {desc}" for name, desc in RATING_RUBRIC)
    return (
        f"You are a ruthless YouTube script editor for the channel genre "
        f"'{genre}'. Score this script for the video \"{title}\".\n\n"
        f"Score each criterion 1-10:\n{rubric}\n\n"
        f"Measured 5-gram overlap with the source transcript: {overlap:.1%}. "
        f"Treat high overlap as an originality failure.\n\n"
        f"CHANNEL STYLE GUIDE:\n{(style_guide or '(none)')[:1500]}\n\n"
        f"SCRIPT:\n{(script or '')[:12000]}\n\n"
        f"Reply with ONLY a JSON object:\n"
        f'{{"score": <1-10 overall, one decimal>, '
        f'"criteria": {{"hook": <n>, "originality": <n>, "accuracy": <n>, '
        f'"structure": <n>, "pacing": <n>, "style_fit": <n>, "ending": <n>}}, '
        f'"feedback": ["<specific, actionable fix>", ...], '
        f'"weak_spans": ["<a passage that reads copied or weak>", ...]}}'
    )


def _parse_json_object(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def rate_script(cfg, title: str, genre: str, script: str, source: str,
                style_guide: str, provider: str | None) -> dict:
    """LLM-as-judge. Returns {score, criteria, feedback, weak_spans, error}.
    Never raises: a judge failure must not lose a usable draft."""
    overlap = overlap_ratio(script, source)
    prompt = rating_prompt(title, genre, script, source, style_guide, overlap)
    try:
        reply = _parse_json_object(
            llm_generate(cfg, prompt, provider=provider))
    except Exception as exc:  # noqa: BLE001
        return {"score": None, "criteria": {}, "feedback": [],
                "weak_spans": [], "error": str(exc)[:200]}
    try:
        score = round(float(reply.get("score")), 1)
    except (TypeError, ValueError):
        score = None
    criteria = reply.get("criteria") if isinstance(reply.get("criteria"),
                                                  dict) else {}
    feedback = [str(f) for f in (reply.get("feedback") or []) if str(f).strip()]
    weak = [str(s) for s in (reply.get("weak_spans") or []) if str(s).strip()]
    return {"score": score, "criteria": criteria, "feedback": feedback,
            "weak_spans": weak, "error": None}


def judge_provider(cfg, writer: str | None, preferred: str | None) -> str | None:
    """Which provider rates the script: an explicit choice, else any configured
    provider that is not the one that wrote it (self-scoring is biased)."""
    if preferred:
        return preferred
    names = [p["name"] for p in cfg.studio_llm_providers]
    others = [n for n in names if n != writer and provider_ready(cfg, n)]
    return others[0] if others else writer


def style_prompt(title: str, genre: str, source_text: str,
                 word_count: int | None = None,
                 extra_direction: str = "") -> str:
    text = (source_text or "").strip()
    if len(text) > 12000:
        text = text[:12000] + " ..."
    if not text:
        raise RuntimeError(
            "No source transcript available - write the style guide manually"
        )
    length_note = ""
    if word_count:
        length_note = (f"\nThe transcript is about {word_count} words "
                       f"(~{max(1, round(word_count / 150))} minutes of "
                       f"narration) - reflect this in the Structure section.")
    extra = (extra_direction or "").strip()
    if extra:
        extra = f"\nADDITIONAL DIRECTION FROM THE CREATOR (follow it):\n{extra}\n"
    return f"""You are a writing coach for a {genre} YouTube channel.
Analyze the WRITING STYLE of the transcript below (from a video titled "{title}").
{length_note}
TRANSCRIPT TO ANALYZE:
{text}
{extra}
Produce a STYLE GUIDE in markdown with exactly these sections:
## Voice & Tone
## Pacing & Rhythm
## Sentence Style
## Hook Pattern
## Structure (beats in order, with rough timing)
## CTA Style
## Vocabulary & Register
## Things to Avoid

Rules:
- Describe patterns abstractly (e.g. "short punchy sentences, averages 8-12 words").
- Do NOT quote, copy, or paraphrase any phrase or sentence from the transcript.
- Be concrete enough that another writer could imitate the style without ever seeing the transcript.

Output ONLY the style guide markdown."""


def script_prompt(title: str, genre: str, source_text: str,
                  style_guide: str = "", target_words: int = 1200,
                  variation: str = "", extra_direction: str = "") -> str:
    facts = (source_text or "").strip()
    if len(facts) > 12000:
        facts = facts[:12000] + " ..."
    if facts:
        facts_block = f"FACTS gathered from research (use these, nothing else):\n{facts}"
    else:
        facts_block = "No research transcript available - write from the title alone."
    style = (style_guide or "").strip()
    if style:
        style_block = f"""STYLE GUIDE (match this exactly - tone, pacing, rhythm,
hook pattern, structure, and CTA style all come from it):
{style[:6000]}"""
    else:
        style_block = "No style guide provided."
    var_block = f"\n{variation}" if variation else ""
    extra = (extra_direction or "").strip()
    if extra:
        extra = f"\nADDITIONAL DIRECTION FROM THE CREATOR (follow it):\n{extra}\n"
    return f"""You are an original YouTube scriptwriter for a {genre} channel.

{style_block}

{facts_block}
{extra}
TASK: Write an original YouTube script titled "{title}".
{var_block}
Rules:
- Follow the STYLE GUIDE above precisely. The script must feel like it was
  written by the writer described there.
- Use ONLY the facts above. Never reuse sentences, phrasing, or the structure
  of any source material - only the style is shared.
- Hook the viewer in the first 15 seconds, following the style guide's hook pattern.
- About {target_words} words. Conversational, second person, no stage directions, no scene labels.
- End with a short call to action matching the style guide's CTA style.

Output ONLY the script text."""


def shotlist_prompts(pid_dir: Path) -> list[str]:
    """Extract the image prompts (in shot order) from shotlist.json."""
    path = pid_dir / "shotlist.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    by_file = {i.get("file"): (i.get("prompt") or "").strip()
               for i in data.get("images", [])
               if isinstance(i, dict) and i.get("file")}
    return [by_file[s["asset"]] for s in data.get("shots", [])
            if isinstance(s, dict) and s.get("asset") in by_file
            and by_file[s["asset"]]]


def load_manifest_brief(cfg) -> str:
    """The ImgToVideo manifest-authoring brief: the master planning prompt
    that turns a narration SRT into shotlist.json + an image batch sheet.

    Loaded from disk on every use so edits to the brief take effect
    immediately. Path: studio.manifest_brief in config.yaml, or
    <imgtovideo_repo>\\docs\\manifest-authoring-brief.md by default."""
    path = None
    if cfg.studio_manifest_brief:
        p = Path(cfg.studio_manifest_brief)
        path = p if p.is_absolute() else cfg.base_dir / p
    elif cfg.imgtovideo_repo:
        path = (Path(cfg.imgtovideo_repo) / "docs"
                / "manifest-authoring-brief.md")
    if not path or not path.exists():
        raise RuntimeError(
            "manifest-authoring-brief.md not found - set studio.imgtovideo_repo "
            "or studio.manifest_brief in config.yaml")
    text = path.read_text(encoding="utf-8")
    if "\n---\n" in text:  # skip the how-to header, keep the prompt itself
        text = text.split("\n---\n", 1)[1]
    return text.strip()


def _extract_json_object(text: str) -> tuple[dict, str]:
    """Extract the first balanced JSON object (string-aware) plus the tail
    after it - tolerant of extra documents following the JSON."""
    start = text.find("{")
    if start == -1:
        raise RuntimeError("LLM returned no JSON object")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(text[start:i + 1])
                except ValueError as exc:
                    raise RuntimeError(f"shotlist JSON is invalid: {exc}")
                return data, text[i + 1:]
    raise RuntimeError("LLM returned an incomplete JSON object")


def parse_shotlist_output(text: str) -> tuple[dict, str]:
    """Parse the LLM's two-document output (manifest-authoring brief):
    shotlist.json first, optional IMAGE BATCH SHEET second.
    Returns (shotlist_data, batch_sheet_text)."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    data, tail = _extract_json_object(text)
    if not isinstance(data.get("shots"), list) or not data["shots"]:
        raise RuntimeError("shotlist has no shots[] entries")
    if not isinstance(data.get("images"), list) or not data["images"]:
        raise RuntimeError("shotlist has no images[] entries")
    for item in data["images"]:
        if not isinstance(item, dict) or not item.get("file") or not item.get("prompt"):
            raise RuntimeError("images[] entries need 'file' and 'prompt'")
    return data, tail.strip()


def shotlist_prompt(brief_text: str, srt_text: str, style_guide: str = "",
                    extra_direction: str = "", bible: str = "",
                    feedback: str = "") -> str:
    """Assemble the manifest-authoring brief with its inputs: the full
    narration SRT, the channel visual style, the optional character /
    reference bible, and the creator's per-stage direction."""
    style = (style_guide or "").strip()
    style_block = (
        f"INPUT 2 - CHANNEL VISUAL STYLE INSTRUCTIONS:\n{style}"
        if style else
        "INPUT 2 - CHANNEL VISUAL STYLE INSTRUCTIONS:\n"
        "(none supplied - write a concise master visual style yourself)")
    bible_block = ""
    if (bible or "").strip():
        bible_block = (f"\n\nINPUT 3 - CHARACTER / REFERENCE BIBLE "
                       f"(preserve these characters, environments and "
                       f"objects):\n{bible.strip()}")
    extra = (extra_direction or "").strip()
    if extra:
        extra = f"\n\nINPUT 4 - CREATOR DIRECTION (follow it):\n{extra}"
    fix_block = ""
    if (feedback or "").strip():
        fix_block = (f"\n\nINPUT 5 - FIXES REQUIRED IN THIS REVISION "
                     f"(the previous shotlist was rejected - address every "
                     f"point):\n{feedback.strip()}")
    return f"""{brief_text.strip()}

---

INPUT 1 - THE FULL NARRATION SRT:
{srt_text.strip()}

{style_block}{bible_block}{extra}{fix_block}"""


# ------------------------------------------- shotlist review (shots gate) ---
# The shotlist declares which SRT cues each shot illustrates (`cues: "3-9"`),
# so alignment is verifiable BEFORE any image is rendered. Structural faults
# are objective (coverage, order, ranges, orphan assets) and are checked for
# free; whether a prompt actually depicts its narration needs the LLM.

_CUE_RANGE_RE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$")
_SRT_BLOCK_RE = re.compile(
    r"(\d+)\s*\n\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*"
    r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*\n(.*?)(?=\n\s*\n|\Z)", re.S)


def parse_srt_cues(srt_text: str) -> list[dict]:
    """[{index, start, end, text}] for every cue in an SRT."""
    cues = []
    for m in _SRT_BLOCK_RE.finditer(srt_text or ""):
        cues.append({"index": int(m.group(1)), "start": m.group(2),
                     "end": m.group(3),
                     "text": " ".join(m.group(4).split())})
    return cues


def cue_range(text: str) -> tuple[int, int] | None:
    m = _CUE_RANGE_RE.match(str(text or ""))
    if not m:
        return None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    return (start, end) if end >= start else (start, start)


# Reference names ARE the identity: FlowImagesGen attaches project assets by
# name, Renderly resolves them as filenames, and Flow's own card matching is
# fuzzy. So the shape is enforced, not hoped for.
REF_NAME_RE = re.compile(r"^(CH|BG|OBJ)_[A-Z0-9]+(?:_[0-9]{2})?$")


def ref_name(value) -> str:
    """The bare reference name for a registry key or an image's ref entry.

    Accepts a name ('CH_MAYA') or a path ('refs/CH_MAYA.png') and returns the
    stem, so the legacy name -> path registry keeps working."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "/" in text or "\\" in text or "." in text:
        return Path(text).stem
    return text


def declared_refs(data: dict) -> dict:
    """{name: entry} for the shotlist's refs registry, whatever its shape."""
    refs = data.get("refs")
    out: dict = {}
    if isinstance(refs, dict):
        for key, entry in refs.items():
            name = ref_name(key)
            if name:
                out[name] = entry
    elif isinstance(refs, list):
        for entry in refs:
            if isinstance(entry, str):
                name = ref_name(entry)
            elif isinstance(entry, dict):
                name = ref_name(entry.get("name"))
            else:
                continue
            if name:
                out[name] = entry
    return out


def shotlist_structural_faults(data: dict, cue_count: int,
                               limit: int = 12) -> list[str]:
    """Objective faults in a shotlist - no LLM, so these must always be zero:
    cue coverage, ordering, ranges, orphan assets, duplicate prompts, and the
    reference registry (naming convention + every used name declared)."""
    faults: list[str] = []
    shots = [s for s in (data.get("shots") or []) if isinstance(s, dict)]
    images = [i for i in (data.get("images") or []) if isinstance(i, dict)]
    if not shots:
        return ["no shots in the shotlist"]
    if not images:
        faults.append("no images in the shotlist")

    ranges: list[tuple[int, int]] = []
    for s in shots:
        rng = cue_range(s.get("cues"))
        if rng is None:
            faults.append(f"shot {s.get('asset') or '?'} has no usable "
                          f"'cues' range ({s.get('cues')!r})")
            continue
        ranges.append(rng)

    if ranges:
        covered: set[int] = set()
        for start, end in ranges:
            covered.update(range(start, end + 1))
        missing = sorted(set(range(1, cue_count + 1)) - covered)
        if missing:
            shown = ", ".join(str(c) for c in missing[:limit])
            faults.append(f"{len(missing)} narration cue(s) have no shot: "
                          f"{shown}{' …' if len(missing) > limit else ''}")
        beyond = sorted(c for c in covered if c > cue_count)
        if beyond:
            faults.append(f"{len(beyond)} cue number(s) exceed the SRT "
                          f"({cue_count} cues): {beyond[:5]}")
        for i in range(1, len(ranges)):
            if ranges[i][0] < ranges[i - 1][0]:
                faults.append(f"shots are out of narration order at shot "
                              f"{i + 1} (cue {ranges[i][0]} after "
                              f"{ranges[i - 1][0]})")
                break
        for i in range(1, len(ranges)):
            if ranges[i][0] <= ranges[i - 1][1]:
                faults.append(f"overlapping cue ranges at shot {i + 1} "
                              f"({ranges[i - 1][0]}-{ranges[i - 1][1]} then "
                              f"{ranges[i][0]}-{ranges[i][1]})")
                break

    names = {str(i.get("file")) for i in images if i.get("file")}
    orphans = sorted({str(s.get("asset")) for s in shots if s.get("asset")}
                     - names)
    if orphans:
        faults.append(f"{len(orphans)} shot(s) reference an asset that is not "
                      f"in images: {', '.join(orphans[:5])}")

    seen: dict[str, str] = {}
    dupes = []
    for i in images:
        prompt = " ".join(str(i.get("prompt") or "").lower().split())
        if not prompt:
            continue
        if prompt in seen:
            dupes.append(f"{i.get('file')} duplicates {seen[prompt]}")
        else:
            seen[prompt] = str(i.get("file"))
    if dupes:
        faults.append(f"{len(dupes)} image prompt(s) are exact duplicates: "
                      f"{'; '.join(dupes[:4])}")

    # Reference registry: the names are the contract with Flow, so a bad or
    # undeclared name must fail here rather than become "reference not found,
    # skipping" on every rendered image.
    declared = declared_refs(data)
    bad = sorted(n for n in declared if not REF_NAME_RE.match(n))
    if bad:
        faults.append(f"{len(bad)} reference name(s) break the CH_/BG_/OBJ_ "
                      f"convention: {', '.join(bad[:6])}")
    used: set[str] = set()
    for i in images:
        for r in (i.get("refs") or []):
            name = ref_name(r)
            if name:
                used.add(name)
    undeclared = sorted(used - set(declared))
    if undeclared:
        faults.append(f"{len(undeclared)} reference name(s) are used by an "
                      f"image but not declared in the shotlist's refs: "
                      f"{', '.join(undeclared[:6])}")
    return faults[:limit]


def alignment_prompt(chunk: list[dict], cues: dict[int, str]) -> str:
    """Audit prompt COMPLETENESS per scene, not just topical match.

    Rendering an image is slow and costs credits, so a prompt that only
    partially describes its beat means a hand regeneration later. The judge is
    asked what the narration at those cues REQUIRES and whether the prompt
    states each of those elements explicitly."""
    items = []
    for s in chunk:
        rng = cue_range(s.get("cues")) or (0, 0)
        narration = " ".join(cues.get(c, "") for c in range(rng[0], rng[1] + 1))
        items.append(f'- asset: {s.get("asset")}\n'
                     f'  cues {rng[0]}-{rng[1]} narrate: "{narration[:700]}"\n'
                     f'  image prompt: "{str(s.get("prompt"))[:700]}"')
    return (
        "You are auditing a video shotlist BEFORE its images are rendered. For "
        "each shot you get the narration at its cue range and the image prompt "
        "that will be sent to the image model. Rendering is slow and costs "
        "money, so a prompt that under-specifies its scene has to be "
        "regenerated by hand later.\n\n"
        "First work out what the narration at those cues REQUIRES the image to "
        "show: the subject(s) present, what they are doing, where they are, the "
        "objects or props involved, and the specific information the cue is "
        "conveying. Then judge whether the PROMPT states each of those "
        "elements explicitly and concretely.\n\n"
        "Verdicts:\n"
        '  "ok"     - every required element is stated explicitly; the image '
        "model has everything it needs.\n"
        '  "weak"   - the right subject is there but it is vague, generic or '
        "missing required detail (e.g. no setting, no action, no props).\n"
        '  "missing" - it does not describe this beat at all, or describes a '
        "different one.\n"
        "Judge completeness only: stylistic wording is irrelevant, and do not "
        "require anything the narration does not ask for.\n\n"
        + "\n".join(items) +
        "\n\nReply with ONLY a JSON object:\n"
        '{"shots": [{"asset": "<asset>", "verdict": "ok"|"weak"|"missing", '
        '"missing": ["<element the narration requires that the prompt does not '
        'state>", ...], "reason": "<one short line>"}]}\n'
        "Include every shot, and leave \"missing\" empty for ok shots."
    )


def review_shotlist(cfg, data: dict, cues: list[dict], provider: str | None,
                    chunk_size: int = 20) -> dict:
    """Structural faults + a per-scene prompt-completeness audit. Returns
    {faults, matched, total, weak, ratio, error}. Never raises.

    `matched` counts shots whose prompt carries everything its cues require;
    `weak` lists the rest with the elements they fail to specify."""
    faults = shotlist_structural_faults(data, len(cues))
    cue_text = {c["index"]: c["text"] for c in cues}
    prompt_by_file = {str(i.get("file")): i.get("prompt")
                      for i in (data.get("images") or [])
                      if isinstance(i, dict)}
    shots = []
    for s in (data.get("shots") or []):
        if isinstance(s, dict) and s.get("asset"):
            shots.append({**s, "prompt": prompt_by_file.get(str(s["asset"]), "")})
    total = len(shots)
    if not shots:
        return {"faults": faults, "matched": 0, "total": 0, "weak": [],
                "ratio": 0.0, "error": None}
    weak: list[dict] = []
    matched = 0
    error = None
    for i in range(0, len(shots), chunk_size):
        chunk = shots[i:i + chunk_size]
        try:
            reply = _parse_json_object(llm_generate(
                cfg, alignment_prompt(chunk, cue_text), provider=provider))
        except Exception as exc:  # noqa: BLE001 - a judge failure must not
            error = str(exc)[:200]          # lose an otherwise usable shotlist
            continue
        verdicts = reply.get("shots") if isinstance(reply.get("shots"),
                                                    list) else []
        by_asset = {str(v.get("asset")): v for v in verdicts
                    if isinstance(v, dict)}
        for s in chunk:
            asset = str(s.get("asset"))
            v = by_asset.get(asset)
            if v is None:
                # the judge skipped it: do not fail a shot on silence
                matched += 1
                continue
            if str(v.get("verdict") or "").lower() == "ok":
                matched += 1
                continue
            missing = [str(m) for m in (v.get("missing") or []) if str(m).strip()]
            weak.append({"asset": asset,
                         "verdict": str(v.get("verdict") or "weak"),
                         "missing": missing[:8],
                         "reason": str(v.get("reason") or "")[:200],
                         "narration": (cue_text.get((cue_range(s.get("cues"))
                                                     or (0, 0))[0], ""))[:200]})
    ratio = (matched / total) if total else 0.0
    return {"faults": faults, "matched": matched, "total": total,
            "weak": weak[:30], "ratio": ratio, "error": error}


# --------------------------------------------------- external tool hooks ---

def run_hook(command: str, subs: dict, timeout: int = 3600) -> None:
    """Run a configured external command, substituting {placeholders}.
    Every value is command-line quoted, so working folders or filenames
    containing spaces, & , ^ or % cannot inject extra commands."""
    cmd = command
    for key, val in subs.items():
        cmd = cmd.replace("{" + key + "}",
                          subprocess.list2cmdline([str(val)]))
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                            timeout=timeout)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "")[-400:]
        raise RuntimeError(f"command failed (exit {result.returncode}): {tail}")
