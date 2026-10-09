"""Studio: human-in-the-loop YouTube production pipeline logic.

Every stage can be completed by an automated tool (if its hook is configured)
or by hand (paste text / upload files) - the human stays in charge.
"""

import contextvars
import functools
import json
import logging
import os
import queue
import random
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import briefs

log = logging.getLogger("whisperradar")

# random directives so regenerating a script produces a genuinely fresh take
VARIATION_ANGLES = [
    "Take a slightly different narrative angle than any previous version.",
    "Open with a different hook pattern than a question.",
    "Lead with the most surprising fact and restructure the beats around it.",
    "Use a more story-driven approach: dramatize ONE specific fact already in "
    "the FACTS above as a narrative moment. Do not invent a named person, a "
    "quote, or an event that is not in the FACTS - if none of the facts "
    "supports a personal story, use this angle on the structure/pacing "
    "instead, not on inventing a character.",
    "Emphasize the practical steps more than the theory.",
    "Frame the topic as a mistake people make and reverse-engineer the fix.",
]


def variation_nudge(attempt: int = 1, overlap: float | None = None,
                    runs: list[str] | None = None,
                    feedback: list[str] | None = None,
                    version: int = 0) -> str:
    """The variation instruction. On a retry it carries the previous
    attempt's measured overlap, the passages that were lifted, and the
    judge's notes - a bare 'be more original' changes nothing.

    `version` is how many scripts already exist for this production. The angle
    rotates by it instead of `random.choice`, which could pick the same angle
    twice in a row and hand back the same script."""
    angle = VARIATION_ANGLES[(version + attempt - 1) % len(VARIATION_ANGLES)]
    parts = [f"[VARIATION {random.randint(1000, 9999)}] {angle}",
             "Produce a fresh take: different wording, sentence order and "
             "rhythm from any earlier attempt."]
    if version > 0:
        parts.append(
            "A previous version of this script already exists. Write a "
            "substantially different version: a different hook, a different "
            "opening 15 seconds, a different order of beats, and different "
            "examples - do not reuse the earlier version's sentences.")
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

# How much of a source transcript reaches the LLM. It used to be 12k, which
# silently cut a 23k transcript in half: the script writer is told to use ONLY
# the facts it is given, so the drop surfaced as accuracy failures in the rating
# gate. 60k chars (~15k tokens) fits every configured model.
SOURCE_FACTS_MAX_CHARS = 60000


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

_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_folder_name(title: str, limit: int = 80) -> str:
    """A title as a Windows-safe folder name."""
    name = _BAD_NAME_CHARS.sub(" ", title or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name[:limit].strip(" .") or "production")


def auto_work_dir(cfg, conn, title: str) -> str | None:
    """<productions_root>/<title> for a new production, or None when no
    location is set. A name already taken by something that is not a
    WhisperRadar production folder gets " (2)", " (3)", ... appended."""
    from . import settings

    root = str(settings.load(conn).get("productions_root") or "").strip()
    if not root:
        return None
    base = Path(root).expanduser()
    name = safe_folder_name(title)
    for n in range(1, 200):
        cand = base / (name if n == 1 else f"{name} ({n})")
        if not cand.exists() or (not any(cand.iterdir())
                                 and not _is_claimed(conn, cand)):
            return str(validate_work_dir(cfg, cand))
    raise RuntimeError(f"No free folder name for '{name}' in {base}")


def _is_claimed(conn, path: Path) -> bool:
    row = conn.execute("SELECT 1 FROM productions WHERE work_dir = ?",
                       (str(path.resolve()),)).fetchone()
    return bool(row)


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


def pick_folder(initial_dir: str | None = None) -> str | None:
    """Open the OS folder chooser and return the chosen absolute path.

    The browser cannot hand a page a real filesystem path, but WhisperRadar's
    server runs on the same machine, so the SERVER opens the native dialog.
    Blocks until the user picks or cancels - fine for a local, single-user
    tool. Returns None when cancelled. Raises RuntimeError when there is no GUI
    session / Tk (e.g. a headless service), so the caller can say so."""
    try:
        import tkinter
        from tkinter import filedialog
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "no folder picker on the server (tkinter is missing) - type the "
            "path instead") from exc
    try:
        root = tkinter.Tk()
    except Exception as exc:  # noqa: BLE001 - headless / no display
        raise RuntimeError(
            "the server has no desktop session to show a folder picker - "
            "type the path instead") from exc
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        start = None
        if initial_dir:
            p = Path(initial_dir).expanduser()
            if p.is_dir():
                start = str(p)
        chosen = filedialog.askdirectory(
            parent=root, title="Choose a working folder",
            initialdir=start, mustexist=False)
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
    chosen = str(chosen or "").strip()
    return chosen or None


MOVE_ITEMS = ["script.md", "style.md", "writing_style.md", "bible.md", "source_transcript.txt",
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
    """The production's ART style (style.md): image/shot direction only. It is
    seeded from the channel's style text and is NOT the script's writing guide -
    see find_writing_style."""
    p = pid_dir / "style.md"
    return p if p.exists() else None


def find_writing_style(pid_dir: Path) -> Path | None:
    """The WRITING style guide of the source video (writing_style.md), made by
    stage 1 (style) and read only by the script stage and its judge. Kept apart
    from style.md so the channel's art direction can never stand in for it."""
    p = pid_dir / "writing_style.md"
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
    `refs_dir` (copied into refs\\). This is what keeps an unattended run
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

    bible_dir: Path | None = None
    refs_dir: Path | None = None
    if eff["bible_dir"] or eff["refs_dir"] or eff["bible"] or eff["style"]:
        source = eff["own_channel_name"] or "own channel"
        if eff["bible_dir"]:
            bible_dir = Path(eff["bible_dir"]).expanduser()
        if eff["refs_dir"]:
            refs_dir = Path(eff["refs_dir"]).expanduser()
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


# The merge stage builds a fast preview draft, then exports an NLE project
# for the configured target. RENDER_TARGETS is the closed set; the label is
# shown in the dashboard and the settings page.
RENDER_TARGETS = ("premiere", "capcut")
RENDER_TARGET_LABELS = {"premiere": "Premiere Pro", "capcut": "Final Cut (CapCut)"}


def find_preview(pid_dir: Path) -> Path | None:
    """The merge stage's fast draft (out\\preview.mp4)."""
    p = pid_dir / "out" / "preview.mp4"
    return p if p.exists() else None


def nle_project(pid_dir: Path, target: str) -> Path | None:
    """The exported NLE project for `target`, or None when it is absent.
    premiere = an FCP7 XML file; capcut = a draft folder."""
    if target == "capcut":
        p = pid_dir / "out" / "capcut" / pid_dir.name
    else:
        p = pid_dir / "out" / "premiere.xml"
    return p if p.exists() else None


def find_nle_projects(pid_dir: Path) -> list[tuple[str, Path]]:
    """[(target, path)] for every NLE export present in the production."""
    out = []
    for target in RENDER_TARGETS:
        p = nle_project(pid_dir, target)
        if p is not None:
            out.append((target, p))
    return out


def find_review_video(pid_dir: Path) -> Path | None:
    """The playable artifact for review: a rendered/uploaded final video,
    else the merge stage's preview draft."""
    return find_final(pid_dir) or find_preview(pid_dir)


def merge_done(pid_dir: Path) -> bool:
    """True once the merge stage has left anything reviewable behind."""
    return bool(find_review_video(pid_dir) or find_nle_projects(pid_dir))


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

    Renderly's resolve_tier understands the legacy 2x/4x scale (1 -> HD,
    2 -> 2K, 3 -> 2K, 4 -> 4K) and rejects 0, so tier 0 maps to 0 and the
    callers omit the upscale entirely (native 1K, below ImgToVideo's canvas
    spec).
    """
    try:
        tier = max(0, min(4, int(value or 0)))
    except (TypeError, ValueError):
        return 0
    if tier == 0:
        return 0
    return 2 if tier <= 2 else 4


class RenderlyQuotaExhausted(RuntimeError):
    """ImgToVideo.ImageGen (--renderly) stopped early (exit 3) because
    Renderly/Gemini reported a quota or billing limit - not just one image
    failing, the whole rest of the batch would fail the same way. `generated`
    is how many images it produced before stopping, so a caller can add the
    fallback generator's count to it rather than losing track."""

    def __init__(self, generated: int, tail: str):
        super().__init__(
            f"Renderly quota/billing limit reached after {generated} "
            f"image(s) this run: {tail}")
        self.generated = generated


# ------------------------------------------------------ kill switch --
# ONE mechanism for every image engine: a process-tree kill of whatever WE
# spawned for this production (FlowBatch's node CLI - its tree owns Chrome -
# and the ImageGen `dotnet run` for the Renderly API). Nothing engine-specific
# lives in the kill path; engines differ only in what they register. A kill
# is final: it does NOT retry (autorun sets the job's cancel flag first, so
# the resume loop exits instead of pausing and trying again).

_BATCH_SCOPE: contextvars.ContextVar = contextvars.ContextVar(
    "wr_batch_scope", default=None)
_BATCH_PROCS: dict = {}          # scope key -> {pid: (Popen, label)}
_BATCH_KILLED: set = set()       # pids a user kill ended (read by the runners)
_BATCH_LOCK = threading.Lock()


def _scope_key(pid_dir) -> str:
    return os.path.normcase(str(Path(pid_dir).resolve()))


class batch_scope:
    """Context manager: processes spawned inside it belong to this
    production's folder, so `kill_image_batch(pdir)` can find them."""

    def __init__(self, pid_dir):
        self._key = _scope_key(pid_dir)
        self._token = None

    def __enter__(self):
        self._token = _BATCH_SCOPE.set(self._key)
        return self

    def __exit__(self, *exc):
        _BATCH_SCOPE.reset(self._token)
        return False


def batch_scoped(fn):
    """Decorator for the stage runners (cfg, pid, ...): everything they spawn
    belongs to production `pid`."""
    @functools.wraps(fn)
    def wrapper(cfg, pid, *args, **kwargs):
        with batch_scope(prod_dir(cfg, pid)):
            return fn(cfg, pid, *args, **kwargs)
    return wrapper


def _popen_tracked(cmd, label: str, **kwargs) -> subprocess.Popen:
    """subprocess.Popen that is registered for the kill switch. On POSIX the
    child gets its own session so the whole group can be signalled."""
    if os.name != "nt":
        kwargs.setdefault("start_new_session", True)
    proc = subprocess.Popen(cmd, **kwargs)
    key = _BATCH_SCOPE.get()
    with _BATCH_LOCK:
        _BATCH_PROCS.setdefault(key, {})[proc.pid] = (proc, label)
    return proc


def _release_tracked(proc: subprocess.Popen) -> bool:
    """Forget a finished process; True when a user kill ended it."""
    with _BATCH_LOCK:
        for procs in _BATCH_PROCS.values():
            procs.pop(proc.pid, None)
        killed = proc.pid in _BATCH_KILLED
        _BATCH_KILLED.discard(proc.pid)
    return killed


def _kill_process_tree(proc: subprocess.Popen) -> bool:
    """The single tree kill: `taskkill /T /F` on Windows, SIGTERM to the
    process group on POSIX. False when it had already exited."""
    if proc.poll() is not None:
        return False
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            proc.terminate()
    return True


def kill_image_batch(pid_dir) -> dict:
    """Tree-kill every image process this production has running. Returns
    {"killed": [labels], "nothing_running": bool} so the caller can say
    honestly what happened. Never retries anything."""
    key = _scope_key(pid_dir)
    with _BATCH_LOCK:
        live = list(_BATCH_PROCS.get(key, {}).values())
    killed = []
    for proc, label in live:
        with _BATCH_LOCK:
            _BATCH_KILLED.add(proc.pid)
        if _kill_process_tree(proc):
            killed.append(label)
        else:
            with _BATCH_LOCK:
                _BATCH_KILLED.discard(proc.pid)
    return {"killed": killed, "nothing_running": not killed}


def _run_tracked(cmd, label: str, timeout: float | None = None, **kwargs):
    """subprocess.run(capture_output, text) equivalent that the kill switch
    can reach. Raises BatchCancelled when a user kill ended it."""
    proc = _popen_tracked(cmd, label, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, **kwargs)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        proc.communicate()
        raise
    finally:
        killed = _release_tracked(proc)
    if killed:
        raise BatchCancelled(f"{label} was killed by the user")
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def run_imagegen(cfg, pid_dir: Path, channel=None, upscale=None,
                  motion_filter=None) -> int:
    """Render the production's shotlist images through ImgToVideo.ImageGen
    in Renderly mode. `channel` is the target Renderly channel id (the
    production's own channel mirror); None falls back to the legacy
    'whisperradar' channel. Returns how many new images landed in images\\.

    `motion_filter`, if given (an iterable of motion codes like ("PL", "PR")),
    passes --motion-filter through to ImageGen so only shots with a matching
    filename suffix go through the paid API this call - everything else in
    the shotlist is left on the table for a separate free-generator call
    (see autorun._run_images, which is the only caller that sets this).

    Raises RenderlyQuotaExhausted (rather than plain RuntimeError) when the
    tool stopped early on a quota/billing wall (exit 3) - see that class."""
    repo = cfg.imgtovideo_repo
    if not repo or not Path(repo, "src", "ImgToVideo.ImageGen").exists():
        raise RuntimeError("Set studio.imgtovideo_repo in config.yaml")
    # Refresh out\image-batch.json first: ImgToVideo.ImageGen reads its
    # per-shot "aspect" field (21:9 for PL/PR - see export-batch) to request
    # the right ratio per file instead of one flat 16:9 for the whole batch.
    # Best-effort: a failure here (e.g. no shotlist yet) surfaces the same
    # way it always did, from the ImageGen call right below, so it is not
    # worth a special error path of its own.
    cli = _imgtovideo_cli(cfg)
    if cli:
        _run_imgtovideo_cli(cli, ["export-batch", str(pid_dir)])
    if channel is None:
        channel = ensure_renderly_channel(cfg)
    if upscale is None:
        upscale = cfg.renderly_upscale
    before = {p.name for p in (pid_dir / "images").iterdir()} \
        if (pid_dir / "images").exists() else set()
    # Renderly's upscale API rejects 0 and ImageGen's --upscale only accepts
    # 2 or 4, so tier 0 (off: keep the native 1K render) omits the flag.
    scale = renderly_upscale(upscale if upscale is not None
                             else cfg.renderly_upscale)
    cmd = [
        "dotnet", "run", "--project",
        str(Path(repo, "src", "ImgToVideo.ImageGen")),
        "-c", "Release", "--",
        str(pid_dir),
        "--renderly", cfg.renderly_url,
        "--channel", str(channel),
        "--image-size", "1K",
    ]
    if scale:
        cmd += ["--upscale", str(scale)]
    if motion_filter:
        cmd += ["--motion-filter", ",".join(motion_filter)]
    result = _run_tracked(cmd, "Renderly API (ImageGen)", timeout=7200)
    img_dir = pid_dir / "images"
    new = [p.name for p in img_dir.iterdir() if p.name not in before] \
        if img_dir.exists() else []
    if result.returncode == 3:
        tail = (result.stderr or result.stdout or "")[-500:]
        raise RenderlyQuotaExhausted(len(new), tail)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "")[-500:]
        raise RuntimeError(f"ImageGen failed (exit {result.returncode}): {tail}")
    return len(new)


def effective_engine(engine: str | None, mode: str | None) -> str:
    """The image engine a production really uses. Render mode "flow" means
    every shot on Google Flow through FlowBatch, whichever engine was picked; the Renderly engine otherwise renders
    PL/PR through its API and everything else through FlowBatch."""
    if engine == "flowbatch" or mode == "flow":
        return "flowbatch"
    return "renderly"


def shotlist_missing_images(pid_dir: Path) -> list[str]:
    """Shotlist image file names that are still absent from images\\ (exact
    file-name comparison)."""
    shotlist_path = pid_dir / "shotlist.json"
    if not shotlist_path.exists():
        raise RuntimeError("Generate the shotlist first")
    data = json.loads(shotlist_path.read_text(encoding="utf-8"))
    img_dir = pid_dir / "images"
    img_dir.mkdir(exist_ok=True)
    existing = {p.name for p in img_dir.iterdir() if p.is_file()}
    return [i["file"] for i in data.get("images", [])
            if isinstance(i, dict) and i.get("file") and i.get("prompt")
            and i["file"] not in existing]


# ------------------------------------------- FlowBatch (2nd engine) --
# The standalone Playwright Flow CLI, consumed in place from its own
# checkout (like ImgToVideo and Renderly - never vendored: its Google
# session lives in a gitignored profile\ folder). Invoked as:
#   node src/cli.js generate --job <job.json> --output <dir> ...

# WhisperRadar's upscale 0-4 -> FlowBatch's resolution tier names.
# 3k does not exist there (off/1k/2k/4k) and normalizeTier throws on unknown
# tiers, so tier 3 collapses to 2k - the same legacy mapping Renderly's
# resolve_tier applies.
FLOWBATCH_TIERS = {0: "off", 1: "1k", 2: "2k", 3: "2k", 4: "4k"}

# Flow refuses prompts over roughly 2450 characters with the SAME message it
# uses for rate limiting, so the job-wide style is only sent when it fits.
FLOWBATCH_MAX_PROMPT_CHARS = 2420

# Per-shot aspect-ratio override for FlowBatch jobs (src/jobs/load.js already
# reads item.aspectRatio, this just needed to be sent). Keyed by the motion
# code suffix on the shot's file name (matches ImgToVideo.Cli export-batch's
# own canvas/aspect table). Only motion codes Flow's own UI actually offers a
# toggle for belong here - Flow's aspectRatioGroup is 16:9/4:3/1:1/3:4/9:16
# with no 21:9, so PL/PR/PV pans stay on the job-wide "16:9" default and stay
# push-ins after MotionEngine's overscan fallback; only PU/PD (1:1) benefit.
FLOWBATCH_ASPECT_BY_MOTION = {"PU": "1:1", "PD": "1:1", "PV": "16:9"}


def flowbatch_dir(cfg) -> Path | None:
    if not cfg.flowbatch_repo:
        return None
    d = Path(cfg.flowbatch_repo).expanduser()
    return d if d.exists() else None


def flowbatch_ready(cfg) -> bool:
    d = flowbatch_dir(cfg)
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
    url = (cfg.flowbatch_project_url or "").strip() or None
    return url, "config" if url else "none"


REFS_GENERATED_MANIFEST = "refs_generated.json"


def find_supplied_refs(pdir: Path) -> list[str]:
    """Image files the USER put in the production's refs\\ folder (uploaded on
    the studio page or seeded from the channel) - files written by the refs
    generator are excluded via its manifest. Filenames only, e.g.
    'MAYA.png': the planning prompt lists them so the planner can attach the
    exact registry paths instead of guessing at what might be on disk."""
    refs_dir = pdir / "refs"
    if not refs_dir.is_dir():
        return []
    try:
        generated = set(json.loads(
            (pdir / REFS_GENERATED_MANIFEST).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        generated = set()
    return sorted(
        f.name for f in refs_dir.iterdir()
        if f.is_file()
        and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
        and f.stem not in generated)


def shotlist_uses_refs(data: dict) -> bool:
    """True when the plan declares a refs registry / refPrompts or any image
    attaches a reference. Used to enforce a channel's references-off rule: a
    plan that uses refs when they are disabled is rejected and re-planned."""
    if isinstance(data.get("refs"), dict) and data["refs"]:
        return True
    if isinstance(data.get("refPrompts"), dict) and data["refPrompts"]:
        return True
    for img in (data.get("images") or []):
        if isinstance(img, dict) and img.get("refs"):
            return True
    return False


def shotlist_refs(pdir: Path) -> dict:
    """The refs the shotlist USES, as {name: {"path": str|None,
    "prompt": str|None, "file": Path|None, "provided": bool}}.

    Only refs an image actually attaches are returned - a bible entry no shot
    uses does not need to exist, and generating it would waste a generation.
    A ref is 'provided' when its registry path resolves to a file (relative
    paths are resolved against the production folder)."""
    shotlist_path = pdir / "shotlist.json"
    if not shotlist_path.exists():
        return {}
    try:
        data = json.loads(shotlist_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    registry = data.get("refs")
    prompts = data.get("refPrompts")
    prompts = prompts if isinstance(prompts, dict) else {}
    used: list[str] = []
    for image in (data.get("images") or []):
        if not isinstance(image, dict):
            continue
        for entry in (image.get("refs") or []):
            name = ref_name(entry)
            if name and name not in used:
                used.append(name)
    out: dict = {}
    for name in used:
        raw = registry.get(name) if isinstance(registry, dict) else None
        raw = raw if isinstance(raw, str) else None
        path = None
        if raw:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = pdir / candidate
            path = candidate if candidate.is_file() else None
        prompt = prompts.get(name)
        # an image sitting in refs\ under this name, whether or not the
        # registry points at it (a seeded or uploaded file the plan declared
        # as null): shown so it can be removed from the refs stage
        disk = None
        refs_dir = pdir / "refs"
        if refs_dir.is_dir():
            disk = next((f for f in sorted(refs_dir.iterdir())
                         if f.is_file() and f.stem.lower() == name.lower()
                         and f.suffix.lower() in (".png", ".jpg", ".jpeg",
                                                  ".webp")), None)
        out[name] = {"path": raw, "prompt": (str(prompt).strip()
                                             if prompt else None),
                     "file": path, "provided": path is not None,
                     "disk": disk}
    return out


def refs_to_generate(pdir: Path) -> dict:
    """The used refs that have no usable file but do have a prompt."""
    return {n: r for n, r in shotlist_refs(pdir).items()
            if not r["provided"] and r["prompt"]}


def _write_refs_job(cfg, pdir: Path, pid: int, refs: dict) -> Path:
    """A FlowBatch job that renders one image per reference, named exactly
    after the ref, so the result can be attached by name later."""
    # the same style source the main job uses: the shotlist's style field, then
    # style.md - the refs should be drawn in the channel's art direction too
    style = ""
    try:
        style = str(json.loads((pdir / "shotlist.json")
                               .read_text(encoding="utf-8")).get("style") or "")
    except (OSError, ValueError):
        style = ""
    if not style.strip():
        style_path = find_style(pdir)
        style = style_path.read_text(encoding="utf-8") if style_path else ""
    style = style.strip()
    longest = max((len(r["prompt"]) for r in refs.values()), default=0)
    job: dict = {
        "name": f"wr-{pid}-refs",
        "outputsDir": str(pdir / "refs"),
        "refMode": "reuse",
        "defaults": {"mode": "image", "agent": False, "aspectRatio": "16:9",
                     "outputs": 1, "refMode": "reuse"},
        "images": [{"file": f"{name}.png", "prompt": r["prompt"]}
                   for name, r in refs.items()],
    }
    url, _ = flow_project_url_for(cfg, pid)
    if url:
        job["projectUrl"] = url
    # the channel's art direction, when it fits Flow's prompt ceiling
    if style and longest + len(style) + 1 <= FLOWBATCH_MAX_PROMPT_CHARS:
        job["style"] = style
    elif style:
        log.info("refs: omitting the %d-char style - ref prompts would exceed "
                 "Flow's %d-char limit", len(style),
                 FLOWBATCH_MAX_PROMPT_CHARS)
    job_path = pdir / "flowbatch_refs.json"
    job_path.write_text(json.dumps(job, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    return job_path


def run_flowbatch_refs(cfg, pdir: Path, pid: int, refs: dict,
                       log=None, cancel=None, upscale: int | None = None) -> dict:
    """Render the missing reference images with FlowBatch, then place them
    so BOTH engines can use them: as files in the production's refs\\ (the
    Renderly API resolves names there) and with the shotlist's registry
    path filled in (so FlowBatch's prepare uploads them by name into the
    Flow project gallery).

    Returns {generated: [names], missing: [names]} - nothing raises for a ref
    that could not be made; the caller reports it."""
    log = log or (lambda m: None)
    if not refs:
        return {"generated": [], "missing": []}
    if not flowbatch_ready(cfg):
        raise RuntimeError(
            "reference generation needs FlowBatch - set "
            "studio.flowbatch_repo in config.yaml")
    repo = flowbatch_dir(cfg)
    job_path = _write_refs_job(cfg, pdir, pid, refs)
    # Reference images are only ever uploaded as references, so they are never
    # upscaled (the `upscale` argument is kept for the callers' signature).
    tier = set_flowbatch_tier(cfg, 0)
    from . import chrome_profile
    chrome_profile.apply(cfg, "flowbatch", log)
    cmd = _flowbatch_cmd(["generate", "--job", str(job_path),
                              "--output", str(pdir / "refs"),
                              "--no-color"])
    url, _ = flow_project_url_for(cfg, pid)
    if url:
        cmd += ["--project-url", url]
    _safe_log(log, f"refs: generating {len(refs)} reference image(s) "
                   f"(upscale tier {tier})")
    _safe_log(log, "$ " + " ".join(cmd))
    code, tail = _flowbatch_stream(cmd, repo, log, cancel)
    if code != 0:
        raise RuntimeError(
            f"reference generation failed (exit {code}): "
            + " | ".join(tail[-4:])[:300])

    # FlowBatch wrote <name>.png straight into refs\ (no staging folder);
    # point the registry at the local file so prepare uploads it under the
    # ref's own name
    refs_dir = pdir / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    generated, missing = [], []
    for name in refs:
        target = refs_dir / f"{name}.png"
        if not target.is_file():
            alt = next((p for p in (refs_dir / f"{name}.jpg",
                                    refs_dir / f"{name}.jpeg") if p.is_file()),
                       None)
            if alt is None:
                missing.append(name)
                continue
            alt.replace(target)  # the registry names refs\<name>.png
        generated.append(name)
    if generated:
        (pdir / REFS_GENERATED_MANIFEST).write_text(
            json.dumps(sorted(generated), indent=2) + "\n", encoding="utf-8")
        _update_ref_paths(pdir, generated)
        log(f"refs: {len(generated)} reference image(s) ready in refs\\ - "
            f"{', '.join(generated[:6])}")
    if missing:
        log(f"refs: {len(missing)} could not be generated: "
            f"{', '.join(missing[:8])}")
    return {"generated": generated, "missing": missing}


def _update_ref_paths(pdir: Path, names: list[str]) -> None:
    """Point the shotlist registry at the generated files (read-modify-write)."""
    shotlist_path = pdir / "shotlist.json"
    try:
        data = json.loads(shotlist_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    registry = data.get("refs")
    if not isinstance(registry, dict):
        registry = {}
    changed = False
    for name in names:
        current = registry.get(name)
        wanted = f"refs/{name}.png"
        if not (isinstance(current, str) and current.strip()):
            registry[name] = wanted
            changed = True
    if changed:
        data["refs"] = registry
        shotlist_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")


def run_renderly_refs(cfg, pdir: Path, pid: int, refs: dict, channel=None,
                      upscale=None, log=None, cancel=None) -> dict:
    """Render the missing reference images through the SAME Renderly image
    engine run_imagegen() uses for the production's real shots - so a
    Renderly channel never has to depend on FlowBatch (or a Flow login) just
    to make its reference images. Mirrors run_flowbatch_refs()'s contract
    exactly: same signature shape, same return value, same placement of the
    results (refs\\<name>.png + the shotlist registry updated).

    Builds a throwaway, shotlist-shaped project folder with one image per ref
    (file=<NAME>.png, prompt=refPrompts[NAME]) and points run_imagegen() at
    it, so the actual render/upscale call is reused unchanged rather than
    forked into a second subprocess path. The temp folder is discarded once
    the results are copied into the real production's refs\\.

    `cancel` is accepted for signature parity with run_flowbatch_refs() but,
    like the plain Renderly images path in _run_images(), is not honored
    mid-render - run_imagegen() itself has no cancellation hook.

    Returns {generated: [names], missing: [names]} - nothing raises for a
    ref that could not be made; the caller reports it."""
    log = log or (lambda m: None)
    if not refs:
        return {"generated": [], "missing": []}
    # the same style source the FlowBatch refs job uses - the shotlist's own
    # style field, then style.md - so refs share the channel's art direction
    style = ""
    try:
        style = str(json.loads((pdir / "shotlist.json")
                               .read_text(encoding="utf-8")).get("style") or "")
    except (OSError, ValueError):
        style = ""
    if not style.strip():
        style_path = find_style(pdir)
        style = style_path.read_text(encoding="utf-8") if style_path else ""
    style = style.strip()

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"wr-{pid}-refs-"))
    try:
        job: dict = {
            "shots": [{"asset": name, "cues": "1-1"} for name in refs],
            "images": [{"file": f"{name}.png", "prompt": r["prompt"]}
                      for name, r in refs.items()],
        }
        if style:
            job["style"] = style
        (tmp_dir / "shotlist.json").write_text(
            json.dumps(job, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        _safe_log(log, f"refs: generating {len(refs)} reference image(s) via "
                       "Renderly")
        run_imagegen(cfg, tmp_dir, channel=channel, upscale=upscale)
        out_dir = tmp_dir / "images"
        refs_dir = pdir / "refs"
        refs_dir.mkdir(parents=True, exist_ok=True)
        generated, missing = [], []
        for name in refs:
            src = next((p for p in (out_dir / f"{name}.png",
                                    out_dir / f"{name}.jpg",
                                    out_dir / f"{name}.jpeg")
                       if p.is_file()), None)
            if src is None:
                missing.append(name)
                continue
            shutil.copy(src, refs_dir / f"{name}.png")
            generated.append(name)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if generated:
        (pdir / REFS_GENERATED_MANIFEST).write_text(
            json.dumps(sorted(generated), indent=2) + "\n", encoding="utf-8")
        _update_ref_paths(pdir, generated)
        log(f"refs: {len(generated)} reference image(s) ready in refs\\ - "
            f"{', '.join(generated[:6])}")
    if missing:
        log(f"refs: {len(missing)} could not be generated: "
            f"{', '.join(missing[:8])}")
    return {"generated": generated, "missing": missing}


def _shot_motion(file_name: str) -> str:
    """The motion code of a shotlist image file name (S01_02_SCN_PL.png -> PL)."""
    stem = file_name[:-4] if file_name.lower().endswith(".png") else file_name
    return stem.rsplit("_", 1)[-1].upper()


def missing_shot_files(pid_dir: Path, motions=None, exclude=None) -> list[str]:
    """Shotlist image files not yet in images\\, optionally only those whose
    motion code is in `motions` or not in `exclude` (e.g. ("PL", "PR"))."""
    try:
        data = json.loads((pid_dir / "shotlist.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    img_dir = pid_dir / "images"
    have = ({p.name for p in img_dir.iterdir() if p.is_file()}
            if img_dir.exists() else set())
    keep = {m.upper() for m in motions} if motions else None
    drop = {m.upper() for m in exclude} if exclude else set()
    out = []
    for item in data.get("images", []):
        if not (isinstance(item, dict) and item.get("file")
                and item.get("prompt") and item["file"] not in have):
            continue
        motion = _shot_motion(item["file"])
        if (keep is not None and motion not in keep) or motion in drop:
            continue
        out.append(item["file"])
    return out


def _set_production_warning(cfg, pid: int, text: str) -> None:
    """Best-effort: put a notice on the production so it is seen, not just
    logged."""
    from . import db

    try:
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            db.update_production(conn, pid, warning=text)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _clear_style_warning(cfg, pid: int) -> None:
    """Drop a stale "style not attached" notice once the style fits again."""
    from . import db

    try:
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
            warn = (prod["warning"] if prod is not None else None) or ""
            if warn.startswith("Visual style NOT attached"):
                db.update_production(conn, pid, warning=None)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def prepare_flowbatch_job(cfg, pid_dir: Path, pid: int,
                              project_url: str | None = None,
                              skip_motion=None) -> tuple[Path, list[str]]:
    """Build a FlowBatch job from the production's shotlist: only the
    images still missing from images\\.

    Per-image refs stay as NAMES and the shotlist's refs registry becomes the
    job's name -> path map, so FlowBatch's default refMode "reuse" attaches
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
    skip = {m.upper() for m in skip_motion} if skip_motion else set()
    for item in data.get("images", []):
        if not (isinstance(item, dict) and item.get("file")
                and item.get("prompt") and item["file"] not in existing):
            continue
        if skip and _shot_motion(item["file"]) in skip:
            continue    # left for another engine (PL/PR go to the API)
        entry = {"file": item["file"], "prompt": item["prompt"]}
        stem = (item["file"][:-4] if item["file"].lower().endswith(".png")
                else item["file"])
        motion = stem.rsplit("_", 1)[-1]
        flow_aspect = FLOWBATCH_ASPECT_BY_MOTION.get(motion)
        if flow_aspect:
            entry["aspectRatio"] = flow_aspect
        if item.get("refs"):
            # only attach refs that actually resolve to a file (registry or
            # refs\\ folder); a ref that was never generated must not stall
            # the whole batch - the image renders from its inline prompt
            usable = [str(r) for r in item["refs"] if str(r) in registry]
            if usable:
                entry["refs"] = usable
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
        log.info("FlowBatch: Flow project from %s", source)
    else:
        # No stored project: the images stage creates a NEW one (FlowBatch
        # --new-project) and fails if it cannot, so Flow's most-recent project
        # is never adopted silently.
        log.info("FlowBatch: no stored Flow project for production %s - a new "
                 "one will be created before rendering", pid)
        from . import db, settings

        try:
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            try:
                prod = db.get_production(conn, pid)
                if prod is not None and (settings.row_get(prod, "flow_project_url")
                                         or "").strip() == "":
                    db.update_production(conn, pid, warning=None)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass
    # The art direction (the shotlist's `style`, else the production's
    # style.md - the same source the refs job uses) is prefixed to EVERY
    # prompt by FlowBatch, and Flow refuses a prompt over its ceiling, so it is
    # sent only when it fits. That decision is made against the longest prompt
    # of the WHOLE shotlist, not just the images still missing: otherwise a
    # resume (a different remainder) could attach the style to some images and
    # silently not to others. When it cannot fit the production gets a visible
    # warning instead of a log line nobody reads.
    style = (data.get("style") or "").strip()
    if not style:
        style_path = find_style(pid_dir)
        if style_path:
            try:
                style = style_path.read_text(encoding="utf-8").strip()
            except OSError:
                style = ""
    longest = max((len(str(i.get("prompt") or ""))
                   for i in data.get("images", []) if isinstance(i, dict)),
                  default=0)
    if style and longest + len(style) + 1 <= FLOWBATCH_MAX_PROMPT_CHARS:
        job["style"] = style
        _clear_style_warning(cfg, pid)
    elif style:
        note = (f"Visual style NOT attached to the Flow images: the style is "
                f"{len(style)} chars and the longest image prompt {longest}, "
                f"over Flow's {FLOWBATCH_MAX_PROMPT_CHARS}-char limit. Shorten "
                f"the style or the longest prompts, then re-plan or resume.")
        log.warning("FlowBatch: %s", note)
        _set_production_warning(cfg, pid, note)
    else:
        note = ("No visual style found (shotlist style / style.md empty): "
                "the Flow images are rendered from their own prompts only.")
        log.warning("FlowBatch: %s", note)
    job_path = pid_dir / "flowbatch.json"
    job_path.write_text(json.dumps(job, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    return job_path, [i["file"] for i in todo]


def _flowbatch_cmd(args: list[str]) -> list[str]:
    return ["node", "src/cli.js", *args]


def set_flowbatch_tier(cfg, upscale: int) -> str | None:
    """Remember the upscale tier in FlowBatch's own local config (the
    same thing its web UI does). Returns the tier name."""
    d = flowbatch_dir(cfg)
    if not d:
        return None
    tier = FLOWBATCH_TIERS.get(int(upscale or 0), "off")
    try:
        subprocess.run(_flowbatch_cmd(["upscale", "--set-tier", tier]),
                       cwd=str(d), capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("FlowBatch: could not set upscale tier %s: %s",
                    tier, exc)
    return tier


def upscale_images_locally(cfg, pid_dir: Path, tier=None,
                           log=None, cancel=None) -> dict:
    """Upscale a production's rendered stills IN PLACE with the local
    Real-ESRGAN engine.

    This is the engine-independent half of "download Flow native first, then
    upscale when the downloads finish": the images stage runs either engine
    at native size and calls this once at the end, and the IMAGES stage's
    manual button can run it again at any time. It never talks to Flow and
    never re-renders anything.

    The upscaler is FlowBatch's (Real-ESRGAN ncnn-Vulkan; a Lanczos CPU
    fallback only when no GPU can run it - that is logged and counted as
    `lanczos`). The Renderly backend's upscaler is deliberately NOT used.
    Idempotent: FlowBatch's --in-place pass skips files already at the tier,
    so re-running it can never upscale an upscale.

    `tier` is an int tier (0-4, like default_upscale) or a FlowBatch tier
    name ("off" | "1k" | "2k" | "4k"); None = the configured default.

    Returns {engine, tier, total, upscaled, skipped, failed} (+ `lanczos`
    when some files used the CPU fallback)."""
    log = log or (lambda m: None)
    if tier is None:
        tier = cfg.renderly_upscale
    tier_name = (str(tier).strip().lower() if isinstance(tier, str)
                 else FLOWBATCH_TIERS.get(int(tier or 0), "off"))
    if tier_name not in ("1k", "2k", "4k"):
        tier_name = "off"
    result = {"engine": "off", "tier": tier_name, "total": 0,
              "upscaled": 0, "skipped": 0, "failed": 0}
    if tier_name == "off":
        _safe_log(log, "Local upscale: tier is off - nothing to do")
        return result
    img_dir = pid_dir / "images"
    files = sorted(img_dir.glob("*.png")) if img_dir.exists() else []
    result["total"] = len(files)
    if not files:
        _safe_log(log, "Local upscale: no PNG images to upscale yet")
        return result

    if not flowbatch_ready(cfg):
        raise RuntimeError(
            "No local upscaler available: the local Real-ESRGAN upscaler is "
            "FlowBatch's - set studio.flowbatch_repo in config.yaml to the "
            "FlowBatch checkout (run `npm install` there; the engine lives in "
            "its tools\\realesrgan folder)")
    result["engine"] = "flowbatch"
    repo = flowbatch_dir(cfg)
    cmd = _flowbatch_cmd(["upscale", str(img_dir), "--tier", tier_name,
                          "--in-place", "--no-color"])
    _safe_log(log, f"Local upscale: {len(files)} image(s) to {tier_name} "
                   f"in place with the local Real-ESRGAN upscaler - no Flow, "
                   f"nothing downloaded or re-rendered")
    _safe_log(log, "$ " + " ".join(cmd))
    stats = {"upscaled": 0, "skipped": 0, "lanczos": 0}

    def _count(line: str) -> None:
        if ": skipped (" in line:
            stats["skipped"] += 1
        elif " (in place) " in line:
            stats["upscaled"] += 1
            if "Lanczos" in line:
                stats["lanczos"] += 1

    code, tail = _flowbatch_stream(cmd, repo, log, cancel, on_line=_count)
    if code != 0:
        raise RuntimeError(
            "Local upscale failed (exit " + str(code) + "): "
            + " | ".join(tail[-4:])[:400])
    result["upscaled"] = stats["upscaled"]
    result["skipped"] = stats["skipped"]
    result["failed"] = max(
        0, len(files) - stats["upscaled"] - stats["skipped"])
    _safe_log(log, f"Local upscale: {stats['upscaled']} upscaled, "
                   f"{stats['skipped']} already at {tier_name}, "
                   f"{result['failed']} failed")
    if stats["lanczos"]:
        result["lanczos"] = stats["lanczos"]
        _safe_log(log, f"Local upscale: WARNING - {stats['lanczos']} image(s) "
                       f"used the CPU Lanczos fallback, not Real-ESRGAN (no "
                       f"usable GPU/Vulkan device). Check `node src/cli.js "
                       f"upscale` in FlowBatch for the detected device.")
    return result


def _adopt_flowbatch_outputs(pdir: Path, names: list[str],
                                 tier: str) -> list[str]:
    """Copy FlowBatch's results into images\\ under the shotlist's own
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
                # FlowBatch writes BOTH the Flow master and the upscaled
                # <stem>_<tier> file. Keep the master (the real source - a bad
                # upscale gets redone later) and drop ONLY the specific
                # upscaled file just adopted - it is now duplicated under
                # images\<name>. Do NOT glob-delete every "{stem}_*" file in
                # flow_images: a stale "{stem}_<othertier>" left over from an
                # earlier run/resume at a different tier setting was never
                # copied anywhere THIS time, so deleting it would be real
                # data loss, not cleanup - only `cand` itself (the file this
                # adopt actually consumed) is a genuine duplicate.
                if cand != name and cand.startswith(f"{stem}_"):
                    try:
                        src.unlink()
                    except OSError:
                        pass
                break
    return adopted


def _safe_log(log, message) -> None:
    """Log a child process line without ever raising. FlowBatch prints
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


FLOW_PREPARE_REPORT = "flow_prepare.json"
FLOW_RECOVER_REPORT = "flowbatch_recover.json"


def _read_json_retry(path: Path, attempts: int = 3, delay: float = 1.5):
    """Read a JSON file another process writes atomically. A missing file means
    "not written"; a malformed one is retried so a slow rename cannot look
    like corruption."""
    for i in range(attempts):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            if i == attempts - 1:
                return None
            time.sleep(delay)
    return None


def run_flowbatch_prepare(cfg, pid_dir: Path, job_path: Path,
                              log=None, new_project: bool = False) -> dict:
    """Ask FlowBatch to create-or-open this production's Flow project and
    get its references into the project gallery, then read back the report.

    The report is the frozen contract (see AGENTS.md): projectUrl, projectId,
    created, and per-ref {name, kind, status, path}. This is the receiving end,
    so FlowBatch only has to write it. It is OPTIONAL: a missing `prepare`
    command, a failure, or an absent report all return {} and generation
    proceeds with the stored URL - preparation must never block a batch.

    `new_project=True` passes FlowBatch --new-project, which refuses to open or
    adopt an existing project: it creates a fresh one or errors. The report's
    `created` flag is what the images stage checks to honor "never fall back to
    a previous project"."""
    log = log or (lambda m: None)
    report_path = pid_dir / FLOW_PREPARE_REPORT
    repo = flowbatch_dir(cfg)
    from . import chrome_profile
    chrome_profile.apply(cfg, "flowbatch", log)
    args = ["prepare", "--job", str(job_path),
            "--report", str(report_path), "--no-color"]
    if new_project:
        args.append("--new-project")
    cmd = _flowbatch_cmd(args)
    _safe_log(log, "$ " + " ".join(cmd))
    try:
        proc = _run_tracked(cmd, "FlowBatch (prepare)", timeout=3600,
                            cwd=str(repo), encoding="utf-8",
                            errors="replace")
    except BatchCancelled:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        _safe_log(log, f"FlowBatch prepare could not run: {exc}")
        return {}
    out = (proc.stdout or "") + (proc.stderr or "")
    for line in out.splitlines():
        if line.strip().startswith("FLOW_PROJECT_URL="):
            _safe_log(log, line.strip())
    if proc.returncode != 0:
        low = out.lower()
        if ("unknown command" in low or "unknown argument" in low
                or "usage:" in low):
            _safe_log(log, "FlowBatch has no 'prepare' command yet - "
                           "skipping project preparation; the Flow project "
                           "still comes from the DB/job")
            return {}
        detail = " | ".join(out.strip().splitlines()[-4:])[:280]
        _safe_log(log, f"FlowBatch prepare failed (exit "
                       f"{proc.returncode}): {detail}")
        # prepare writes the report the moment the project exists - BEFORE it
        # touches references. So a failure during the REF work still has a usable
        # project URL, and generate uploads the missing refs itself. Only a
        # failure with no usable project is a handshake failure.
        report = _read_json_retry(report_path)
        url = _usable_flow_project(report)
        if url:
            _safe_log(log, f"FlowBatch prepare opened/created {url} but its "
                           f"reference step failed - keeping the project; "
                           f"generate will attach the references")
            return report
        # a real failure, not "unsupported": the caller needs to know, because
        # a stored project URL that prepare could not open is probably dead and
        # generation will fail on it too
        return {"error": detail}
    report = _read_json_retry(report_path)
    if not isinstance(report, dict) or not report:
        _safe_log(log, "FlowBatch prepare wrote no readable report - "
                       "continuing with the stored project URL")
        return {}
    return report


def _apply_prepare_report(cfg, pid_dir: Path, pid: int, job_path: Path,
                          report: dict, log=None) -> None:
    """Persist what prepare reported, and switch the job to refMode 'assets'
    when every reference is already in the project gallery (attach by name,
    never upload)."""
    from . import db, settings

    log = log or (lambda m: None)
    if not report:
        return
    url = (report.get("projectUrl") or "").strip() or None
    project_id = (report.get("projectId") or "").strip() or None
    try:
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
            if prod is not None and url:
                had = (settings.row_get(prod, "flow_project_url")
                       or "").strip()
                db.update_production(conn, pid, flow_project_url=url,
                                     flow_project_id=project_id)
                if report.get("created") and had and had != url:
                    log(f"FlowBatch CREATED a new Flow project ({url}) "
                        f"even though production {pid} already had {had} - "
                        f"check for a duplicate project")
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - never block a batch
        log(f"could not persist the Flow project: {exc}")

    try:
        job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    changed = False
    if url and job.get("projectUrl") != url:
        job["projectUrl"] = url
        changed = True
    refs = report.get("refs")
    if isinstance(refs, list) and refs:
        present = {"uploaded", "reused", "generated"}
        ok = all(str(r.get("status") or "").lower() in present
                 for r in refs if isinstance(r, dict))
        mode = "assets" if ok else "reuse"
        if job.get("refMode") != mode:
            job["refMode"] = mode
            changed = True
        if isinstance(job.get("defaults"), dict) \
                and job["defaults"].get("refMode") != mode:
            job["defaults"]["refMode"] = mode
            changed = True
        missing = [str(r.get("name")) for r in refs if isinstance(r, dict)
                   and str(r.get("status") or "").lower() == "missing"]
        if ok:
            log(f"FlowBatch: all {len(refs)} reference(s) are in the "
                f"project - refMode 'assets' (no uploads)")
        elif missing:
            log(f"FlowBatch: {len(missing)} reference(s) missing from the "
                f"project: {', '.join(missing[:8])}")
    if changed:
        Path(job_path).write_text(
            json.dumps(job, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")


def _usable_flow_project(report) -> str:
    """The projectUrl from a prepare report, or "" when it is missing or a dead
    page - Flow redirects a deleted (or other-account) project to
    .../404?reason=project."""
    url = (report.get("projectUrl") or "").strip() if isinstance(report, dict) else ""
    if not url or "404" in url or "/project/" not in url:
        return ""
    return url


def _strip_job_project_url(job_path: Path) -> None:
    """Drop a job's stored Flow project so the next prepare CREATES a new one."""
    try:
        job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(job, dict) or "projectUrl" not in job:
        return
    job.pop("projectUrl", None)
    try:
        Path(job_path).write_text(
            json.dumps(job, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass


def _require_flow_project(report, had_stored: bool) -> str:
    """The Flow project a FlowBatch batch may use, or raise.

    With no stored URL the batch MUST create a fresh project: opening Flow's
    most-recent project instead is the "silently reuse the wrong project" bug,
    so a report that is not `created` fails here. With a stored URL, opening it
    is expected; only a missing/dead project is fatal."""
    url = _usable_flow_project(report)
    if not url:
        detail = report.get("error") if isinstance(report, dict) else None
        raise RuntimeError(
            "FlowBatch did not open or create a Flow project"
            + (f": {detail}" if detail else ""))
    if not had_stored and not (isinstance(report, dict) and report.get("created")):
        raise RuntimeError(
            "FlowBatch was asked to create a NEW Flow project but did not "
            "report one - it opened an existing project instead. Refusing to "
            "reuse a previous project.")
    return url


# Google's "We noticed some unusual activity" refusal = the account/session is
# being throttled. Waiting a short while does not clear it, and every further
# attempt lowers the standing, so the batch stops at the FIRST one and the
# auto-resume waits images_throttle_wait_minutes (default 60) before resuming.
THROTTLE_PATTERN = re.compile(r"unusual activity", re.I)
# FlowBatch's consecutive-failure guard fired (a broken session/UI). Part of the
# error text, and what the images stage looks for to try a gallery recovery
# before it pauses: Flow usually generated some of those cards anyway.
FLOW_CARDS_FAILED_MARKER = "stopped after too many cards failed in a row"
THROTTLE_MARKER = "Flow is throttling this account"


def throttle_error(where: str, wait_minutes: int | None = None) -> RuntimeError:
    """The one error both engines raise for the 'unusual activity' block."""
    wait = (f"it resumes after the configured wait "
            f"({wait_minutes} min)" if wait_minutes
            else "it resumes after the configured wait")
    return RuntimeError(
        f"{THROTTLE_MARKER} ({where}: \"We noticed some unusual activity\"). "
        f"Retrying straight away only lowers the account's standing, so the "
        f"batch stopped at the first refusal; {wait}. Whatever rendered is "
        f"kept. Raising the delay between images lowers the risk.")


def image_delay_seconds(cfg, pid: int | None = None) -> int:
    """Extra seconds to wait between rendered images (images_delay_seconds
    setting; 0 = each engine's own default)."""
    from . import db, settings

    try:
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            glob = settings.load(conn)
        finally:
            conn.close()
        return max(0, int(glob.get("images_delay_seconds") or 0))
    except Exception as exc:  # noqa: BLE001 - never block a batch
        log.debug("could not read the image delay: %s", exc)
        return 0


def image_throttle_wait_seconds(cfg, pid: int) -> int:
    """Seconds to wait after Flow's 'unusual activity' block (the
    images_throttle_wait_minutes setting; 0 = no auto-resume)."""
    from . import db, settings

    try:
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            eff = settings.for_production(conn, db.get_production(conn, pid))
        finally:
            conn.close()
        return max(0, int(eff.get("images_throttle_wait_minutes", 60))) * 60
    except Exception as exc:  # noqa: BLE001 - never block a batch
        log.debug("could not read the throttle wait: %s", exc)
        return 3600


def image_batch_limits(cfg, pid: int) -> tuple[int, bool, int, int, int]:
    """(chunk_size, stop_on_failure, max_consecutive_failures,
    refusal_wait_seconds, still_busy_wait_seconds) for the images stage.

    Flow tolerates roughly 80-100 automated generations on one account before
    it refuses, and both engines drive the SAME account, so a long shotlist is
    chunked across runs and a failing batch is stopped instead of ground
    through. max_consecutive_failures is the FlowBatch
    CLI's back-to-back-failure stop (a broken session fails every further
    card); refusal_wait_seconds is how long auto-run/manual render waits before
    resuming after that refusal, and still_busy_wait_seconds the shorter wait
    after a plain 'still busy' wave. All global settings, shared by manual
    render and auto-run."""
    from . import db, settings

    try:
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            eff = settings.for_production(conn, db.get_production(conn, pid))
        finally:
            conn.close()
        return (max(0, int(eff.get("images_chunk_size") or 0)),
                bool(eff.get("images_stop_on_failure")),
                max(1, int(eff.get("images_max_consecutive_failures") or 5)),
                max(0, int(eff.get("images_resume_wait_minutes") or 0)) * 60,
                max(0, int(eff.get("images_still_busy_wait_minutes") or 0)) * 60)
    except Exception as exc:  # noqa: BLE001 - never block a batch
        log.debug("could not read the image batch limits: %s", exc)
        return 0, False, 5, 600, 300


def _flowbatch_stream(cmd: list[str], repo, log, cancel,
                      on_line=None) -> tuple[int, list[str]]:
    """Run a FlowBatch CLI call, streaming its stdout into the job log.
    Returns (exit code, the last output lines); `cancel` kills the process
    tree - FlowBatch spawns its own Chrome, so a plain proc.kill() would
    orphan the browser. The process is registered for the kill switch
    (`kill_image_batch`): a user kill raises BatchCancelled here, so no
    caller mistakes it for a failed batch and resumes. `on_line`, when given,
    sees every line (the tail only keeps the last 40, which is not enough to
    count a long upscale pass)."""
    proc = _popen_tracked(cmd, "FlowBatch", cwd=str(repo),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace")
    tail: list[str] = []
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            tail.append(line)
            del tail[:-40]
            _safe_log(log, line)
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:  # noqa: BLE001 - never break the run
                    log.debug("on_line callback failed", exc_info=True)
            if cancel is not None and cancel():
                raise BatchCancelled("stopped by user")
    finally:
        if proc.poll() is None:
            _kill_process_tree(proc)
        proc.wait(timeout=30)
        killed = _release_tracked(proc)
    if killed:
        raise BatchCancelled("FlowBatch was killed by the user")
    return proc.returncode or 0, tail


def run_flowbatch_recover(cfg, pid_dir: Path, pid: int,
                          upscale: int | None = None, log=None,
                          cancel=None,
                          project_url: str | None = None) -> dict:
    """Adopt images a stopped FlowBatch left in the Flow project's gallery.

    Triggered by the IMAGES stage "Recover from Flow gallery" button, and
    automatically when a batch stops on "too many cards failed in a row"
    (before the pause): Flow already generated them, so re-running the prompts would pay twice for the
    same stills. Builds the same missing-only job `generate` uses, asks the
    FlowBatch CLI to match gallery result tiles to those items and download
    what is missing into flow_images\\, then adopts it (which also sweeps up
    files a crashed run had downloaded but never adopted). Returns
    {recovered, still_missing} as shotlist file names. It never generates."""
    log = log or (lambda m: None)
    if not flowbatch_ready(cfg):
        raise RuntimeError("Set studio.flowbatch_repo in config.yaml to "
                           "your FlowBatch checkout (and run npm install)")
    names = shotlist_missing_images(pid_dir)
    if not names:
        return {"recovered": [], "still_missing": []}
    url, source = flow_project_url_for(cfg, pid, project_url)
    if not url:
        # Without the production's own project, recover would read Flow's
        # most recent gallery - another production's images could match a
        # reused prompt and be adopted under the wrong name.
        raise RuntimeError("Recovery needs this production's Flow project "
                           "URL - none is stored. Run the images stage once "
                           "(prepare records it), or set it on the channel.")
    repo = flowbatch_dir(cfg)
    job_path, _ = prepare_flowbatch_job(cfg, pid_dir, pid, project_url)
    tier = set_flowbatch_tier(cfg, cfg.renderly_upscale if upscale is None
                                   else upscale)
    report_path = pid_dir / FLOW_RECOVER_REPORT
    try:
        report_path.unlink()
    except OSError:
        pass
    from . import chrome_profile
    chrome_profile.apply(cfg, "flowbatch", log)
    cmd = _flowbatch_cmd(["recover", "--job", str(job_path),
                          "--output", str(pid_dir / "flow_images"),
                          "--report", str(report_path), "--no-color",
                          "--project-url", url])
    _safe_log(log, f"FlowBatch recover: {len(names)} missing image(s), Flow "
                   f"project from {source}, upscale tier {tier} - nothing "
                   f"will be generated")
    _safe_log(log, "$ " + " ".join(cmd))
    code, tail = _flowbatch_stream(cmd, repo, log, cancel)
    if code != 0:
        text = " ".join(tail).lower()
        if "unknown command" in text:
            raise RuntimeError(
                "This FlowBatch checkout has no 'recover' command yet - "
                "update FlowBatch, then try again")
        raise RuntimeError("FlowBatch recover failed (exit " + str(code)
                           + "): " + " | ".join(tail[-4:])[:400])
    report = _read_json_retry(report_path)
    if isinstance(report, dict) and "stillMissing" in report:
        _safe_log(log, f"FlowBatch recover report: "
                       f"{len(report.get('recovered') or [])} matched in the "
                       f"gallery, {len(report.get('stillMissing') or [])} "
                       f"unmatched")
    adopted = _adopt_flowbatch_outputs(pid_dir, names, tier or "off")
    still_missing = [n for n in names
                     if not (pid_dir / "images" / n).exists()]
    return {"recovered": adopted, "still_missing": still_missing}


def run_imagegen_flowbatch(cfg, pid_dir: Path, pid: int,
                               upscale: int | None = None, log=None,
                               cancel=None, project_url: str | None = None,
                               skip_motion=None) -> int:
    """Render the production's missing shotlist images with FlowBatch.

    Returns how many new images landed in images\\. Long-running by design:
    Flow rate-limits automation and the CLI waits it out, so the timeout is
    generous and `cancel` kills the whole process tree."""
    if not flowbatch_ready(cfg):
        raise RuntimeError("Set studio.flowbatch_repo in config.yaml to "
                           "your FlowBatch checkout (and run npm install)")
    repo = flowbatch_dir(cfg)
    job_path, names = prepare_flowbatch_job(cfg, pid_dir, pid, project_url,
                                            skip_motion=skip_motion)
    # No stored project -> CREATE a fresh one (never adopt Flow's most recent).
    # A stored project -> open it; if it is dead, strip it and create instead.
    # Both are verified below: reusing a previous project is a hard failure.
    stored, source = flow_project_url_for(cfg, pid, project_url)
    report = run_flowbatch_prepare(cfg, pid_dir, job_path, log,
                                   new_project=not stored)
    if stored and not _usable_flow_project(report):
        _safe_log(log, f"FlowBatch: the stored Flow project ({source}) is not "
                       f"usable - creating a new one")
        _strip_job_project_url(job_path)
        report = run_flowbatch_prepare(cfg, pid_dir, job_path, log,
                                       new_project=True)
    _require_flow_project(report, had_stored=bool(stored))
    _apply_prepare_report(cfg, pid_dir, pid, job_path, report, log)
    tier = set_flowbatch_tier(cfg, cfg.renderly_upscale if upscale is None
                                  else upscale)
    from . import chrome_profile
    chrome_profile.apply(cfg, "flowbatch", log)
    cmd = _flowbatch_cmd(["generate", "--job", str(job_path),
                              "--output", str(pid_dir / "flow_images"),
                              "--no-color"])
    chunk, stop_on_failure, max_consecutive_failures, _refusal_wait, _busy = (
        image_batch_limits(cfg, pid))
    if chunk:
        # bound this run: Flow's tolerance is per account and both engines
        # share it, so the rest of the shotlist waits for the next run
        cmd += ["--limit", str(chunk)]
    delay = image_delay_seconds(cfg, pid)
    if delay:
        # pacing: Google scores the session partly on generation speed
        cmd += ["--delay", str(delay)]
    if stop_on_failure:
        # the CLI's own flag stops at the first failed item (its granularity);
        # anything rendered is kept and the production stays resumable
        cmd += ["--fail-fast"]
    # back-to-back-failure guard (the one shared images_max_consecutive_failures
    # setting)
    cmd += ["--max-consecutive-failures", str(max_consecutive_failures)]
    if log and (chunk or stop_on_failure):
        _safe_log(log, "FlowBatch: batch guards - "
                       + (f"at most {chunk} image(s), " if chunk else "")
                       + ("stop on first failure" if stop_on_failure
                          else "no failure guard")
                       + f", stop after {max_consecutive_failures} in a row")
    # prefer whatever prepare learned, then the DB/job resolution
    resolved_url, source = flow_project_url_for(cfg, pid, project_url)
    if resolved_url:
        cmd += ["--project-url", resolved_url]
        if log:
            _safe_log(log, f"FlowBatch: Flow project from {source}")
    if log:
        _safe_log(log, f"FlowBatch: {len(names)} image(s), "
                       f"upscale tier {tier}")
        _safe_log(log, "$ " + " ".join(cmd))
    proc_code, tail = _flowbatch_stream(cmd, repo, log, cancel)
    if proc_code != 0:
        text = " ".join(tail).lower()
        if THROTTLE_PATTERN.search(text):
            raise throttle_error(
                "FlowBatch", image_throttle_wait_seconds(cfg, pid) // 60)
        if "failed in a row" in text:
            # FlowBatch's consecutive-failure guard stopped the batch: a broken
            # session/UI. Say "refusing this session" so the auto-resume treats
            # it like a throttle - waits the configured
            # images_resume_wait_minutes (10 by default) before retrying, rather
            # than the plain, shorter "were not produced" pause.
            raise RuntimeError(
                f"FlowBatch {FLOW_CARDS_FAILED_MARKER} - "
                f"Flow is refusing this session. Whatever rendered is kept "
                f"(state\\wr-{pid}.json); it resumes after the configured wait.")
        if "rate limit" in text or "refused the generation" in text:
            raise RuntimeError(
                "Flow is refusing this session - the generation was refused "
                "(a reCAPTCHA score on this browser profile, not a temporary "
                "limit). FlowBatch stops the batch on purpose rather than "
                "lowering the score further. It resumes after the configured "
                f"wait - finished images are kept in its state\\wr-{pid}.json. "
                "Raising delayBetweenItemsMs in FlowBatch's config/settings.json "
                "lowers the risk.")
        if "already in use" in text or "existing browser session" in text:
            raise RuntimeError(
                "FlowBatch could not open its browser profile because "
                "another Chrome is already using it - close the FlowBatch "
                "UI / that Chrome window, then Resume. (state is kept in its "
                f"state\\wr-{pid}.json)")
        if "executable doesn't exist" in text and "ms-playwright" in text:
            raise RuntimeError(
                "Playwright's browser is not installed - run "
                f"`npx playwright install chromium` in {repo}, or configure "
                "FlowBatch to use your system Chrome. (exit "
                f"{proc_code})")
        raise RuntimeError(
            f"FlowBatch failed (exit {proc_code}): "
            + " | ".join(tail[-4:])[:400]
            + f" - state is kept in its state\\wr-{pid}.json so a re-run "
              f"resumes")
    adopted = _adopt_flowbatch_outputs(pid_dir, names, tier or "off")
    if not adopted:
        raise RuntimeError("FlowBatch produced no images - check the log "
                           "and its debug\\ folder")
    # Flow refuses some prompts on content policy. It retries, then skips the
    # item and carries on, so the batch "succeeds" while images are missing -
    # and the merge would run on an incomplete set. Adopt what exists first
    # (nothing is lost, and a resume only re-renders the gaps), then report.
    missing = [n for n in names if not (pid_dir / "images" / n).exists()]
    if missing:
        if "failed in a row" in " ".join(tail).lower():
            raise RuntimeError(
                f"FlowBatch stopped on back-to-back failures ({len(missing)} "
                f"of {len(names)} not produced) - Flow is refusing this "
                f"session. The {len(adopted)} that rendered are kept; it "
                f"resumes after the configured wait.")
        raise RuntimeError(
            f"{len(missing)} of {len(names)} image(s) were not produced - "
            f"Flow refused them (usually content policy; see the "
            f"debug\\error-*.png captures). The {len(adopted)} that succeeded "
            f"are kept. Rewrite those prompts (or the shotlist), then Resume: "
            f"only the gaps are re-rendered. Missing: "
            + ", ".join(missing[:12])
            + (" …" if len(missing) > 12 else ""))
    return len(adopted)



class BatchCancelled(RuntimeError):
    """Raised when a caller-requested cancel interrupts a long batch."""


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


def _imgtovideo_cli(cfg) -> Path | None:
    repo = cfg.imgtovideo_repo
    cli = Path(repo, "src", "ImgToVideo.Cli") if repo else None
    return cli if cli and cli.exists() else None


def _run_imgtovideo_cli(cli: Path, args: list[str], timeout: int = 14400):
    """Run ImgToVideo.Cli headless; raise with the tail on a non-zero exit."""
    cmd = ["dotnet", "run", "--project", str(cli),
           "-c", "Release", "--", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        tail = ((result.stderr or "") + (result.stdout or ""))[-600:]
        raise RuntimeError(
            f"ImgToVideo.Cli {args[0]} failed (exit {result.returncode}): {tail}")
    return result


def run_merge_render(cfg, pid_dir: Path, target: str = "premiere") -> dict:
    """Merge with ImgToVideo.Cli (headless): build the fast preview draft,
    then export the NLE project for `target`. Returns
    {preview, target, project} - preview is the playable out\\preview.mp4,
    project is the exported XML/draft folder."""
    cli = _imgtovideo_cli(cfg)
    if not cli:
        raise RuntimeError("Set studio.imgtovideo_repo in config.yaml")
    if target not in RENDER_TARGETS:
        target = RENDER_TARGETS[0]

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

    # 1. fast preview draft - the reviewable cut
    _run_imgtovideo_cli(cli, ["render-final", str(pid_dir), "--preview"])
    preview = find_preview(pid_dir)
    if not preview:
        raise RuntimeError("preview build finished but no preview.mp4 found")

    # 2. NLE project for the chosen target
    _run_imgtovideo_cli(cli, [f"export-{target}", str(pid_dir)])
    project = nle_project(pid_dir, target)
    if not project:
        raise RuntimeError(
            f"{RENDER_TARGET_LABELS[target]} export finished but no project "
            f"file was found")
    return {"preview": preview, "target": target, "project": project}


# Output resolution presets written into the production's imgtovideo.json as
# output.width/height (ImgToVideo uses SnakeCaseLower). The project default is
# 2560x1440, so "2k" keeps today's behaviour. The PREVIEW stays at ImgToVideo's
# 960x540 draft - the shimmer seen on productions 5/6 was low-resolution
# B-frames, fixed in ImgToVideo by preview_bframes=0, not a resolution problem.
RENDER_RESOLUTIONS = {"1080p": (1920, 1080), "2k": (2560, 1440),
                      "4k": (3840, 2160), "flow-native": (1376, 768)}
RENDER_RESOLUTION_LABELS = {"1080p": "1920x1080 (Full HD)",
                            "2k": "2560x1440 (2K)",
                            "4k": "3840x2160 (4K)",
                            "flow-native": "1376x768 (Flow native)"}


def apply_render_resolution(cfg, pid: int) -> None:
    """Write the production's effective render resolution into imgtovideo.json.

    Read-modify-write, never a fresh file: ImgToVideo's options are a shared
    contract and it may hold keys we do not own (the same rule as the
    FlowBatch job)."""
    from . import db, settings

    pdir = prod_dir(cfg, pid)
    options_file = pdir / "imgtovideo.json"
    try:
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            eff = settings.for_production(conn, db.get_production(conn, pid))
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - never block a stage
        log.debug("could not resolve the render resolution: %s", exc)
        return
    size = RENDER_RESOLUTIONS.get(str(eff.get("render_resolution") or "").lower())
    if not size:
        return
    try:
        data = (json.loads(options_file.read_text(encoding="utf-8"))
                if options_file.exists() else {})
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    output = data.get("output")
    if not isinstance(output, dict):
        output = {}
    if output.get("width") == size[0] and output.get("height") == size[1]:
        return
    output["width"], output["height"] = size
    data["output"] = output
    data.setdefault("schema_version", 1)
    data.setdefault("naming", {"image_extensions": [".png", ".jpg", ".jpeg",
                                                   ".webp"]})
    options_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    log.info("render resolution %sx%s written to imgtovideo.json",
             size[0], size[1])


def _png_size(path: Path):
    """(width, height) from a PNG header, or None for anything else."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def image_size_warning(cfg, pid: int, sample: int = 8) -> str:
    """'' when the production's images are at least as wide as the render
    output (or there is nothing to compare); otherwise a sentence saying that
    ImgToVideo will have to scale them UP. Reads PNG headers only."""
    pdir = prod_dir(cfg, pid)
    try:
        data = json.loads((pdir / "imgtovideo.json").read_text(encoding="utf-8"))
        out_w = int((data.get("output") or {}).get("width") or 0)
    except (OSError, ValueError, TypeError):
        return ""
    img_dir = pdir / "images"
    if not out_w or not img_dir.is_dir():
        return ""
    sizes = []
    for p in sorted(img_dir.glob("*.png"))[:sample]:
        size = _png_size(p)
        if size:
            sizes.append(size)
    if not sizes:
        return ""
    # the long side: a 1:1 or 9:16 still is legitimately narrower than 16:9
    narrowest = min(max(w, h) for w, h in sizes)
    if narrowest >= out_w:
        return ""
    return (f"images are only {narrowest}px on their long side but the render output is "
            f"{out_w}px wide - ImgToVideo will scale them up (check Render "
            f"resolution / Upscale tier)")


def prepare_project_folder(cfg, pid: int) -> Path:
    """Make the production folder a valid ImgToVideo project folder."""
    pdir = prod_dir(cfg, pid)
    options_file = pdir / "imgtovideo.json"
    if not options_file.exists():
        options_file.write_text(json.dumps({
            "schema_version": 1,
            "naming": {"image_extensions": [".png", ".jpg", ".jpeg", ".webp"]},
        }, indent=2), encoding="utf-8")
    apply_render_resolution(cfg, pid)
    warning = image_size_warning(cfg, pid)
    if warning:
        log.warning("production %s: %s", pid, warning)
    return pdir


def _saved_llm_settings(cfg) -> tuple[list | None, str | None]:
    """The saved LLM settings: (nested providers, llm_default) from the
    database, or (None, None) when nothing is stored yet (or the DB read
    failed - a settings failure must not take the pipeline down).

    The settings table is the single source of truth; config.yaml no longer
    carries any LLM providers or a default LLM."""
    try:
        from . import db

        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            raw = db.get_setting(conn, "llm_providers")
            raw_default = db.get_setting(conn, "llm_default")
        finally:
            conn.close()
        nested = None
        if raw:
            nested = json.loads(raw) if isinstance(raw, str) else raw
        default = (raw_default or "").strip() or None
        return nested, default
    except Exception as exc:  # noqa: BLE001 - never block on settings storage
        log.debug("could not read saved LLM settings: %s", exc)
        return None, None


def _flatten_nested(nested: list | None) -> list[dict]:
    """The nested gateway/model list saved by Settings > Providers, flattened
    to the flat shape the pipeline uses: one entry per MODEL, named by its
    label, sharing the gateway's base_url and key."""
    flat: list[dict] = []
    for prov in nested or []:
        if not isinstance(prov, dict) or not str(prov.get("name") or "").strip():
            continue
        for model in prov.get("models") or []:
            if isinstance(model, str):
                model = {"id": model}
            if not isinstance(model, dict) or not str(model.get("id") or "").strip():
                continue
            flat.append({
                "name": str(model.get("name") or model["id"]).strip(),
                "base_url": str(prov.get("base_url") or "").strip(),
                "model": str(model["id"]).strip(),
                "api_key": prov.get("api_key"),
                "env_key": str(prov.get("env_key") or "WR_LLM_API_KEY").strip(),
                "gateway": str(prov.get("name")).strip(),
            })
    return flat


def providers(cfg) -> list[dict]:
    """The LLM providers the pipeline can use, as flat {name, base_url, model,
    api_key, env_key, gateway} entries.

    The Settings > Providers page saves a NESTED list to the DB (one entry per
    gateway + key, each with several models - b.ai and OpenRouter both serve
    many models on one key). This flattens that to the flat shape the rest of
    the pipeline already uses. Empty when nothing has been saved yet - manage
    providers in Settings > LLM providers."""
    nested, _default = _saved_llm_settings(cfg)
    return _flatten_nested(nested)


def llm_default(cfg) -> str | None:
    """The global default LLM saved in the database (Settings > LLM), or None
    when nothing is saved."""
    return _saved_llm_settings(cfg)[1]


def providers_nested(cfg) -> list[dict]:
    """The RAW nested provider list for the Settings > Providers editor:
    whatever the page saved. Empty until providers are saved there - there is
    no config.yaml seed anymore."""
    nested, _default = _saved_llm_settings(cfg)
    return nested if isinstance(nested, list) else []


def _resolve_provider(cfg, name: str | None = None) -> dict:
    provs = providers(cfg)
    if not provs:
        raise RuntimeError("No LLM providers configured (Settings > LLM providers)")
    name = name or llm_default(cfg)
    for p in provs:
        if p["name"] == name:
            return p
    raise RuntimeError(f"Unknown LLM provider '{name}'")


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


class LLMStalled(RuntimeError):
    """The provider accepted the request but never produced content. Raised so a
    caller can retry on a DIFFERENT provider instead of waiting out the whole
    timeout: glm-flash stalls on large prompts (keep-alive lines keep the socket
    busy, so the socket timeout never fires). deepseek/GPT/Claude-class models
    do not; the guard is about a provider that goes quiet, not about length."""


class LLMEmpty(RuntimeError):
    """The provider answered but returned no content - a gateway glitch that is
    worth retrying on another provider (glm-flash returned this today)."""


def _rejects_temperature(exc: Exception) -> bool:
    """True when a provider's error is specifically about the `temperature`
    parameter - some reasoning-style models (gpt-6-luna hit this) reject any
    value but their own default. Narrow on purpose: a real outage, a bad key
    or a malformed request must never match this and get treated as if
    stripping temperature would fix it."""
    msg = str(exc).lower()
    return "temperature" in msg and (
        "unsupported parameter" in msg
        or "not supported with this model" in msg
        or "does not support" in msg)


def _rejects_max_tokens(exc: Exception) -> bool:
    """True when a provider's error is specifically about the `max_tokens`
    value itself (too large for the model, an unsupported parameter, etc) -
    same narrow-on-purpose reasoning as _rejects_temperature. Added for
    shotlist_max_tokens: an explicit budget sized from the narration (see its
    docstring) is a generous ESTIMATE, not a verified-safe value for every
    provider's real ceiling, so a provider that hard-rejects it must fall
    back to no cap (the old behavior: truncate and continue) rather than
    failing the whole attempt outright."""
    msg = str(exc).lower()
    return "max_tokens" in msg and (
        "exceeds" in msg or "maximum" in msg or "too large" in msg
        or "invalid" in msg or "not supported" in msg
        or "unsupported parameter" in msg)


# A big prompt can take a while to START streaming: deepseek needed ~118s for a
# 32k-char prompt, so a short idle rule would kill a healthy call. Allow much
# longer for the FIRST token than for the gaps between tokens. Keep-alive lines
# never count as content.
LLM_FIRST_TOKEN_TIMEOUT = 300
LLM_IDLE_TIMEOUT = 90


def script_max_tokens(words: int) -> int:
    """A generous ceiling for a script of `words` words. Prose runs ~1.3-1.6
    tokens per word; the headroom keeps a slightly long draft from being cut
    while still bounding generation time (no max_tokens = unbounded ramble)."""
    return int(max(0, words) * 1.6) + 200


def script_target_words(setting: int | None, source_words: int) -> int:
    """The written script is never planned LONGER than the transcript it is
    based on - a longer target only makes generation slower without adding
    substance. An explicit `studio.script_words` still wins when it is shorter."""
    wanted = int(setting or 0) or source_words or 1200
    cap = source_words or wanted
    return min(wanted, cap)


# How many times a truncated draft gets asked to finish itself before the
# attempt is scored as-is. Mirrors SHOTLIST_CONTINUE_ROUNDS's reasoning, but a
# script rarely needs more than one continuation to reach a natural ending.
SCRIPT_CONTINUE_ROUNDS = 2

# Characters treated as trailing "decoration" around a sentence's real end
# (a closing quote, parenthesis, or stray markdown emphasis marker) - stripped
# before checking for terminal punctuation, so `he said "no."` and `*done.*`
# both read as complete.
_SCRIPT_TRAILING_CLOSERS = ('"', "'", '”', '’', ')', ']', '*', '_')


def script_looks_truncated(text: str) -> bool:
    """True when a draft stops mid-sentence or mid-word instead of reaching a
    real ending.

    This is a distinct failure from running long: on a long single-pass
    generation the model sometimes just stops before finishing the thought,
    independent of script_max_tokens's headroom (confirmed on real attempts
    that cut off well short of their token budget, at word counts LOWER than
    other attempts that finished cleanly). The judge's `ending` criterion
    scores this a flat 1 regardless of how good everything before the cutoff
    was, which craters the whole attempt for a reason that has nothing to do
    with writing quality - so it is worth detecting and continuing rather
    than burning the attempt on it."""
    t = (text or "").rstrip()
    if not t:
        return True
    while t and t[-1] in _SCRIPT_TRAILING_CLOSERS:
        t = t[:-1]
    return not t.endswith((".", "!", "?"))


def script_continuation_prompt(base_prompt: str, partial: str) -> str:
    """Ask for the rest of a script that stopped mid-sentence/mid-word.

    Only the TAIL of what was written is re-sent - the base prompt already
    carries the facts and style guide - mirroring continuation_prompt's
    approach for a cut-off shotlist.

    The reply is joined onto `partial` with NO separator inserted in code
    (see the join in _run_script) - whether a space belongs at the seam is
    the model's call, not something a blind "\n\n" or " " can get right for
    both a mid-word cut and a mid-sentence one. Getting this wrong is not
    cosmetic: a real run once produced "...out of necessity r\n\nather than
    preference..." because the join inserted a paragraph break INSIDE the
    word "rather", and the resulting mangled text tanked that attempt's
    structure/pacing scores well below what the actual writing deserved."""
    tail = (partial or "")[-2000:]
    return (base_prompt.rstrip()
            + "\n\n---\n\nYOUR PREVIOUS OUTPUT WAS CUT OFF before it reached "
            "a conclusion. It ended with:\n" + tail
            + "\n\nYour reply will be joined DIRECTLY onto that text with "
            "nothing inserted between them - so if it stopped mid-word, "
            "start with the rest of that exact word and no leading space; "
            "if it stopped between two complete words, start with a single "
            "leading space before the next word. Continue from EXACTLY "
            "where it stopped - the same sentence, the same voice - with no "
            "repetition of anything already written, no restating the "
            "introduction, and no commentary about continuing or being cut "
            "off. Write only the remaining script, and bring it to ONE "
            "single, clear ending (not several).")


def _pinned_fallback_provider(cfg) -> str | None:
    """Settings > LLM > Fallback LLM: a global override read straight from
    the database (this runs inside the LLM transport, with no production/pid
    in scope for the usual channel<-production override chain - see
    `settings.llm_fallback_provider`'s help text)."""
    try:
        from . import db

        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            raw = db.get_setting(conn, "llm_fallback_provider")
        finally:
            conn.close()
        return (raw or "").strip() or None
    except Exception as exc:  # noqa: BLE001 - never block on settings storage
        log.debug("could not read the pinned fallback provider: %s", exc)
        return None


def _fallback_provider(cfg, failed: str) -> dict | None:
    """A different READY provider to retry a stalled/empty/rejected request
    on, or None.

    A pinned choice (Settings > LLM > Fallback LLM) wins outright when it is
    itself ready and is not the provider that just failed. Otherwise prefers
    a DIFFERENT gateway - today all providers sit on the one api.b.ai
    gateway, so a stall there repeats on any of them, but this matters once
    a second gateway (OpenRouter, Claude, ...) is configured."""
    providers_list = providers(cfg)
    by_name = {p["name"]: p for p in providers_list}

    pinned = _pinned_fallback_provider(cfg)
    if pinned and pinned != failed and pinned in by_name             and provider_ready(cfg, pinned):
        return by_name[pinned]

    failed_url = next((str(p.get("base_url") or "").rstrip("/")
                       for p in providers_list if p.get("name") == failed), "")
    ready = [p for p in providers_list
             if p.get("name") != failed and provider_ready(cfg, p.get("name"))]
    for p in ready:
        if str(p.get("base_url") or "").rstrip("/") != failed_url:
            return p
    return ready[0] if ready else None


def _curl_binary() -> str | None:
    """Path to a curl executable, or None if unavailable. Cheap to call
    per-request (this is a Windows PATH lookup, not a subprocess)."""
    return shutil.which("curl")


def _curl_config(url: str, headers: dict, payload: dict) -> str:
    """A curl -K config, so the request (incl. the Authorization header)
    travels over stdin instead of argv - it never shows up in Task Manager
    / `ps` / shell history the way a full curl command line would."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    lines = [f'url = "{esc(url)}"', "silent", "show-error", "fail-with-body"]
    for k, v in headers.items():
        lines.append(f'header = "{esc(k)}: {esc(v)}"')
    lines.append(f'data = "{esc(json.dumps(payload))}"')
    return "\n".join(lines) + "\n"


def _spawn_curl(config_text: str, timeout: int) -> subprocess.Popen:
    """Start curl reading its request from that config text on stdin.

    curl uses the OS's own TLS stack (Schannel on Windows); Python's
    ``urllib``/``ssl`` uses its bundled OpenSSL. Some networks' HTTPS-
    inspecting security software (or a router doing deep packet inspection)
    triggers a mid-connection TLS renegotiation that Schannel handles
    transparently but that OpenSSL-based clients can hang on forever - the
    request never errors, it just never delivers a byte. That is invisible
    from curl (works instantly) and from the provider's own dashboard (a
    stalled stream showed no tokens either way), and it can affect one PC on
    a network while another PC/network is unaffected. Shelling out to curl
    sidesteps it entirely.
    """
    proc = subprocess.Popen(
        [_curl_binary(), "-s", "-S", "-N", "--max-time", str(timeout + 30),
         "-K", "-"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, encoding="utf-8",
        # text=True with no encoding falls back to locale.getpreferredencoding(),
        # which on Windows is the system ANSI codepage (e.g. cp1252), not UTF-8.
        # The API's SSE response is UTF-8, so every multi-byte character (a
        # curly apostrophe, an em dash, an accented name) got decoded one byte
        # at a time as if it were cp1252 - "'" (E2 80 99) became "â"
        # read back as "â€™" ("a-circumflex, euro, trademark"),
        # i.e. exactly the "a€™" mojibake seen throughout every judge review
        # and generated script. The request side never had this problem (the
        # outgoing JSON is built with json.dumps()'s default ensure_ascii=True,
        # so it's already plain ASCII \uXXXX escapes before curl ever sees it)
        # - only curl's stdout was ever mis-decoded.
    )
    try:
        proc.stdin.write(config_text)
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
    return proc


def _curl_read_stream(proc: subprocess.Popen, provider_name: str,
                      timeout: int) -> str:
    """Read curl's SSE stdout with the same first-token/idle stall guards as
    the urllib path (LLM_FIRST_TOKEN_TIMEOUT / LLM_IDLE_TIMEOUT), killing
    curl if it stalls instead of waiting out `timeout`."""
    q: queue.Queue = queue.Queue()

    def _pump():
        try:
            for line in proc.stdout:
                q.put(line)
        except Exception:  # noqa: BLE001 - pipe torn down; treat as EOF
            pass
        finally:
            q.put(None)

    threading.Thread(target=_pump, daemon=True).start()

    started = time.monotonic()
    deadline = started + timeout
    last_content = None
    parts: list = []
    tail: list = []  # last few raw lines, for an error message on failure
    while True:
        now = time.monotonic()
        if now > deadline:
            proc.kill()
            raise LLMStalled(f"'{provider_name}' produced no complete answer "
                             f"in {timeout}s")
        if last_content is None and now - started > LLM_FIRST_TOKEN_TIMEOUT:
            proc.kill()
            raise LLMStalled(f"'{provider_name}' sent no first token for "
                             f"{LLM_FIRST_TOKEN_TIMEOUT}s")
        if last_content is not None and now - last_content > LLM_IDLE_TIMEOUT:
            proc.kill()
            raise LLMStalled(f"'{provider_name}' stalled mid-answer "
                             f"({LLM_IDLE_TIMEOUT}s without content)")
        try:
            line = q.get(timeout=0.5)
        except queue.Empty:
            continue
        if line is None:
            break
        tail.append(line)
        del tail[:-20]
        text_line = line.strip()
        if not text_line.startswith("data:"):
            continue
        chunk = text_line[5:].strip()
        if chunk == "[DONE]":
            break
        try:
            delta = json.loads(chunk)["choices"][0].get("delta", {})
        except (ValueError, KeyError, IndexError):
            continue
        content = delta.get("content") or ""
        if content:
            parts.append(content)
            last_content = time.monotonic()
        elif delta.get("reasoning_content"):
            # a reasoning/"thinking" model (mimo, deepseek-r1-style, ...)
            # streams its chain-of-thought as reasoning_content BEFORE any
            # real content - that can legitimately run past the first-token
            # timeout on a big planning prompt (500+ shots). It is not the
            # answer, so it is not appended to parts, but it proves the
            # request is alive and must reset the stall clock the same way
            # real content does, or a slow-to-think model gets killed as
            # "stalled" mid-thought on every large prompt.
            last_content = time.monotonic()

    returncode = proc.wait(timeout=10)
    if returncode != 0:
        stderr = (proc.stderr.read() or "").strip()
        body = "".join(tail).strip()
        # `parts` only fills once real SSE "data:" delta content has been
        # decoded. A non-zero curl exit AFTER that point means the stream
        # was flowing and then the connection was cut, not that the
        # provider sent a bad reply - the raw partial JSON in `body` is
        # just the tail of a perfectly normal stream, so dumping it as
        # "the error" reads as gibberish to a person. Say plainly that the
        # connection dropped instead. A non-zero exit with NO decoded
        # content (e.g. a synchronous JSON error body, never a stream at
        # all) keeps showing that body - it's the actual, useful error.
        if parts:
            log.warning("'%s' curl exited %s mid-stream; last output: %s",
                       provider_name, returncode, body[:300] or "(none)")
            why = f" ({stderr})" if stderr else f" (curl exit {returncode})"
            raise RuntimeError(
                f"'{provider_name}' connection was cut before the reply "
                f"finished{why} - this is usually a dropped network/gateway "
                f"connection, not a bad response. Try again.")
        detail = body or stderr or f"curl exited {returncode}"
        raise RuntimeError(f"'{provider_name}' request failed: {detail[:300]}")

    text = "".join(parts).strip()
    if text:
        return text
    raise LLMEmpty(f"'{provider_name}' returned an empty response")


def _openai_chat_curl(p: dict, prompt: str, timeout: int = 600,
                      max_tokens: int | None = None,
                      temperature: float = 1.0) -> str:
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
        "temperature": temperature,  # 1.0 by default for creative writing (regenerations must differ); judges pass a lower value
        "messages": [{"role": "user", "content": prompt}],
    }
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
        "Accept": "text/event-stream",
    }
    config_text = _curl_config(url, headers, payload)

    last_exc = None
    for attempt in range(2):
        try:
            proc = _spawn_curl(config_text, timeout)
            return _curl_read_stream(proc, p["name"], timeout)
        except RuntimeError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            last_exc = exc
            log.warning("LLM connection error (attempt %d): %s", attempt + 1, exc)
    raise RuntimeError(f"LLM connection failed after retry: {last_exc}")


def _openai_chat_urllib(p: dict, prompt: str, timeout: int = 600,
                        max_tokens: int | None = None,
                        temperature: float = 1.0) -> str:
    """The original pure-Python transport, kept as a fallback for machines
    with no curl on PATH. See _openai_chat_curl for why curl is preferred."""
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
        "temperature": temperature,  # 1.0 by default for creative writing (regenerations must differ); judges pass a lower value
        "messages": [{"role": "user", "content": prompt}],
    }
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
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
            started = time.monotonic()
            last_content = None
            deadline = started + timeout
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ctype = r.headers.get("Content-Type", "")
                if "event-stream" not in ctype:
                    data = json.loads(r.read())
                    return data["choices"][0]["message"]["content"].strip()
                for raw in r:
                    now = time.monotonic()
                    if now > deadline:
                        raise LLMStalled(
                            f"'{p['name']}' produced no complete answer in "
                            f"{timeout}s")
                    if last_content is None:
                        if now - started > LLM_FIRST_TOKEN_TIMEOUT:
                            raise LLMStalled(
                                f"'{p['name']}' sent no first token for "
                                f"{LLM_FIRST_TOKEN_TIMEOUT}s")
                    elif now - last_content > LLM_IDLE_TIMEOUT:
                        raise LLMStalled(
                            f"'{p['name']}' stalled mid-answer "
                            f"({LLM_IDLE_TIMEOUT}s without content)")
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
                    content = delta.get("content") or ""
                    if content:
                        parts.append(content)
                        last_content = time.monotonic()
                    elif delta.get("reasoning_content"):
                        # see the matching comment in _curl_read_stream
                        last_content = time.monotonic()
            text = "".join(parts).strip()
            if text:
                return text
            raise LLMEmpty(f"'{p['name']}' returned an empty response")
        except RuntimeError:
            raise
        except (urllib.error.URLError, http.client.HTTPException,
                ConnectionError, TimeoutError, OSError) as exc:
            last_exc = exc
            log.warning("LLM connection error (attempt %d): %s", attempt + 1, exc)
    raise RuntimeError(f"LLM connection failed after retry: {last_exc}")


def openai_chat(p: dict, prompt: str, timeout: int = 600,
                max_tokens: int | None = None,
                temperature: float = 1.0) -> str:
    """Send one chat completion and return the full text.

    `temperature` defaults to 1.0 (creative: script/style/shotlist writing,
    where a retry must genuinely differ) - a judge call passes a lower value
    so the same input grades the same way twice; either way it is the SAME
    knob, just set differently per caller, never per-provider.

    Prefers curl as the transport (see _openai_chat_curl's docstring for
    why); falls back to the pure-Python urllib path only when curl is not on
    PATH at all. Set WR_LLM_TRANSPORT=urllib to force the old path (e.g. to
    compare behavior while debugging a provider issue)."""
    forced = (os.environ.get("WR_LLM_TRANSPORT") or "").strip().lower()
    if forced == "urllib" or (not forced and not _curl_binary()):
        return _openai_chat_urllib(p, prompt, timeout=timeout,
                                   max_tokens=max_tokens, temperature=temperature)
    return _openai_chat_curl(p, prompt, timeout=timeout, max_tokens=max_tokens,
                             temperature=temperature)


# Wire protocols an LLM provider can speak, selected per provider with the
# `api` config key. Only OpenAI-compatible /chat/completions is implemented
# today; adding Claude's Messages API or Gemini's generateContent later is a
# new function plus one entry here - no caller changes.
CHAT_APIS = {"openai": openai_chat}


# Models that already refused a custom temperature (remembered for the life of
# the process): they get the default straight away instead of failing the first
# call of every request. Keyed by endpoint + model, never by provider name.
_NO_CUSTOM_TEMPERATURE: set[tuple[str, str]] = set()


def llm_generate(cfg, prompt: str, timeout: int = 1800,
                 provider: str | None = None,
                 max_tokens: int | None = None,
                 temperature: float = 1.0) -> str:
    p = _resolve_provider(cfg, provider)
    api = (p.get("api") or "openai").lower()
    fn = CHAT_APIS.get(api)
    temp_key = (str(p.get("base_url") or ""), str(p.get("model") or ""))
    if temp_key in _NO_CUSTOM_TEMPERATURE:
        temperature = 1.0
    if fn is None:
        raise RuntimeError(
            f"LLM provider '{p['name']}' uses api '{api}', which is not "
            f"implemented yet (available: {', '.join(sorted(CHAT_APIS))}). "
            f"Add an adapter to CHAT_APIS in studio.py.")

    def _retry_on_different_provider(exc: Exception) -> str:
        # A provider that goes quiet, answers empty, or (below) hard-fails
        # even after a temperature retry must not burn the whole timeout:
        # retry the SAME prompt once on a different ready provider,
        # preferring a different gateway.
        alt = _fallback_provider(cfg, p["name"])
        alt_fn = (CHAT_APIS.get((alt.get("api") or "openai").lower())
                  if alt else None)
        if not alt or alt_fn is None:
            raise exc
        log.warning("%s - retrying on '%s'", exc, alt["name"])
        try:
            return alt_fn(alt, prompt, timeout=timeout, max_tokens=max_tokens,
                          temperature=temperature)
        except Exception as exc2:  # noqa: BLE001 - always wrap, never swallow
            # Losing the ORIGINAL failure behind the fallback's own error
            # made a fine setup look like the fallback provider was the
            # (only) problem, when the real story is "both failed" - keep
            # both messages so whoever reads the log/warning knows which
            # provider to actually go fix.
            raise RuntimeError(
                f"'{p['name']}' failed ({exc}), and the fallback "
                f"'{alt['name']}' also failed ({exc2})") from exc2

    try:
        return fn(p, prompt, timeout=timeout, max_tokens=max_tokens,
                  temperature=temperature)
    except (LLMStalled, LLMEmpty) as exc:
        return _retry_on_different_provider(exc)
    except RuntimeError as exc:
        # Retry the SAME provider/model with one offending parameter backed
        # out, before ever swapping the model out for a different one - a
        # different model may simply behave differently (grade differently
        # as a judge, or write differently as a planner), which is exactly
        # the inconsistency a fixed provider is meant to avoid (see cc75462
        # for the temperature case this mirrors). Only if the model still
        # will not answer at all does it fall through to a different provider.
        retry_temperature = temperature
        retry_max_tokens = max_tokens
        backed_out = []
        if temperature != 1.0 and _rejects_temperature(exc):
            retry_temperature = 1.0
            backed_out.append("a custom temperature")
        if max_tokens and _rejects_max_tokens(exc):
            retry_max_tokens = None
            backed_out.append("an explicit max_tokens")
        if not backed_out:
            raise
        log.warning("%s - retrying '%s' without %s",
                   exc, p["name"], " or ".join(backed_out))
        try:
            out = fn(p, prompt, timeout=timeout, max_tokens=retry_max_tokens,
                     temperature=retry_temperature)
            if retry_temperature != temperature:
                _NO_CUSTOM_TEMPERATURE.add(temp_key)
            return out
        except (LLMStalled, LLMEmpty, RuntimeError) as exc2:
            return _retry_on_different_provider(exc2)


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


RATING_RUBRIC = [    ("hook", "Does the first 15 seconds earn attention without clickbait?"),
    ("originality", "Is it a genuine rewrite, not a reworded copy?"),
    ("accuracy", "Are the claims consistent with the SOURCE FACTS and not invented?"),
    ("structure", "Clear beats, logical order, no filler or repetition?"),
    ("pacing", "Does it hold attention to the end at a spoken pace?"),
    ("style_fit", "Does it obey the channel's style guide and tone?"),
    ("ending", "Does it land a payoff rather than trailing off?"),
]

# How much of the source transcript the judge sees. It MUST see it: the rubric
# asks whether claims are supported by the source, and without it the judge
# demanded external citations the transcript never had (accuracy 7-8, so no
# attempt could clear a 9.0 bar).
#
# This MUST match SOURCE_FACTS_MAX_CHARS, the cap the WRITER's own facts block
# uses (script_prompt) - the judge grades against the exact same `facts` text
# the writer was told to use. A smaller judge-side cap silently hid the back
# half of `facts` from the judge only: any real fact past this cutoff that the
# writer legitimately used got marked "absent from the SOURCE FACTS" on every
# single attempt, no matter how accurate the script actually was. A typical
# research-notes block runs 25-28k chars - well under the writer's 60k budget
# but more than double the old 12k judge-side cap, so this was not a rare edge
# case, it was hit on most real productions.
JUDGE_SOURCE_CHARS = SOURCE_FACTS_MAX_CHARS


def rating_prompt(title: str, genre: str, script: str, source: str,
                  style_guide: str, overlap: float,
                  extra_direction: str = "") -> str:
    rubric = "\n".join(f"- {name}: {desc}" for name, desc in RATING_RUBRIC)
    facts = (source or "").strip()[:JUDGE_SOURCE_CHARS]
    # The writer is told to follow the creator's additional direction, so the
    # judge must see it too. Without it the judge penalised a script for doing
    # exactly what the creator asked (e.g. matching the title's number of
    # points when the source's own count differed).
    direction = (extra_direction or "").strip()
    direction_block = ""
    if direction:
        direction_block = (
            f"CREATOR'S ADDITIONAL DIRECTION (the writer was told to follow "
            f"it): {direction}\n"
            f"Judge whether the script follows this direction, and do NOT "
            f"penalise it for doing what the direction asks, even where that "
            f"differs from the title, the SOURCE FACTS' own structure or "
            f"count. Invented facts are still penalised.\n\n")
    return (
        f"You are a ruthless YouTube script editor for the channel genre "
        f"'{genre}'. Score this script for the video \"{title}\".\n\n"
        f"Score each criterion 1-10:\n{rubric}\n\n"
        f"Measured 5-gram overlap with the source transcript: {overlap:.1%}. "
        f"Treat high overlap as an originality failure.\n\n"
        f"The SOURCE FACTS below are the ground truth: a claim is accurate when "
        f"it is consistent with them. Do NOT ask for external citations or "
        f"sources - the SOURCE FACTS are the source. Penalise only claims that "
        f"are absent from, or contradict, the SOURCE FACTS.\n\n"
        f"SOURCE FACTS:\n{facts or '(none)'}\n\n"
        f"CHANNEL STYLE GUIDE:\n{(style_guide or '(none)')[:3000]}\n\n"
        f"{direction_block}"
        f"SCRIPT:\n{script or ''}\n\n"
        f"Reply with ONLY a JSON object:\n"
        f'{{"score": <1-10 overall, one decimal>, '
        f'"criteria": {{"hook": <n>, "originality": <n>, "accuracy": <n>, '
        f'"structure": <n>, "pacing": <n>, "style_fit": <n>, "ending": <n>}}, '
        f'"feedback": ["<specific, actionable fix>", ...], '
        f'"weak_spans": ["<a passage that reads copied or weak>", ...]}}'
    )


def _parse_json_object(text: str) -> dict:
    """Extract a judge's JSON verdict from its reply. Uses the same
    balanced-brace, string-aware scan as the shotlist planner's
    _extract_json_object - NOT a naive `re.search(r"\{.*\}")`, which used to
    grab from the first "{" to the LAST "}" anywhere in the text. A judge that
    replies with valid JSON and then adds so much as one sentence of
    commentary (reasoning-style models do this constantly despite being told
    to reply with ONLY JSON) would have that greedy match swallow the whole
    tail; one stray brace anywhere in that commentary (e.g. "I nearly scored
    this higher {but the pacing dragged}") broke json.loads and threw the
    ENTIRE verdict away - including a perfectly good score - misreported as
    "the judge reply had no usable score" with no hint the score was ever
    there."""
    try:
        data, _tail = _extract_json_object(text or "")
    except RuntimeError:
        return {}
    return data if isinstance(data, dict) else {}


def rate_script(cfg, title: str, genre: str, script: str, source: str,
                style_guide: str, provider: str | None,
                temperature: float = 1.0, extra_direction: str = "") -> dict:
    """LLM-as-judge. Returns {score, criteria, feedback, weak_spans, error}.
    Never raises: a judge failure must not lose a usable draft.

    `temperature` defaults to 1.0 (matching every other LLM call) but the
    script stage passes a low value here - the writer must keep varying
    between attempts, the judge scoring it should not."""
    overlap = overlap_ratio(script, source)
    prompt = rating_prompt(title, genre, script, source, style_guide, overlap,
                           extra_direction=extra_direction)
    try:
        # 900 was a flat guess (see 9aafa69) sized for a bare score - it never
        # accounted for the 7-field criteria object plus feedback[] plus
        # weak_spans[] the prompt actually asks for. A verbose judge model
        # (e.g. gpt-5.6-luna) routinely overruns it, truncating the JSON
        # mid-object; _parse_json_object then can't close the object, the
        # score is thrown away with it, and every attempt reports "the judge
        # reply had no usable score" even when the judge clearly did score it
        # (confirmed: production 19 lost a real score on 6/6 attempts this
        # way). 2200 covers criteria + a handful of feedback/weak_spans
        # strings with headroom.
        raw = llm_generate(cfg, prompt, provider=provider, max_tokens=2200,
                           temperature=temperature)
        reply = _parse_json_object(raw)
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
    # A reply that parses to no score (prose, an empty answer, a different JSON
    # shape) used to surface only as a cryptic "rating n/a". Keep the reason so
    # the stage can say WHY the judge could not rate the draft.
    error = None
    if score is None:
        snippet = " ".join(str(raw or "").split())[:160]
        error = ("the judge reply had no usable score" +
                 (f": {snippet}" if snippet else " (empty reply)"))
    return {"score": score, "criteria": criteria, "feedback": feedback,
            "weak_spans": weak, "error": error}


def judge_provider(cfg, writer: str | None, preferred: str | None) -> str | None:
    """Which provider rates the script: an explicit choice, else a configured
    provider that is not the one that wrote it (self-scoring is biased), and
    preferably on a DIFFERENT gateway - a stall on api.b.ai hits every provider
    on api.b.ai, so a judge there would fail exactly when it is needed."""
    if preferred:
        return preferred
    provs = providers(cfg)
    writer_url = next((str(p.get("base_url") or "").rstrip("/")
                       for p in provs if p.get("name") == writer), "")
    ready = [p for p in provs
             if p.get("name") != writer and provider_ready(cfg, p.get("name"))]
    for p in ready:
        if str(p.get("base_url") or "").rstrip("/") != writer_url:
            return p["name"]
    return ready[0]["name"] if ready else writer


def style_prompt_from_scratch(title: str, genre: str, channel: str = "",
                              channel_notes: str = "",
                              extra_direction: str = "") -> str:
    """The writing style guide for a production with NO source transcript:
    written from the title, the channel and the creator's own direction
    instead of analysed from a reference narration."""
    about = ""
    if (channel or "").strip():
        about += f'\nThe channel is called "{channel.strip()}".'
    if (channel_notes or "").strip():
        about += f"\nAbout the channel: {channel_notes.strip()}"
    extra = (extra_direction or "").strip()
    if extra:
        extra = f"\nDIRECTION FROM THE CREATOR (follow it):\n{extra}\n"
    return f"""You are a writing coach for a {genre} YouTube channel.
There is no reference transcript. Design the narration WRITING STYLE for a
video titled "{title}".{about}
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
- Fit the genre and the channel; do not invent facts about the topic.
- Be concrete enough that another writer could follow the style without seeing any example.

Output ONLY the style guide markdown."""


def style_prompt(title: str, genre: str, source_text: str,
                 word_count: int | None = None,
                 extra_direction: str = "") -> str:
    text = (source_text or "").strip()
    if len(text) > SOURCE_FACTS_MAX_CHARS:
        log.warning("style: source transcript is %d chars - using the first "
                    "%d", len(text), SOURCE_FACTS_MAX_CHARS)
        text = text[:SOURCE_FACTS_MAX_CHARS] + " ..."
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
    # The writer is told to use ONLY these facts, so silently dropping half the
    # research shows up as accuracy failures. 60k chars (~15k tokens) fits every
    # configured model; anything beyond that is logged rather than hidden.
    if len(facts) > SOURCE_FACTS_MAX_CHARS:
        log.warning("script: source transcript is %d chars - using the first "
                    "%d (raise SOURCE_FACTS_MAX_CHARS if the model allows)",
                    len(facts), SOURCE_FACTS_MAX_CHARS)
        facts = facts[:SOURCE_FACTS_MAX_CHARS] + " ..."
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
        extra = (f"\nADDITIONAL DIRECTION FROM THE CREATOR (follow it; where it "
                 f"conflicts with the facts on structure or the number of "
                 f"points, the direction wins - but never invent facts to "
                 f"satisfy it):\n{extra}\n")
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
- The facts are NOTES, not prose: write entirely new sentences and do not follow
  their wording or order. No run of five or more consecutive words may match the
  facts or any source material.
- Choose your OWN story structure. The notes follow the order of an existing
  video, which is not a story order: decide what the viewer should learn first,
  where the tension or surprise lands, and how it ends, then arrange the facts
  to serve that - a different opening beat, a different sequence and a different
  closing from the order the notes list them in. The new order must still make
  sense: set a fact up before the one that depends on it, keep cause before
  effect, and never move a fact where it loses its meaning or its qualifiers.
- Never state a specific number, measurement, legal claim, or behavioral/causal
  detail unless it appears in the facts above. If a fact is approximate, qualified,
  or uncertain, keep the script exactly that approximate, qualified, or uncertain -
  do not sharpen it into something more specific, dramatic, or certain than the
  facts actually support. When in doubt, describe it more vaguely, not more vividly.
- Do not add a whole topic, comparison, example, or scenario that is not IN the
  facts, even one that sounds plausible or is true in general (a comparison to a
  different species, a historical period the facts do not cover, a hypothetical
  process like "how a gathering would have worked"). If you need more material to
  reach the target length, go deeper on a fact you already have - do not reach
  for a new, ungrounded topic.
- Match the facts' own confidence level. If a fact says "may have", "suggests",
  "is consistent with", or "one possible explanation", the script must carry that
  same hedge - never upgrade it into "proves", "shows that", "is why", or a flat
  statement of what happened or why. A dramatic TELLING of a fact is fine; a more
  CERTAIN version of the fact is not.
- State each idea ONCE, where it best belongs, then move on. Before writing a
  sentence, check whether the script has already made this point - if so, cut the
  new one rather than restating it "one more time" for emphasis. A script that
  circles back to summarize what it already explained is padding, not pacing.
- Hook the viewer in the first 15 seconds, following the style guide's hook pattern.
- About {target_words} words - this is a real target, not a ceiling. If you are
  running short, do NOT pad by repeating a point already made; instead go deeper
  on facts you have not fully unpacked yet, add another concrete example the
  facts support, or slow down and narrate a moment instead of summarizing it.
  Landing well under {target_words} words means you left facts unused, not that
  you wrote a tighter script. Conversational, second person, no stage directions,
  no scene labels.
- Write ONE ending. Land the final point once, in a single short closing passage,
  then go straight into the call to action - do not summarize the video, restate
  the thesis, or add a second "so what does this all mean" passage before it.
- End with a short call to action matching the style guide's CTA style.

Output ONLY the script text."""


# Notes are built PART BY PART. One call over a whole transcript used to hit a
# 2000-token cap and stop mid-word partway through the list (a 4,500-word, 11-rule
# transcript produced notes that ended at rule 7), so the writer and the judge
# never saw the later facts: the script could not cover them and the judge
# flagged them as invented. Each part is small enough that its notes fit the
# budget, and together the parts cover the whole transcript.
NOTES_MAX_TOKENS = 4000          # per part
NOTES_CHUNK_WORDS = 1200


def split_for_notes(text: str, max_words: int = NOTES_CHUNK_WORDS) -> list[str]:
    """Split a transcript into parts of about `max_words` words, cutting at a
    sentence end when one is near. Short texts stay in one part."""
    words = (text or "").split()
    if len(words) <= int(max_words * 1.25):
        return [(text or "").strip()]
    chunks: list[list[str]] = []
    start = 0
    while start < len(words):
        end = min(len(words), start + max_words)
        if end < len(words):
            floor = start + int(max_words * 0.7)
            for i in range(end - 1, floor - 1, -1):
                if words[i].endswith((".", "!", "?")):
                    end = i + 1
                    break
        chunks.append(words[start:end])
        start = end
    if len(chunks) > 1 and len(chunks[-1]) < int(max_words * 0.25):
        chunks[-2].extend(chunks.pop())
    return [" ".join(c) for c in chunks]


def notes_prompt(title: str, genre: str, source_text: str,
                 part: tuple[int, int] | None = None) -> str:
    """Turn a source transcript into neutral research NOTES for the writer.

    Feeding the transcript itself as the 'facts' made the writer echo it: the
    first live attempt measured 93.5% 5-gram overlap with the source, and the
    copycat gate rejects anything over 20%. Notes in the model's own words break
    that echo before the script is written."""
    text = (source_text or "").strip()
    if len(text) > SOURCE_FACTS_MAX_CHARS:
        text = text[:SOURCE_FACTS_MAX_CHARS] + " ..."
    part_note = ""
    if part and part[1] > 1:
        part_note = (f"\nThis is part {part[0]} of {part[1]} of the transcript. "
                     f"Extract the facts from THIS part only - the other parts "
                     f"are handled separately.\n")
    return f"""You are a researcher for a {genre} YouTube channel. Below is a transcript of an existing video titled "{title}".
{part_note}
Extract the FACTS it contains as a terse bulleted list - every claim, number, name, place and example, one per line.

Rules:
- Write in your own words. Copy no sentence, phrase or clause from the transcript: no run of five or more consecutive words may appear in your notes.
- Facts only: no introduction, no commentary, no headings, no conclusion.
- Keep every number and proper noun exactly as written.
- Group related facts under a short label line when that helps.

TRANSCRIPT:
{text}

Output only the bullet list."""


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


def load_manifest_brief(cfg, profile=None, presentation: str = "") -> str:
    """The manifest-authoring brief: the master planning prompt that turns a
    narration SRT into shotlist.json, rendered for ONE
    channel.

    The brief is a template (brief_template.md, shipped with WhisperRadar):
    `profile` (a briefs.MotionProfile, or its key) fills the motion/pacing
    slots and `presentation` (free text) the who-is-on-screen slot. With
    neither it renders to exactly the original brief. Read from disk on every
    use, so edits to the template take effect immediately. studio.manifest_brief
    in config.yaml points at a custom template instead (a plain brief with no
    slots still works for the standard profile; a leading how-to header above
    the first `---` line is skipped)."""
    if not isinstance(profile, briefs.MotionProfile):
        profile = briefs.get_profile(profile)
    path = None
    if cfg.studio_manifest_brief:
        p = Path(cfg.studio_manifest_brief)
        path = p if p.is_absolute() else cfg.base_dir / p
        if not path.exists():
            raise RuntimeError(
                f"studio.manifest_brief points at {path}, which does not "
                "exist - fix or clear it in config.yaml")
    text = briefs.read_template(path)
    if path is not None and "\n---\n" in text:
        # a hand-made brief may keep ImgToVideo's how-to header: skip it
        text = text.split("\n---\n", 1)[1]
    return briefs.render_brief(text.strip(), profile, presentation)


def _repair_smart_quotes(raw: str) -> str:
    """Claude's web page shows a reply's JSON as ordinary text with
    typographic quotes (“like this”) - also around quotes INSIDE a string.
    Turn the double ones back into straight quotes and escape the ones that
    are part of the text: a quote only closes a string when what follows is
    `:`, `}`, `]`, the end, or a comma that leads on to a new string/object/
    list/number. Single curly quotes become plain apostrophes."""
    t = (raw.replace("\u201c", '"').replace("\u201d", '"')
         .replace("\u2018", "'").replace("\u2019", "'"))
    out, in_str, esc = [], False, False
    for i, ch in enumerate(t):
        if in_str and esc:
            esc = False
            out.append(ch)
            continue
        if in_str and ch == "\\":
            esc = True
            out.append(ch)
            continue
        if ch != '"':
            out.append(ch)
            continue
        if not in_str:
            in_str = True
            out.append(ch)
            continue
        j = i + 1
        while j < len(t) and t[j] in " \t\r\n":
            j += 1
        nxt = t[j] if j < len(t) else ""
        closes = nxt in ("", ":", "}", "]")
        if nxt == ",":
            k = j + 1
            while k < len(t) and t[k] in " \t\r\n":
                k += 1
            closes = k < len(t) and t[k] in '"{[-0123456789'
        if closes:
            in_str = False
            out.append(ch)
        else:
            out.append('\\"')
    return "".join(out)


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
                    # strict=False: the planner sometimes emits a RAW control
                    # character (a literal newline) inside a prompt string, which
                    # json rejects by default and which would fail the whole plan
                    data = json.loads(text[start:i + 1], strict=False)
                except ValueError as exc:
                    # A long plan occasionally slips a trailing comma (e.g. a
                    # dropped "refs" field leaves `"prompt": "...", }`). Valid
                    # JSON is never touched - this only runs after a failure.
                    cleaned = re.sub(r",(\s*[}\]])", r"\1", text[start:i + 1])
                    try:
                        data = json.loads(cleaned, strict=False)
                    except ValueError:
                        try:                  # Claude's page: curly quotes
                            data = json.loads(_repair_smart_quotes(
                                text[start:i + 1]), strict=False)
                        except ValueError:
                            raise RuntimeError(
                                f"shotlist JSON is invalid: {exc}")
                return data, text[i + 1:]
    raise RuntimeError("LLM returned an incomplete JSON object")


def parse_shotlist_output(text: str) -> tuple[dict, str]:
    """Parse the LLM's shotlist reply (manifest-authoring brief): the
    shotlist JSON. Anything written after it (older briefs, or a custom brief,
    still ask for an IMAGE BATCH SHEET) is returned as the tail and is not
    needed - batch_sheet_text() builds the sheet from the JSON.
    Returns (shotlist_data, tail_text)."""
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


def batch_sheet_text(data: dict) -> str:
    """The readable image batch sheet, built from shotlist.json (no LLM, no
    stored copy that can go stale): master prompt, canvas per motion code,
    then one line per image grouped by main beat (S01, S02, ...)."""
    images = [i for i in (data.get("images") or [])
              if isinstance(i, dict) and i.get("file")]

    def code(fname: str) -> str:
        return Path(str(fname)).stem.rsplit("_", 1)[-1].upper()

    used = [c for c in briefs.MOTION_CODE_INFO
            if any(code(i["file"]) == c for i in images)]
    lines = ["=== DOCUMENT 1: IMAGE BATCH SHEET ===", "",
             "=== MASTER PROMPT ===", (data.get("style") or "").strip(), "",
             "=== CANVAS SPEC (by motion code in the filename) ===",
             briefs._canvas_spec(used) or "ST/ZI/ZO: 2304x1296",
             "Larger canvases are fine if the aspect and overscan direction "
             "are preserved. Always 8-bit RGB or RGBA with a solid (white) "
             "background - no transparency."]
    scene = None
    for img in images:
        m = re.match(r"^(S\d+)_", str(img["file"]))
        beat = m.group(1) if m else "OTHER"
        if beat != scene:
            scene = beat
            lines += ["", f"=== {beat} ==="]
        info = briefs.MOTION_CODE_INFO.get(code(img["file"]))
        size = f" [{info[1]}]" if info else ""
        refs = [str(r) for r in (img.get("refs") or []) if str(r).strip()]
        tail = f" \u00b7 refs: {', '.join(refs)}" if refs else ""
        lines.append(f"{img['file']}{size} \u2014 "
                     f"{(img.get('prompt') or '').strip()}{tail}")
    return "\n".join(lines) + "\n"


# A long plan's reply can be cut off mid-JSON (the brief's Section 11 says to
# continue in the next message until every cue is covered). We ask for the rest
# and keep appending instead of throwing the plan away.
SHOTLIST_CONTINUE_ROUNDS = 5


def continuation_prompt(base_prompt: str, partial: str) -> str:
    """Ask for the rest of a reply that was cut off mid-JSON.

    Only the TAIL of what was written is re-sent - the base prompt already
    carries the brief and the narration - so the request stays in context even
    when the finished plan is 100+ images."""
    tail = (partial or "")[-3000:]
    return (base_prompt.rstrip()
            + "\n\n---\n\nYOUR PREVIOUS OUTPUT WAS CUT OFF. It ended with:\n"
            + tail
            + "\n\nContinue from EXACTLY where it stopped: the same JSON "
            "document, the same entry, no repetition of anything already "
            "written, no commentary, no markdown fences, no second \"style\" "
            "field. Output only the remaining content, close every bracket "
            "cleanly, and finish every cue through the last one. Write "
            "nothing after the JSON.")


def shotlist_tail_gap(data: dict, cue_count: int) -> int | None:
    """The last covered cue, when a plan cleanly closes its JSON (unlike the
    mid-JSON cutoff continuation_prompt handles) but simply stops before the
    end of the narration - e.g. 73 shots covering cues 1-221 of 484, with no
    error, just nothing planned for the rest. Every attempt re-planning from
    scratch tends to truncate at roughly the same point, so this lets the
    caller ask for just the missing tail instead.

    Returns None when the plan already reaches cue_count, or when it has some
    OTHER structural problem (an internal gap, an overlap, out-of-order
    shots, a malformed 'cues' range) that a tail continuation can't fix -
    those still need the full review-and-feedback re-plan."""
    shots = [s for s in (data.get("shots") or []) if isinstance(s, dict)]
    if not shots:
        return None
    ranges = []
    for s in shots:
        rng = cue_range(s.get("cues"))
        if rng is None:
            return None
        ranges.append(rng)
    if ranges[0][0] != 1:
        return None
    covered_end = ranges[0][1]
    for start, end in ranges[1:]:
        if start != covered_end + 1:
            return None  # a real gap, overlap, or out-of-order shot
        covered_end = end
    return covered_end if covered_end < cue_count else None


def shotlist_tail_continuation_prompt(base_prompt: str, from_cue: int,
                                      cue_count: int) -> str:
    """Ask for just the missing tail of a plan shotlist_tail_gap flagged -
    a JSON FRAGMENT (only 'shots' and 'images', continuing the existing
    numbering) to be merged onto the plan with merge_shotlist_continuation,
    not a full replacement. Distinct from continuation_prompt, which resumes
    a reply that was cut off mid-document rather than one that finished
    cleanly but stopped short."""
    return (base_prompt.rstrip()
            + "\n\n---\n\nYour previous reply was a complete, validly-closed "
            f"JSON document, but it only plans cues 1-{from_cue} of this "
            f"narration's {cue_count} cues - the rest was left unplanned. "
            f"Continue the SAME shotlist: plan cues {from_cue + 1}-"
            f"{cue_count} (every one, in order, no gaps, no repeats of a cue "
            "already covered above), continuing the same asset numbering. "
            "Output ONLY a JSON object with exactly two keys, \"shots\" and "
            "\"images\", containing just these NEW entries - no other keys, "
            "no markdown fences, no commentary, no repetition of anything "
            "already planned.")


def merge_shotlist_continuation(data: dict, addition: dict) -> dict:
    """Append a tail continuation's new shots/images onto an already-valid
    plan (see shotlist_tail_gap / shotlist_tail_continuation_prompt)."""
    merged = dict(data)
    merged["shots"] = list(data.get("shots") or []) + list(addition.get("shots") or [])
    merged["images"] = list(data.get("images") or []) + list(addition.get("images") or [])
    return merged


def shotlist_prompt(brief_text: str, narration: str, style_guide: str = "",
                    extra_direction: str = "", bible: str = "",
                    feedback: str = "",
                    supplied_refs: list[str] | None = None,
                    pacing_note: str = "", allow_refs: bool = True) -> str:
    """Assemble the manifest-authoring brief with its inputs: the narration
    (cue-delimited, timestamp-free - see compact_srt), the channel visual
    style, the optional character / reference bible, and the creator's
    per-stage direction."""
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
    refs_block = ""
    if not allow_refs:
        refs_block = (
            "\n\nREFERENCES ARE DISABLED FOR THIS CHANNEL (strict rule): do "
            "NOT declare a top-level \"refs\" registry, do NOT write any "
            "\"refPrompts\" entries, and do NOT put a \"refs\" array on any "
            "image. With no reference image to carry anything, EVERY image "
            "prompt must be fully self-contained and state all of: the "
            "subject(s) and what they are DOING, the specific location, "
            "every prop the narration mentions, the spatial layout (who is "
            "where, foreground/background), and where the scene's light "
            "comes from. Recurring characters/objects must be re-described "
            "in full in each prompt that shows them (look, wardrobe, the "
            "exact prop) so they stay consistent without a reference. A "
            "terse prompt that omits what a cue requires will be marked weak "
            "and the plan rejected. Any shotlist that references a name will "
            "be rejected and re-planned.")
    elif supplied_refs:
        listing = "\n".join(f"- refs/{name}" for name in supplied_refs)
        refs_block = (
            f"\n\nSUPPLIED REFERENCE FILES ALREADY ON DISK (read this BEFORE "
            f"planning - in this production's refs folder; these are the ONLY "
            f"reference files that exist):\n{listing}\n"
            f"These files are used AS-IS. To attach one, put its name "
            f"(the file name without extension) in the shot's refs and "
            f"declare it in the refs registry with EXACTLY the path shown "
            f"above - never a different spelling, never a subfolder. Do NOT "
            f"write a refPrompts entry for a supplied file: it is never "
            f"generated. Attach a supplied character to every shot where it "
            f"appears on screen. Every other reference you need (other "
            f"characters, environments, props) does NOT exist yet and is "
            f"generated on the fly before rendering: give each a CH_/BG_/OBJ_ "
            f"name and a detailed refPrompts entry.")
    pacing_block = ""
    if (pacing_note or "").strip():
        pacing_block = (
            f"\n\nPACING MATH FOR THIS CHANNEL - OVERRIDES SECTIONS 1 AND 2 "
            f"ABOVE (computed from the narration itself, follow it exactly): "
            f"the brief above says there is no duration cap and a 15s+ hold "
            f"is fine with motion - THAT IS THE GENERAL RULE FOR CHANNELS "
            f"WITH NO LIMIT, IT DOES NOT APPLY HERE. This channel enforces a "
            f"hard maximum hold, checked by the review before anything "
            f"renders; a shot over it is a hard failure regardless of "
            f"whether it carries motion:\n{pacing_note.strip()}")
    return f"""{brief_text.strip()}

---

INPUT 1 - NARRATION (one line per cue: "N: text (Ns)"; the cue numbers are what the shot `cues` ranges refer to and the (Ns) is how long the cue runs; timestamps are intentionally omitted):
{narration.strip()}

{style_block}{bible_block}{extra}{fix_block}{refs_block}{pacing_block}"""


def shotlist_patch_prompt(weak: list[dict], style_guide: str = "",
                          bible: str = "") -> str:
    """A narrow follow-up prompt: rewrite ONLY the image prompts flagged weak
    by the last review - nothing else about the plan.

    Used instead of a full shotlist_prompt() re-plan once a shotlist already
    has zero structural faults and only falls short on prompt detail. A full
    re-plan regenerates every prompt from scratch, and in practice does not
    reliably honor "keep everything that already passed" - that is what
    produced non-monotonic swings across retries of the same shotlist (e.g.
    85% detailed, then 63% on the very next attempt). A patch call can only
    ever touch the assets it is given, so the rest of the shotlist is safe
    by construction rather than by the model's cooperation."""
    style = (style_guide or "").strip()
    style_block = (f"\n\nCHANNEL VISUAL STYLE:\n{style}" if style else "")
    bible_block = ""
    if (bible or "").strip():
        bible_block = (f"\n\nCHARACTER / REFERENCE BIBLE (preserve these "
                       f"characters, environments and objects exactly as "
                       f"described):\n{bible.strip()}")
    lines = []
    for w in weak:
        missing = ("; missing: " + ", ".join(w["missing"])
                   if w.get("missing") else "")
        lines.append(f"- {w['asset']}{missing}"
                     + (f" - {w['reason']}" if w.get("reason") else "")
                     + (f" | REVEAL SHOT with {len(w['reveal'])} items in "
                        + ("a 2x2 grid (reading order: top-left, top-right, "
                           "bottom-left, bottom-right), one equal quadrant "
                           "each: keep that layout, name every item in "
                           "narration order, nothing may cross the centre "
                           "lines" if w.get("reveal_layout") == "grid" else
                           "a left-to-right row, one equal slice each: keep "
                           "that layout, name every item in narration order, "
                           "nothing may cross a slice line")
                        if w.get("reveal") else "")
                     + (f" | BUILD-UP IMAGE {w['reveal_item'][0]} of "
                        f"{w['reveal_item'][1]}: it is centre-cropped into "
                        f"a slot, so keep ONE subject centered with generous "
                        f"empty space on both sides"
                        if w.get("reveal_item") else "")
                     + (f" | cue says: {w['narration']}"
                        if w.get("narration") else "")
                     + (f"\n  CURRENT PROMPT: {w['prompt']}"
                        if w.get("prompt") else ""))
    has_current = any(w.get("prompt") for w in weak)
    keep_rule = (
        " Where a CURRENT PROMPT is shown, START FROM IT: keep its subjects "
        "(the same animals, people and objects), its composition and its "
        "lighting block, and change only what the problem named for that "
        "asset requires. Never swap a subject for a different one."
        if has_current else "")
    return (
        "You are revising a small number of image prompts from an existing, "
        "otherwise-approved shotlist for a narrated video. Do NOT change "
        "anything about the plan itself (shot count, order, cue coverage, "
        "hold lengths, references, other prompts) - rewrite ONLY the PROMPT "
        "TEXT for the exact assets listed below, so each one states every "
        "element its narration cue requires: who is in frame, what they are "
        "doing, where they are, the objects or props involved, and the "
        "specific information the cue conveys." + keep_rule
        + style_block + bible_block +
        "\n\nAssets to rewrite:\n" + "\n".join(lines) +
        "\n\nReply with ONLY a JSON object of the form "
        '{"patches": {"<asset filename>": "<new prompt text>", ...}} '
        "with exactly one entry per asset listed above, nothing else."
    )


def parse_shotlist_patch(text: str) -> dict[str, str]:
    """Parse a shotlist_patch_prompt() reply into {asset: new_prompt}. Never
    raises - a malformed or empty reply just yields no patches, and the
    caller falls back to a full re-plan."""
    data = _parse_json_object(text)
    patches = data.get("patches") if isinstance(data, dict) else None
    if not isinstance(patches, dict):
        return {}
    return {str(k): str(v) for k, v in patches.items()
            if isinstance(v, str) and v.strip()}


def with_current_prompts(weak: list[dict], data: dict) -> list[dict]:
    """Copies of the weak entries carrying each asset's CURRENT prompt text,
    so the patch writer starts from it instead of inventing the scene from
    the narration alone (a writer that never sees the old prompt swapped a
    cheetah for a dog and for birds of prey when asked to remove a name)."""
    by = {i.get("file"): i.get("prompt") for i in (data.get("images") or [])
          if isinstance(i, dict) and i.get("file")}
    out = []
    for w in weak:
        w2 = dict(w)
        cur = by.get(w.get("asset"))
        if cur and not w2.get("prompt"):
            w2["prompt"] = str(cur)
        out.append(w2)
    return out


def check_shotlist_patch(patches: dict[str, str],
                         asked: list[str]) -> tuple[dict[str, str], list[str], list[str]]:
    """(usable patches, unknown keys, missing assets). A patch keyed by an
    asset that was not asked for (a model mislabelling an entry) is dropped
    rather than silently ignored later, and an asked-for asset with no patch
    is reported so the caller can ask for just that one again."""
    wanted = [str(a) for a in asked]
    good = {k: v for k, v in patches.items() if k in wanted}
    unknown = [k for k in patches if k not in wanted]
    missing = [a for a in wanted if a not in good]
    return good, unknown, missing


def apply_shotlist_patch(data: dict, patches: dict[str, str]) -> dict:
    """Return a COPY of a shotlist plan with only the given assets' prompts
    replaced - every other shot, image and registry entry is untouched."""
    if not patches:
        return data
    new_data = json.loads(json.dumps(data))  # cheap deep copy, no aliasing
    images = new_data.get("images")
    if isinstance(images, list):
        for item in images:
            if isinstance(item, dict) and item.get("file") in patches:
                item["prompt"] = patches[item["file"]]
    return new_data


# ---------------------------------------------- fault-targeted shotlist fix ---
# The same loop an external LLM chat runs by hand: a checker reports exactly
# what is wrong, the writer fixes ONLY that, the checker looks again. A full
# re-plan regenerates everything and does not reliably keep what already
# passed; a fix call that returns just the entries it changed cannot lose the
# rest of the plan, and its reply stays small (long plans were the ones that
# kept getting cut off).

FIX_MAX_RELEVANT_SHOTS = 40
FIX_CONTEXT_CUES = 2


def shotlist_fix_scope(data: dict, faults: list[str], weak: list[dict],
                       cue_count: int) -> tuple[list[dict], set[int]]:
    """(shots to show the fixer in full, cue numbers whose narration to show):
    every shot named in a fault or flagged weak, plus the shots covering any
    cue number a fault mentions. Capped so the prompt stays bounded."""
    shots = [x for x in (data.get("shots") or []) if isinstance(x, dict)]
    text = "\n".join(str(f) for f in faults)
    named = {str(w.get("asset")) for w in weak if w.get("asset")}
    named |= {str(x.get("asset")) for x in shots
              if x.get("asset") and str(x.get("asset")) in text}
    # cue numbers only where a fault talks about cues ("cue 40", "cues 5-8",
    # "have no shot: 6, 7", "(5-8 then 7-9)") - not counts like "12 shots"
    cues_mentioned = {
        int(n) for seg in re.findall(
            r"(?:cues?|no shot:|then|\()\s*([\d,\s\-\u2013\u2026]+)", text)
        for n in re.findall(r"\d+", seg) if 1 <= int(n) <= cue_count}
    picked: list[dict] = []
    seen: set[int] = set()
    for i, x in enumerate(shots):
        rng = cue_range(x.get("cues"))
        hit = str(x.get("asset")) in named or (
            rng and any(rng[0] <= c <= rng[1] for c in cues_mentioned))
        if hit and i not in seen:
            picked.append(x)
            seen.add(i)
        if len(picked) >= FIX_MAX_RELEVANT_SHOTS:
            break
    show: set[int] = set(cues_mentioned)
    for x in picked:
        rng = cue_range(x.get("cues"))
        if rng:
            lo = max(1, rng[0] - FIX_CONTEXT_CUES)
            hi = min(cue_count, rng[1] + FIX_CONTEXT_CUES)
            show.update(range(lo, hi + 1))
    return picked, show


def shotlist_fix_prompt(data: dict, faults: list[str], weak: list[dict],
                        cue_text: dict[int, str], cue_count: int,
                        max_hold_seconds: float, style_guide: str = "",
                        bible: str = "", allow_refs: bool = True) -> str:
    """The writer's fix request: the checker's faults (and the weak prompts)
    plus just the part of the plan they concern. The reply is a JSON DELTA -
    see apply_shotlist_fix - never the whole shotlist."""
    shots = [x for x in (data.get("shots") or []) if isinstance(x, dict)]
    images = {str(i.get("file")): i for i in (data.get("images") or [])
              if isinstance(i, dict) and i.get("file")}
    picked, show = shotlist_fix_scope(data, faults, weak, cue_count)
    style = (style_guide or "").strip()
    style_block = f"\n\nCHANNEL VISUAL STYLE:\n{style}" if style else ""
    bible_block = (f"\n\nCHARACTER / REFERENCE BIBLE (preserve these exactly "
                   f"as described):\n{bible.strip()}"
                   if (bible or "").strip() else "")
    overview = "\n".join(
        f"{x.get('cues')} | {x.get('asset')} | {x.get('motion') or '-'}"
        f" | {x.get('scene') or '-'}" for x in shots)
    entries = json.dumps(
        {"shots": picked,
         "images": [images[str(x.get("asset"))] for x in picked
                    if str(x.get("asset")) in images]},
        ensure_ascii=False, indent=1)
    narration = "\n".join(f"{c}: {cue_text.get(c, '')}" for c in sorted(show)
                           if c in cue_text)
    fault_lines = "\n".join(f"- {f}" for f in faults) or "(none)"
    weak_lines = []
    for w in weak:
        missing = ("; missing: " + ", ".join(w["missing"])
                   if w.get("missing") else "")
        weak_lines.append(f"- {w.get('asset')}{missing}"
                          + (f" - {w['reason']}" if w.get("reason") else "")
                          + (f" | cue says: {w['narration']}"
                             if w.get("narration") else ""))
    refs_rule = ("" if allow_refs else
                 " References are DISABLED for this channel: no \"refs\" "
                 "registry, no \"refPrompts\", no per-image \"refs\"; every "
                 "prompt is fully self-contained.")
    return (
        "You are fixing an existing shotlist for a narrated video. A checker "
        "reviewed it and found the problems listed below. Fix ONLY those "
        "problems. Everything not listed already passed: do not change "
        "other shots, other prompts, the order or the numbering."
        + style_block + bible_block +
        f"\n\nRULES: no shot may hold longer than {max_hold_seconds:g}s "
        "(a shot's hold is the sum of its cues' durations); every narration "
        "cue 1-" + str(cue_count) + " is covered by exactly one shot, in "
        "order, no gaps, no overlaps; every shot's asset has an entry in "
        "images; image file names keep the existing convention and a new "
        "name continues the numbering; the motion code in a file name "
        "matches the shot's motion; every image prompt states who or what "
        "is in frame, what they are doing, where, the props involved and "
        "the information the cue conveys." + refs_rule +
        "\n\nFAULTS TO FIX (must all be gone afterwards):\n" + fault_lines +
        ("\n\nWEAK PROMPTS TO REWRITE:\n" + "\n".join(weak_lines)
         if weak_lines else "") +
        "\n\nWHOLE PLAN, one line per shot (cues | asset | motion | scene):\n"
        + overview +
        "\n\nFULL ENTRIES OF THE SHOTS CONCERNED:\n" + entries +
        "\n\nNARRATION AROUND THEM (cue: text):\n" + narration +
        "\n\nReply with ONLY a JSON object with the keys \"shots\" and "
        "\"images\", containing just the entries you changed or added, no "
        "commentary, no markdown fences:\n"
        "- \"shots\": each entry REPLACES every existing shot whose cues "
        "overlap its \"cues\" (so to split a long shot, return the new "
        "shots covering its whole range; to fill a gap, return a shot for "
        "the missing cues). Give each the full shot object (cues, asset, "
        "motion, scene, ...).\n"
        "- \"images\": the entry (file, prompt, refs if used) for every "
        "new asset, and for every existing asset whose prompt you rewrite "
        "(same file name replaces it).\n"
        "An image that no shot uses any more is dropped automatically. "
        "Return an empty list for a key you do not need.")


def parse_shotlist_fix(text: str) -> dict:
    """Parse a fix reply. Raises RuntimeError (message contains 'incomplete'
    for a reply cut off mid-JSON, so the caller can ask it to continue)."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    data, _tail = _extract_json_object(text)
    shots = data.get("shots") or []
    images = data.get("images") or []
    if not isinstance(shots, list) or not isinstance(images, list):
        raise RuntimeError("fix reply has no shots/images lists")
    return {"shots": [x for x in shots if isinstance(x, dict)],
            "images": [x for x in images if isinstance(x, dict)]}


def apply_shotlist_fix(data: dict, fix: dict) -> tuple[dict, set[str], str]:
    """Return (a COPY of the plan with the fix applied, the assets that
    changed, a one-line summary). Reply shots replace every existing shot
    they overlap; reply images replace by file name or are appended; an image
    only the replaced shots used is dropped. A reply with nothing usable
    returns the plan unchanged and no changed assets."""
    new = json.loads(json.dumps(data))
    old_shots = [x for x in (new.get("shots") or []) if isinstance(x, dict)]
    reply = [x for x in fix.get("shots") or []
             if x.get("asset") and cue_range(x.get("cues"))]
    reply_images = [x for x in fix.get("images") or []
                    if x.get("file") and str(x.get("prompt") or "").strip()]
    if not reply and not reply_images:
        return data, set(), "the fix changed nothing"
    covered: list[tuple[int, int]] = [cue_range(x["cues"]) for x in reply]

    def overlaps(shot: dict) -> bool:
        rng = cue_range(shot.get("cues"))
        return bool(rng) and any(rng[0] <= b and a <= rng[1]
                                 for a, b in covered)

    removed = [x for x in old_shots if overlaps(x)]
    kept = [x for x in old_shots if not overlaps(x)]
    shots = kept + reply
    shots.sort(key=lambda x: (cue_range(x.get("cues")) or (10 ** 9, 0))[0])
    new["shots"] = shots

    images = [x for x in (new.get("images") or []) if isinstance(x, dict)]
    by_file = {str(x.get("file")): i for i, x in enumerate(images)}
    for item in reply_images:
        f = str(item["file"])
        if f in by_file:
            images[by_file[f]] = {**images[by_file[f]], **item}
        else:
            images.append(item)
            by_file[f] = len(images) - 1
    used = {str(x.get("asset")) for x in shots}
    gone = {str(x.get("asset")) for x in removed} - used
    images = [x for x in images if str(x.get("file")) not in gone]
    new["images"] = images
    changed = ({str(x["asset"]) for x in reply}
               | {str(x["file"]) for x in reply_images})
    summary = (f"replaced {len(removed)} shot(s) with {len(reply)}, "
               f"wrote {len(reply_images)} image prompt(s)"
               + (f", dropped {len(gone)} unused image(s)" if gone else ""))
    return new, changed, summary


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


def compact_srt(srt_text: str) -> str:
    """The narration as `N: text (Ns)` lines - cue numbers kept, timestamps
    dropped, per-cue DURATION kept.

    The planning brief tells the model it never writes timings ("the cue ranges
    carry them"), so the per-cue timestamp block is pure prompt overhead - on a
    long narration it is a large share of the SRT. The `subtitles.srt` FILE is
    untouched: the pacing gate and ImgToVideo's assembler still read its real
    timestamps. Cue NUMBERS are kept because the shotlist's cue ranges and the
    assembler index by them. The per-cue duration is kept because a planner
    that cannot see how long each cue runs cannot pace its shots (a 16-minute
    narration once came back as 34 shots) - and the pacing gate needs seconds,
    not indexes, to be predictable. Falls back to the raw text when nothing
    parses, so a malformed SRT can never blank the narration."""
    cues = parse_srt_cues(srt_text)
    if not cues:
        return (srt_text or "").strip()
    lines = []
    for c in cues:
        dur = int(_srt_seconds(c["end"]) - _srt_seconds(c["start"]) + 0.5)
        lines.append(f"{c['index']}: {c['text']} ({max(1, dur)}s)")
    return "\n".join(lines)


# Measured on two real, finished productions (bytes of combined shotlist.json
# + batch_sheet.txt output, divided by shot count): ~513 b/shot and ~844
# b/shot respectively, i.e. roughly 210-260 tokens/shot at a conservative
# 3.3 chars/token. SHOTLIST_TOKENS_PER_SHOT keeps headroom above the higher
# figure on purpose - this is a ceiling to avoid a cut-off reply, not a
# target, and overshooting costs nothing (providers bill actual completion
# tokens, not the requested max_tokens).
SHOTLIST_TOKENS_PER_SHOT = 300
SHOTLIST_TOKENS_OVERHEAD = 2000
# A shot holds ~6s on average on both measured productions (6.02s on a
# 178-shot/1072s plan) - between the brief's ST-only 5s ceiling and its
# general ~12-15s one, which is exactly what "most shots span several cues,
# ST only for the shortest ones" should produce. Used only to ESTIMATE
# likely shot count for sizing the token budget; it has no bearing on the
# actual plan the model writes.
SHOTLIST_ASSUMED_AVG_HOLD = 6.0
# An outer safety bound, not a design target: the largest narration seen so
# far (812 cues, 1072s) needs ~37-45k tokens of actual output (measured
# directly), so 64000 leaves real headroom above that without guessing at
# what any given provider's hard ceiling is. If a provider rejects a value
# this large outright, llm_generate's _rejects_max_tokens fallback retries
# the same call with no cap at all (the old behavior: truncate and
# continue) rather than failing the attempt.
SHOTLIST_TOKENS_CEILING = 64000


def shotlist_max_tokens(total_narration_seconds: float, cue_count: int,
                        max_hold_seconds: float) -> int:
    """A generous output-token budget for the shotlist generation call, sized
    from the narration itself so a normal-sized plan finishes in one LLM
    reply instead of needing SHOTLIST_CONTINUE_ROUNDS extra round trips to
    finish a reply that was cut off mid-JSON. Each continuation round is a
    full sequential LLM call (it depends on the previous one, so it can't be
    parallelized) - pure added latency on every attempt that needs one.

    Mirrors script_max_tokens's reasoning (no budget = ride the provider's
    own default, which is what was silently truncating long plans) but
    shots are not a 1:1 function of narration length the way script words
    are, so this estimates an expected shot COUNT first: total narration
    duration divided by a typical hold (SHOTLIST_ASSUMED_AVG_HOLD), with 40%
    headroom for a more fragmented-than-average plan, never below the hard
    pacing floor (every shot at the channel's max hold), and never above one
    shot per cue (shotlist_pacing's FRAGMENTATION_SHARE cannot exceed that
    by definition). This is an estimate for SIZING THE BUDGET ONLY - it does
    not influence, cap, or validate how many shots the plan actually ends up
    with; shotlist_pacing still does that, independently, after the fact."""
    min_shots = (int(total_narration_seconds / max_hold_seconds)
                + (1 if total_narration_seconds % max_hold_seconds else 0)
                if max_hold_seconds > 0 and total_narration_seconds > 0
                else 0)
    expected = (total_narration_seconds / SHOTLIST_ASSUMED_AVG_HOLD
               if total_narration_seconds > 0 else 0)
    shots_estimate = max(min_shots, expected) * 1.4
    if cue_count:
        shots_estimate = min(shots_estimate, cue_count)
    tokens = int(shots_estimate * SHOTLIST_TOKENS_PER_SHOT) + SHOTLIST_TOKENS_OVERHEAD
    return max(SHOTLIST_TOKENS_OVERHEAD, min(tokens, SHOTLIST_TOKENS_CEILING))


def cue_range(text: str) -> tuple[int, int] | None:
    m = _CUE_RANGE_RE.match(str(text or ""))
    if not m:
        return None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    return (start, end) if end >= start else (start, start)


def _srt_seconds(ts: str) -> float:
    """'HH:MM:SS,mmm' (or with '.') -> seconds."""
    m = re.match(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})", str(ts or ""))
    if not m:
        return 0.0
    ms = int(m.group(4).ljust(3, "0"))
    return (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            + ms / 1000.0)


# Checked BEFORE a single image renders: no image may HOLD longer than this. The
# COUNT is never fixed - the cues drive it, so one image may cover one cue or
# many, and a dense passage can run to 7-8 images a minute while a developing
# idea holds. The channel sets the maximum (Settings > shotlist_max_hold_seconds);
# 12s by default. Fragmentation (one image per cue) is a fault in its own right.
SHOT_MAX_HOLD_DEFAULT = 12.0
# The planning brief's own rules: a long hold (~15s+) is acceptable only when it
# carries motion; ST is only for ~1-2-cue shots (~2-4s) and ~10% of shots; no
# motion code above ~40% (PL/PR tighter, at ~10% - they default too easily);
# most shots should span several cues.
# (The limits themselves live in briefs.py, next to the brief wording they
# mirror; a channel's motion profile can replace them - see shotlist_pacing.)
ST_MAX_HOLD_SECONDS = briefs.ST_MAX_HOLD_SECONDS
ST_MAX_SHARE = briefs.ST_MAX_SHARE
MOTION_MAX_SHARE = briefs.MOTION_MAX_SHARE
# PL/PR are the easiest motions to reach for by default (any left/right beat
# "just works" with a pan), so a plan leans on them far more readily than the
# others - a tighter, code-specific cap catches that before the general 40%
# ceiling would ever trip.
MOTION_MAX_SHARE_OVERRIDES = briefs.MOTION_MAX_SHARE_OVERRIDES
FRAGMENTATION_SHARE = 0.50


def shotlist_type_faults(data: dict, profile) -> list[str]:
    """Hard faults against the channel's shot-type spec (briefs.normalize_types):
    a type over its cap (0% = never used) and the host-in-frame share per type.
    A shot's type is the third part of its asset name (S01_04_INF_ZI.png); it
    shows the host when its image entry lists the channel's host ref. Nothing
    set = no faults."""
    spec = getattr(profile, "types", None) if profile else None
    if not spec:
        return []
    shots = [x for x in (data.get("shots") or []) if isinstance(x, dict)
             and x.get("asset")]
    if not shots:
        return []
    refs_by_file = {}
    for img in (data.get("images") or []):
        if isinstance(img, dict) and img.get("file"):
            refs_by_file[str(img["file"])] = {
                ref_name(r).lower() for r in (img.get("refs") or []) if r}
    by_type: dict[str, list[str]] = {}
    for x in shots:
        parts = Path(str(x["asset"])).stem.split("_")
        code = parts[-2].upper() if len(parts) >= 3 else ""
        by_type.setdefault(code, []).append(str(x["asset"]))
    total = len(shots)
    faults: list[str] = []
    for code, cap in (spec.get("max") or {}).items():
        n = len(by_type.get(code, []))
        if not n:
            continue
        if cap == 0:
            faults.append(
                f"{n} {code} shot(s) but {code} is never used on this "
                f"channel: {', '.join(by_type[code][:4])}"
                f"{' ...' if n > 4 else ''}")
        elif total >= briefs.TYPE_CAP_MIN_PLAN and n * 100 > cap * total:
            faults.append(
                f"{code} is {n / total:.0%} of the shots ({n} of {total}); "
                f"this channel allows at most {cap}%")
    if "graphics_max" in spec:
        g = spec["graphics_max"]
        names = [f for c in briefs.GRAPHIC_CODES for f in by_type.get(c, [])]
        n = len(names)
        if n and (g == 0 or (total >= briefs.TYPE_CAP_MIN_PLAN
                             and n * 100 > g * total)):
            faults.append(
                f"diagram/infographic shots ({'/'.join(briefs.GRAPHIC_CODES)}) "
                f"are {n / total:.0%} of the shots ({n} of {total}); this "
                f"channel allows "
                + ("none" if g == 0 else f"at most {g}%"))
    ref = str(spec.get("host_ref") or "").strip().lower()
    for code, want in (spec.get("host") or {}).items():
        files = by_type.get(code, [])
        if not ref or not files:
            continue
        shown = [f for f in files if ref in refs_by_file.get(f, set())]
        n, h = len(files), len(shown)
        share = h * 100 / n
        label = spec.get("host_ref")
        if want == 0 and h:
            faults.append(f"the host ({label}) is in {h} of {n} {code} shots "
                          f"but must never appear in them: "
                          f"{', '.join(shown[:4])}{' ...' if h > 4 else ''}")
        elif want == 100 and h < n:
            missing = [f for f in files if f not in shown]
            faults.append(f"the host ({label}) must be in every {code} shot "
                          f"but is missing from {n - h} of {n}: "
                          f"{', '.join(missing[:4])}"
                          f"{' ...' if len(missing) > 4 else ''}")
        elif 0 < want < 100 and n >= briefs.TYPE_MIN_SHOTS \
                and abs(share - want) > briefs.HOST_TOLERANCE:
            faults.append(f"the host ({label}) is in {h} of {n} {code} shots "
                          f"({share:.0f}%); this channel wants about {want}% "
                          f"({max(0, want - briefs.HOST_TOLERANCE)}-"
                          f"{min(100, want + briefs.HOST_TOLERANCE)}%)")
    return faults


def shot_reveal(shot: dict) -> list[int] | None:
    """The cue numbers of a reveal shot (one per item, in order) as the plan
    wrote them - "reveal": [41, 42, 43] or {"cues": [...]} - or None when the
    shot has no usable reveal. Whether they are VALID is shotlist_reveal_faults'
    job; this only reads them."""
    raw = shot.get("reveal") if isinstance(shot, dict) else None
    if isinstance(raw, dict):
        raw = raw.get("cues")
    if not isinstance(raw, list) or not raw:
        return None
    out: list[int] = []
    for v in raw:
        if isinstance(v, bool) or not isinstance(v, int):
            return None
        out.append(v)
    return out


REVEAL_LAYOUTS = ("row", "grid")
BUILTIN_SFX = ("pop", "ding", "click", "tick", "whoosh", "swipe")
SFX_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\- ]{0,39}$")


def shot_reveal_layout(shot: dict) -> str:
    """"row" (default) or "grid" - what the plan wrote, lower-cased; an unknown
    value is returned as written so the fault check can name it."""
    raw = shot.get("reveal") if isinstance(shot, dict) else None
    if isinstance(raw, dict) and raw.get("layout") not in (None, ""):
        return str(raw.get("layout")).strip().lower()
    return "row"


def shot_reveal_assets(shot: dict) -> list[str] | None:
    """The separate images of a build-up reveal ("assets": [...]) or None."""
    raw = shot.get("reveal") if isinstance(shot, dict) else None
    if not isinstance(raw, dict) or raw.get("assets") in (None, ""):
        return None
    assets = raw.get("assets")
    if not isinstance(assets, list):
        return []
    return [a.strip() if isinstance(a, str) else "" for a in assets]


def shot_sfx(shot: dict) -> list[str] | None:
    """Sound names of a shot ("sfx": "pop" or a list) or None. Non-string
    entries come back as "" so shotlist_sfx_faults can reject them."""
    raw = shot.get("sfx") if isinstance(shot, dict) else None
    if raw in (None, ""):
        return None
    if isinstance(raw, str):
        return [raw.strip()]
    if isinstance(raw, list):
        return [v.strip() if isinstance(v, str) else "" for v in raw]
    return [""]


def shotlist_sfx_faults(shots: list[dict]) -> list[str]:
    """Sound effects: names only (a built-in or a file the creator put in the
    project's sfx folder); a normal shot takes one name, a reveal shot at most
    one per item."""
    faults: list[str] = []
    for s in shots:
        if not isinstance(s, dict) or s.get("sfx") in (None, ""):
            continue
        asset = str(s.get("asset") or "?")
        names = shot_sfx(s)
        if not names or any(not SFX_NAME_RE.match(n) for n in names):
            faults.append(f"{asset}: \"sfx\" must be a sound name such as "
                          f"\"pop\" (built-in: {', '.join(BUILTIN_SFX)}) or a "
                          f"list of names")
            continue
        items = shot_reveal(s)
        if items is None and len(names) > 1:
            faults.append(f"{asset}: a shot without a reveal takes ONE sfx "
                          f"name, not a list")
        elif items is not None and len(names) > len(items):
            faults.append(f"{asset}: {len(names)} sfx names for "
                          f"{len(items)} reveal items")
    return faults


def shotlist_reveal_faults(shots: list[dict],
                           image_names: set | None = None) -> list[str]:
    """Objective faults on reveal shots: 2-4 items, one cue per item, strictly
    increasing, inside the shot's own cues, the first on the shot's first cue,
    and a static (ST) shot. A reveal that breaks one of these cannot be cut
    correctly, so it fails here instead of surfacing at merge time."""
    faults: list[str] = []
    lo_n, hi_n = briefs.REVEAL_MIN_ITEMS, briefs.REVEAL_MAX_ITEMS
    for s in shots:
        if not isinstance(s, dict) or s.get("reveal") in (None, ""):
            continue
        asset = str(s.get("asset") or "?")
        cues = shot_reveal(s)
        rng = cue_range(s.get("cues"))
        if cues is None or not (lo_n <= len(cues) <= hi_n):
            faults.append(f"{asset}: \"reveal\" needs {lo_n}-{hi_n} cue "
                          f"numbers, one per item, e.g. [41, 42, 43]")
            continue
        if any(b <= a for a, b in zip(cues, cues[1:])):
            faults.append(f"{asset}: reveal cues {cues} must increase, one "
                          f"per item in narration order")
        elif rng and (cues[0] != rng[0] or cues[-1] > rng[1]):
            faults.append(f"{asset}: reveal cues {cues} must start at the "
                          f"shot's first cue ({rng[0]}) and stay inside its "
                          f"cues {rng[0]}-{rng[1]}")
        code = Path(asset).stem.split("_")[-1].upper()
        motion = str(s.get("motion") or "ST").upper()
        if code != "ST" or motion != "ST":
            faults.append(f"{asset}: a reveal shot is static - set motion "
                          f"\"ST\" and end the file name with _ST")
        layout = shot_reveal_layout(s)
        if layout not in REVEAL_LAYOUTS:
            faults.append(f"{asset}: reveal layout \"{layout}\" must be "
                          f"\"row\" or \"grid\"")
        elif layout == "grid" and len(cues) != 4:
            faults.append(f"{asset}: a grid reveal needs exactly 4 items "
                          f"(2x2), not {len(cues)}")
        assets = shot_reveal_assets(s)
        if assets is not None:
            if (len(assets) != len(cues) or any(not a for a in assets)
                    or len(set(assets)) != len(assets)):
                faults.append(f"{asset}: reveal \"assets\" needs {len(cues)} "
                              f"different image file names, one per cue")
                continue
            if assets[0] != asset:
                faults.append(f"{asset}: the shot's asset must be the first "
                              f"of its reveal assets ({assets[0]})")
            bad = [a for a in assets
                   if Path(a).stem.split("_")[-1].upper() != "ST"]
            if bad:
                faults.append(f"{asset}: every reveal image is static - end "
                              f"{', '.join(bad[:3])} with _ST")
            if image_names is not None:
                lost = [a for a in assets if a not in image_names]
                if lost:
                    faults.append(f"{asset}: reveal image(s) not in images: "
                                  f"{', '.join(lost[:4])}")
    return faults


def shotlist_pacing(data: dict, cues: list[dict],
                    max_hold_seconds: float = SHOT_MAX_HOLD_DEFAULT,
                    profile: "briefs.MotionProfile | None" = None
                    ) -> tuple[list[str], list[str]]:
    """(faults, warnings) for how long the plan holds each image, derived from
    the SRT cue timings before anything is rendered.

    The COUNT is not fixed - the cues drive it, so an image may cover one cue or
    many. The hard rule is the channel's maximum hold: a shot planned to hold
    longer re-plans with the brief's own instructions - split at a MEANING
    boundary (Section 2), keep the existing asset's name and its first cues, and
    give the new image the next unused sub-beat index in the SAME scene
    (Section 9 - sub-beat numbers are stable, never rename). The brief's other
    rules (no long STATIC hold, ST only on short holds and ~10% of shots, no
    motion code above ~40% - PL/PR tighter at ~10% - no fragmentation) are
    enforced at the same time.

    `profile` is the channel's motion profile (briefs.py): it can forbid
    motion codes outright (a static-only channel), lift or drop the ST / motion
    share limits, and set a minimum hold. None = the standard limits."""
    prof = profile or briefs.STANDARD
    shots = [s for s in (data.get("shots") or []) if isinstance(s, dict)]
    if not shots or not cues:
        return [], []
    cap = max(1.0, float(max_hold_seconds or SHOT_MAX_HOLD_DEFAULT))
    by_index = {c["index"]: c for c in cues if isinstance(c, dict)}
    duration = _srt_seconds(cues[-1].get("end"))
    holds: list[tuple[float, str, str, int, int]] = []
    for s in shots:
        rng = cue_range(s.get("cues"))
        if not rng:
            continue
        first, last = by_index.get(rng[0]), by_index.get(rng[1])
        if not first or not last:
            continue
        # a reveal shot is static but long by design: it must not count as ST
        # (long-static, ST share/hold caps) nor trip an allowed-codes check
        motion = ("REVEAL" if shot_reveal(s) else
                  str(s.get("motion") or "").upper())
        holds.append((_srt_seconds(last.get("end")) - _srt_seconds(first.get("start")),
                      motion,
                      str(s.get("asset") or "?"), rng[0], rng[1]))
    if not holds:
        return [], []
    faults: list[str] = []
    long = sorted((h for h in holds if h[0] > cap), reverse=True)
    if long:
        SHOWN_CAP = 30
        lines = []
        for hold, _motion, asset, first, last in long[:SHOWN_CAP]:
            midpoint = _srt_seconds(by_index[first]["start"]) + hold / 2
            choices = [c for c in range(first + 1, last + 1) if c in by_index]
            split_at = min(choices,
                           key=lambda c: abs(_srt_seconds(by_index[c]["start"])
                                             - midpoint)) if choices else None
            at = f" around cue {split_at}" if split_at else ""
            lines.append(f"{asset} cues {first}-{last} ({hold:.0f}s) - split{at}")
        omitted = len(long) - len(lines)
        # Naming every offender (not just a sample) matters: a re-plan is a
        # fresh call with no memory of the previous attempt, so a shot left
        # off this list is one the model has no way to know about. Earlier
        # this only ever showed the worst 6, and re-plans kept the total
        # offender count roughly flat attempt to attempt (fixing the 6 shown,
        # breaking new ones elsewhere) instead of driving it to zero.
        floor = int(duration / cap) + 1
        if prof.min_hold:
            count_note = (
                f"Split only where the idea genuinely changes and do not cut "
                f"shots below ~{prof.min_hold:.0f}s: this channel paces slowly "
                f"({prof.min_hold:.0f}-{cap:.0f}s per shot). ")
        else:
            count_note = (
                f"The count is NOT a target to hit: {floor} is only the "
                f"absolute FLOOR (it assumes every shot runs the full {cap:.0f}s, "
                f"but most cues are shorter), so splitting every long hold drives "
                f"the real count WELL ABOVE {floor} - do not "
                f"stop near that number. ")
        listed_desc = ("every shot over the maximum" if not omitted
                       else f"the {len(lines)} worst of {len(long)} (the rest "
                            "follow the identical rule)")
        faults.append(
            f"{len(long)} shot(s) hold longer than the {cap:.0f}s maximum. Split "
            f"EVERY one of them at a meaning boundary (a number, statistic or "
            f"price arrives; a second character, object or location enters; the "
            f"narration pivots; the action changes; a list ends and the payoff "
            f"begins) so no image holds longer than {cap:.0f}s. {count_note}"
            f"Number each scene's images in order - the first image in scene S12 is "
            f"S12_01, the next S12_02, and so on - so a new image takes the next "
            f"unused sub-beat in its scene, with its own TYPE and MOTION codes and "
            f"its own prompt describing the separated sub-beat. Below is {listed_desc}"
            f" (apply the same rule to every shot over the maximum, not only "
            f"the ones named here):\n  "
            + "\n  ".join(lines)
            + (f"\n  ...and {omitted} more of the same kind" if omitted else ""))
    if prof.allowed:
        banned = [(a, m) for _h, m, a, _f, _l in holds
                  if m and m != "REVEAL" and m not in prof.allowed]
        if banned:
            only = "/".join(prof.allowed)
            faults.append(
                f"{len(banned)} shot(s) use a motion code this channel does "
                f"not allow (allowed: {only} only): "
                + ", ".join(f"{a} ({m})" for a, m in banned[:6])
                + f". Set every shot's \"motion\" to {only} and end its asset "
                f"file name with the same code (e.g. S01_03_SCN_"
                f"{prof.allowed[0]}.png).")
    if prof.static_long_hold is not None:
        static_long = [(h, a) for h, m, a, _f, _l in holds
                       if m == "ST" and h >= prof.static_long_hold]
        if static_long:
            faults.append("a long STATIC hold is never acceptable: " + ", ".join(
                f"{a} ({h:.0f}s, ST)" for h, a in static_long[:4]))
    st = [(h, a) for h, m, a, _f, _l in holds if m == "ST"]
    if (prof.st_max_share is not None and st
            and len(st) / len(holds) > prof.st_max_share):
        faults.append(f"ST is {len(st) / len(holds):.0%} of shots (cap ~"
                      f"{prof.st_max_share:.0%}) - ST is the exception, not "
                      f"the default")
    if prof.st_max_hold is not None:
        st_long = [(h, a) for h, a in st if h > prof.st_max_hold]
        if st_long:
            faults.append(f"ST shots hold longer than {prof.st_max_hold:.0f}s: "
                          + ", ".join(f"{a} ({h:.0f}s)" for h, a in st_long[:4]))
    if prof.code_max_share is not None:
        for code in sorted({m for _h, m, _a, _f, _l in holds
                            if m and m != "REVEAL"}):
            share = sum(1 for _h, m, _a, _f, _l in holds if m == code) / len(holds)
            share_cap = prof.code_share_overrides.get(code, prof.code_max_share)
            if share > share_cap:
                faults.append(f"motion {code} is {share:.0%} of shots (cap ~"
                              f"{share_cap:.0%}) - vary the motion codes")
    for codes, share_cap in getattr(prof, "group_caps", ()):
        share = sum(1 for _h, m, _a, _f, _l in holds if m in codes) / len(holds)
        if share > share_cap + 1e-9:
            faults.append(f"{'/'.join(codes)} together are {share:.0%} of "
                          f"shots (cap ~{share_cap:.0%}) - vary the motion "
                          f"codes")
    if len(holds) >= briefs.MIX_MIN_SHOTS:
        total = len(holds)
        for codes, target in briefs.mix_targets(prof):
            share = sum(1 for _h, m, _a, _f, _l in holds if m in codes) / total
            floor = target * briefs.MIX_FLOOR_RATIO
            if share < floor - 1e-9:
                label = "/".join(codes)
                faults.append(
                    f"{label} is only {share:.0%} of shots - this channel's "
                    f"mix is about {target:.0%} (never below ~{floor:.0%}). "
                    f"Give about {max(1, round(target * total) - round(share * total))}"
                    f" more shots {label}: change the shot's \"motion\" AND "
                    f"the ending of its asset file name together"
                    + (" (only on holds of ~5s or less)" if codes == ("ST",)
                       else ""))
        if briefs.zoom_balanced(prof):
            zi = sum(1 for _h, m, _a, _f, _l in holds if m == "ZI")
            zo = sum(1 for _h, m, _a, _f, _l in holds if m == "ZO")
            if zi + zo >= briefs.MIX_MIN_ZOOM_SHOTS:
                lo, hi = briefs.ZOOM_SPLIT
                ratio = zi / (zi + zo)
                if ratio < lo - 1e-9 or ratio > hi + 1e-9:
                    more, less = ("ZO", "ZI") if ratio > hi else ("ZI", "ZO")
                    move = round(abs(zi - (zi + zo) / 2))
                    faults.append(
                        f"ZI is {ratio:.0%} of the ZI+ZO shots ({zi} ZI, {zo} "
                        f"ZO) - it must be {lo:.0%}-{hi:.0%}. Change about "
                        f"{move} {less} shots to {more}: change the shot's "
                        f"\"motion\" AND the ending of its asset file name "
                        f"together")
    if prof.min_hold and len(holds) > 1:
        body = holds[:-1]   # the last shot ends where the narration ends
        short = [(h, a) for h, _m, a, _f, _l in body if h < prof.min_hold]
        if short and len(short) / len(body) > briefs.MIN_HOLD_MAX_SHORT_SHARE:
            faults.append(
                f"{len(short)} of {len(body)} shots hold under the "
                f"{prof.min_hold:.0f}s this channel expects - the plan is "
                f"cutting the narration into too many short shots; group the "
                f"cues that develop one idea into a single shot of "
                f"{prof.min_hold:.0f}-{cap:.0f}s. Shortest: "
                + ", ".join(f"{a} ({h:.0f}s)" for h, a in
                            sorted(short)[:5]))
    one_cue = sum(1 for s in shots
                  if (r := cue_range(s.get("cues"))) and r[0] == r[1])
    if one_cue / len(holds) > FRAGMENTATION_SHARE:
        faults.append(f"{one_cue} of {len(holds)} shots span a single cue - "
                      f"changing images more often than the ideas change is "
                      f"fragmentation; group the cues that develop one idea")
    faults.extend(shotlist_type_faults(data, prof))
    faults.extend(shotlist_type_faults(data, prof))
    # No count warning: the count is never fixed - if every hold is under the
    # maximum the plan is legal, however many images that takes.
    return faults, []


# Reference names ARE the identity: FlowBatch attaches project assets by
# name, Renderly resolves them as filenames, and Flow's own card matching is
# fuzzy. So the shape is enforced, not hoped for.
REF_NAME_RE = re.compile(r"^(CH|BG|OBJ)_[A-Z0-9]+(?:_[0-9]{2})?$")

# Refs a plan INVENTS for on-the-fly generation are capped: each one is a Flow
# generation (credits), and a plan that leans on invented refs instead of the
# supplied bible is a planning smell. Refs the user PROVIDES do not count.
REFS_ON_THE_FLY_CAP = 20


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


def shotlist_cue_faults(shots: list[dict], cue_count: int,
                        limit: int = 12) -> list[str]:
    """Cue-coverage faults of a shotlist's shots: a bad range, a cue with no
    shot (typically the tail of a plan that stopped early), cue numbers past
    the SRT, shots out of order, overlapping ranges. No LLM - always exact."""
    faults: list[str] = []
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
    return faults


def shotlist_coverage_report(pdir: Path) -> dict | None:
    """{cues, covered_to, faults} for the production's saved shotlist against
    its subtitles, or None when either is missing/unreadable. `faults` is
    empty exactly when every cue 1..cues is covered once, in order."""
    try:
        data = json.loads((Path(pdir) / "shotlist.json")
                          .read_text(encoding="utf-8"))
        cues = parse_srt_cues((Path(pdir) / "subtitles.srt")
                              .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not cues or not isinstance(data, dict):
        return None
    shots = [x for x in (data.get("shots") or []) if isinstance(x, dict)]
    ends = [r[1] for x in shots if (r := cue_range(x.get("cues")))]
    faults = shotlist_cue_faults(shots, len(cues)) if shots else \
        ["no shots in the shotlist"]
    return {"cues": len(cues), "covered_to": max(ends) if ends else 0,
            "faults": faults}


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

    faults.extend(shotlist_cue_faults(shots, cue_count, limit))
    names = {str(i.get("file")) for i in images if i.get("file")}
    faults.extend(shotlist_reveal_faults(shots, names))
    faults.extend(shotlist_sfx_faults(shots))
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
    # skipping" on every rendered image. The CH_/BG_/OBJ_ convention is
    # mandatory only for refs the plan INVENTS for on-the-fly generation (no
    # supplied path, but a refPrompts entry): refs the user PROVIDES keep
    # whatever name they were given - renaming a supplied file's identity
    # would break the path it points at.
    declared = declared_refs(data)
    prompts = data.get("refPrompts")
    prompts = prompts if isinstance(prompts, dict) else {}
    used: set[str] = set()
    for i in images:
        for r in (i.get("refs") or []):
            name = ref_name(r)
            if name:
                used.add(name)
    generated = sorted(
        n for n in used
        if not (isinstance(declared.get(n), str) and declared.get(n).strip())
        and str(prompts.get(n) or "").strip())
    bad = sorted(n for n in generated if not REF_NAME_RE.match(n))
    if bad:
        faults.append(f"{len(bad)} generated reference name(s) break the "
                      f"CH_/BG_/OBJ_ convention (provided refs may keep their "
                      f"own names): {', '.join(bad[:6])}")
    if len(generated) > REFS_ON_THE_FLY_CAP:
        faults.append(f"{len(generated)} references would be generated on the "
                      f"fly - the cap is {REFS_ON_THE_FLY_CAP}: "
                      f"{', '.join(generated[:8])}")
    undeclared = sorted(used - set(declared))
    if undeclared:
        faults.append(f"{len(undeclared)} reference name(s) are used by an "
                      f"image but not declared in the shotlist's refs: "
                      f"{', '.join(undeclared[:6])}")
    # A SUPPLIED reference (a registry entry with a path) that no image attaches
    # means the plan never features it. The brief calls a plan that never uses a
    # supplied character a failure, and the refs stage would copy the file for
    # nothing - so this must fail here rather than render a faceless plan.
    supplied = {n for n, entry in declared.items()
                if isinstance(entry, str) and entry.strip()}
    if supplied and not (used & supplied):
        faults.append("the shotlist declares "
                      f"{len(supplied)} supplied reference(s) but no image "
                      f"attaches any of them: {', '.join(sorted(supplied)[:6])}")
    # A ref with no path AND no prompt can never be produced: the refs stage
    # would have nothing to generate it from, so the image would silently lose
    # its subject. (A path that exists on disk is checked by the refs stage,
    # which can see the filesystem.)
    stranded = sorted(
        n for n in used
        if not (isinstance(declared.get(n), str) and declared.get(n).strip())
        and not str(prompts.get(n) or "").strip())
    if stranded:
        faults.append(f"{len(stranded)} reference name(s) have no supplied "
                      f"path and no prompt, so they cannot be generated: "
                      f"{', '.join(stranded[:6])}")
    return faults[:limit]


def alignment_prompt(chunk: list[dict], cues: dict[int, str],
                     style_guide: str = "") -> str:
    """Audit prompt COMPLETENESS per scene, not just topical match.

    Rendering an image is slow and costs credits, so a prompt that only
    partially describes its beat means a hand regeneration later. The judge is
    asked what the narration at those cues REQUIRES and whether the prompt
    states each of those elements explicitly."""
    items = []
    for s in chunk:
        rng = cue_range(s.get("cues")) or (0, 0)
        narration = " ".join(cues.get(c, "") for c in range(rng[0], rng[1] + 1))
        # No truncation: the manifest brief lets a single image prompt run up
        # to ~2400 characters (shared with the "style" master prompt's own
        # 1500-character budget), and a shot merging many cues into one
        # INF/overview beat can produce narration well past a few hundred
        # characters too. Clipping either one at a small fixed length (this
        # used to be [:700]) hides real content from the judge - a fully
        # detailed prompt that puts a required element after character 700
        # was being marked "missing" for something that was actually there.
        # chunk_size already bounds how much text one judge call carries.
        reveal = shot_reveal(s)
        reveal_line = ""
        if reveal:
            grid = shot_reveal_layout(s) == "grid"
            where = "quadrant" if grid else "slice"
            parts = "; ".join(
                f'item {k} ({where} {k} of {len(reveal)}) at cue {c}: '
                f'"{cues.get(c, "")}"' for k, c in enumerate(reveal, 1))
            reveal_line = (
                f'  REVEAL SHOT, {len(reveal)} items shown one at a time, '
                + ('in a 2x2 grid (top-left, top-right, bottom-left, '
                   'bottom-right)' if grid else 'left to right')
                + f': {parts}\n')
        item = s.get("reveal_item")
        if item:
            reveal_line += (
                f'  BUILD-UP IMAGE {item[0]} of {item[1]} ({item[2]}): shown '
                f'in its own slot beside the others and centre-cropped to '
                f'fill it\n')
        items.append(f'- asset: {s.get("asset")}\n'
                     f'  cues {rng[0]}-{rng[1]} narrate: "{narration}"\n'
                     + reveal_line +
                     f'  image prompt: "{str(s.get("prompt"))}"')
    reveal_rules = ((REVEAL_JUDGE_RULES if any(
        shot_reveal(s) for s in chunk) else "")
        + (REVEAL_BUILDUP_JUDGE_RULES if any(
            s.get("reveal_item") for s in chunk) else ""))
    return (
        "You are auditing a video shotlist BEFORE its images are rendered. For "
        "each shot you get the narration at its cue range and the image prompt "
        "that will be sent to the image model. Rendering is slow and costs "
        "money, so a prompt that under-specifies its scene has to be "
        "regenerated by hand later.\n\n"
        "First work out what the narration at those cues REQUIRES the image to "
        "show: the subject(s) present, what they are doing, where they are, the "
        "objects or props involved, and the specific CONCRETE, VISIBLE "
        "information the cue is conveying. Then judge whether the PROMPT "
        "states each of those elements explicitly and concretely.\n\n"
        "A single still frame cannot show everything a sentence can say. Do "
        "NOT require the prompt to depict any of the following - these are "
        "narration's job, not the image's, and a prompt that omits them is "
        "still complete:\n"
        "  - recurrence or frequency (\"every morning\", \"each time\", "
        "\"always\")\n"
        "  - duration or a span of time (\"for years\", \"over decades\", "
        "\"since childhood\")\n"
        "  - universal or statistical claims (\"every human\", \"wired into "
        "all of us\", a percentage or study result)\n"
        "  - internal, invisible states (a thought, an urge, a memory, "
        "something happening \"in the brain\" or \"before conscious "
        "awareness\")\n"
        "  - causal or explanatory claims about WHY something happens, as "
        "opposed to showing THAT it happens\n"
        "  - exact numbers, ranges, dates or units (\"25-150 Hz\", \"1 to "
        "10 centimeters per second\", \"in 1943\", \"10 to 15 minutes\") "
        "WHEN THE CHANNEL STYLE BELOW BANS READABLE TEXT/NUMBERS - showing "
        "the PRECISE figure then needs a legible label the image is not "
        "allowed to carry, so a relative visual (a gauge, a bar, a "
        "before/after size) already satisfies the cue. If the style "
        "permits on-image text, a prompt that omits an exact figure the "
        "narration states IS missing it - hold the prompt to that "
        "figure normally\n"
        "Only flag an element as missing when a viewer could plausibly see it "
        "in a single static illustration: a concrete subject, a visible "
        "action or pose, a setting, an object or prop, a visible expression, "
        "or a spatial relationship between things in frame.\n\n"
        "Verdicts:\n"
        '  "ok"     - every required CONCRETE, VISIBLE element is stated '
        "explicitly; the image model has everything it needs.\n"
        '  "weak"   - the right subject is there but it is vague, generic or '
        "missing required visible detail (e.g. no setting, no action, no "
        "props).\n"
        '  "missing" - it does not depict this beat at all, or depicts a '
        "different one.\n"
        "Judge visual completeness only: stylistic wording is irrelevant, and "
        "do not require anything the narration does not ask for, or anything "
        "in the excluded list above.\n\n"
        + (f"CHANNEL VISUAL STYLE (its text/number policy decides the "
           f"exact-number exclusion above):\n{style_guide.strip()[:2000]}\n\n"
           if (style_guide or "").strip() else "")
        + "\n".join(items)
        + reveal_rules +
        "\n\nReply with ONLY a JSON object:\n"
        '{"shots": [{"asset": "<asset>", "verdict": "ok"|"weak"|"missing", '
        '"missing": ["<element the narration requires that the prompt does not '
        'state>", ...], "reason": "<one short line>"}]}\n'
        "Include every shot, and leave \"missing\" empty for ok shots."
    )


REVEAL_JUDGE_RULES = (
    "\n\nREVEAL SHOTS. A shot marked REVEAL SHOT is ONE image holding N items "
    "in a left-to-right row, each in its own equal-width slice (halves, thirds "
    "or quarters). The video cuts the image along the slice lines and shows "
    "slice 1, then adds slice 2 when the narrator reaches item 2, and so on - "
    "so a wrong layout cannot be repaired later. Judge a reveal shot STRICTLY: "
    "the prompt must (1) state the exact item count N; (2) name each item and "
    "put them in the narrated order, left to right, item k in slice k - the "
    "item the narration names at the first listed cue is the LEFTMOST; (3) "
    "place every item inside its own equal slice, centered, with clear empty "
    "space between neighbouring slices; (4) keep everything (objects, shadows, "
    "floor lines, glows, arrows, props, text) from crossing a vertical slice "
    "line; (5) use one plain, identical background, lighting, scale and "
    "ground line across all slices; (6) share no character, hand or object "
    "between slices. If any of (1)-(6) is absent or ambiguous the verdict is "
    "\"weak\" and \"missing\" names exactly which one (e.g. \"item 2 is not "
    "assigned to the middle third\", \"does not forbid objects crossing the "
    "slice lines\"). If the items described do not match what the narration "
    "names at those cues, or the order is wrong, the verdict is \"missing\". "
    "For a reveal marked as a 2x2 GRID the same applies to four equal "
    "quadrants in reading order - item 1 top-left, item 2 top-right, item 3 "
    "bottom-left, item 4 bottom-right - and nothing may cross the vertical or "
    "the horizontal centre line. "
    "A reveal prompt is NOT excused by the excluded-elements list above for "
    "these layout requirements.\n")


REVEAL_BUILDUP_JUDGE_RULES = (
    "\n\nBUILD-UP IMAGES. A shot marked BUILD-UP IMAGE is one of several "
    "separate pictures that appear one after another and stay on screen side "
    "by side (a row of tall strips, or a 2x2 grid). It is judged like any "
    "image against ITS OWN narration, plus one layout requirement: the prompt "
    "must make the picture ONE clear subject centered in the frame with "
    "generous empty space on both sides, because only the centre is kept when "
    "the picture is cropped into its slot. A prompt that spreads several "
    "subjects across the width, or pushes the subject to an edge, is "
    "\"weak\" and \"missing\" says \"one centered subject with empty side "
    "margins\".\n")


def judge_shots(data: dict, prompt_by_file: dict) -> list[dict]:
    """The shots the judge sees. A normal or one-image reveal shot is itself;
    a build-up reveal (separate images) becomes one pseudo-shot per image, each
    covering only ITS item's cues, so every image is graded against what the
    narrator says while it appears and a weak one can be patched by file name
    like any other prompt."""
    out: list[dict] = []
    for s in (data.get("shots") or []):
        if not isinstance(s, dict) or not s.get("asset"):
            continue
        assets = shot_reveal_assets(s)
        cues_n = shot_reveal(s)
        rng = cue_range(s.get("cues"))
        if assets and cues_n and len(assets) == len(cues_n) and rng:
            layout = shot_reveal_layout(s)
            for k, a in enumerate(assets):
                last = cues_n[k + 1] - 1 if k + 1 < len(cues_n) else rng[1]
                item = {**s, "asset": a, "cues": f"{cues_n[k]}-{last}",
                        "prompt": prompt_by_file.get(a, ""),
                        "reveal_item": (k + 1, len(assets),
                                        "2x2 grid" if layout == "grid"
                                        else "row")}
                item.pop("reveal", None)
                out.append(item)
            continue
        out.append({**s, "prompt": prompt_by_file.get(str(s["asset"]), "")})
    return out


def _judge_shot_chunks(cfg, shots: list[dict], cue_text: dict[int, str],
                       provider: str | None, chunk_size: int,
                       style_guide: str, temperature: float) -> dict:
    """Run alignment_prompt over `shots` in chunk_size-sized batches, IN
    PARALLEL. Returns {matched, weak, verdicts, unreviewed, error}.

    `verdicts` maps every actually-judged asset to its verdict entry
    ({"verdict": "ok"} for a pass, the full weak/missing/reason entry
    otherwise) - review_shotlist_patch below carries these forward on a
    later patch attempt instead of re-judging shots that did not change.

    The chunk calls are independent of each other (each is its own
    self-contained judge request), so they used to run one after another
    for no reason - on a 100+ shot plan split into 20-shot chunks that is
    5-8 sequential LLM round trips stacked end to end. Running them
    concurrently turns that into roughly the time of the SLOWEST chunk,
    not the sum of all of them.

    Each chunk also gets ONE immediate retry before it is given up on as
    `unreviewed`. A transient failure (a dropped connection, a provider
    hiccup) used to cost that chunk's shots the same way a genuine judge
    outage does - 2 unlucky chunks out of 9 could drag an otherwise-passing
    plan's ratio down for a reason that has nothing to do with the plan's
    actual quality. One retry catches the common transient case without
    turning a real, repeated failure into a silent retry loop."""
    chunks = [shots[i:i + chunk_size] for i in range(0, len(shots), chunk_size)]
    if not chunks:
        return {"matched": 0, "weak": [], "verdicts": {}, "unreviewed": 0,
                "error": None}

    def _judge_one(chunk):
        last_err = None
        for retry in range(2):  # first try + one retry
            try:
                reply = _parse_json_object(llm_generate(
                    cfg, alignment_prompt(chunk, cue_text, style_guide=style_guide),
                    provider=provider, temperature=temperature))
                return chunk, reply, None
            except Exception as exc:  # noqa: BLE001 - a judge failure must
                last_err = str(exc)[:200]  # not lose an otherwise usable plan
        return chunk, None, last_err

    matched = 0
    weak: list[dict] = []
    verdicts: dict[str, dict] = {}
    unreviewed = 0
    error = None
    # 8 was a leftover from an unrelated ai33.py concurrency pattern -
    # judge chunks are independent HTTP calls to the shotlist judge
    # provider, so 8 meant a 228-shot/chunk_size-20 plan (12 chunks) ran
    # in two waves instead of one. 16 clears that case in a single wave
    # while still bounding worst-case concurrency for very large plans
    # (800+ cues can mean 40+ chunks) against an API with no documented
    # rate limit.
    with ThreadPoolExecutor(max_workers=min(16, len(chunks))) as pool:
        for chunk, reply, err in pool.map(_judge_one, chunks):
            if err is not None:
                # These shots were never actually judged - they must NOT
                # count as matched (that would silently pass unverified
                # prompts) and must NOT be added to `weak` either (that
                # would tell the next attempt to rewrite prompts that may be
                # perfectly fine; only the judge call failed). `unreviewed`
                # is how a caller tells "genuinely reviewed, some are weak"
                # apart from "the judge never ran".
                error = err
                unreviewed += len(chunk)
                continue
            chunk_verdicts = (reply.get("shots")
                              if isinstance(reply.get("shots"), list) else [])
            by_asset = {str(v.get("asset")): v for v in chunk_verdicts
                       if isinstance(v, dict)}
            for s in chunk:
                asset = str(s.get("asset"))
                v = by_asset.get(asset)
                if v is None or str(v.get("verdict") or "").lower() == "ok":
                    # v is None: the judge skipped it - do not fail a shot on
                    # silence
                    matched += 1
                    verdicts[asset] = {"verdict": "ok", "missing": [],
                                       "reason": ""}
                    continue
                missing = [str(m) for m in (v.get("missing") or [])
                          if str(m).strip()]
                entry = {"asset": asset,
                        "verdict": str(v.get("verdict") or "weak"),
                        "missing": missing[:8],
                        "reason": str(v.get("reason") or "")[:200],
                        "narration": (cue_text.get(
                            (cue_range(s.get("cues")) or (0, 0))[0], ""))[:200]}
                if shot_reveal(s):
                    entry["reveal"] = shot_reveal(s)
                    entry["reveal_layout"] = shot_reveal_layout(s)
                if s.get("reveal_item"):
                    entry["reveal_item"] = s["reveal_item"]
                weak.append(entry)
                verdicts[asset] = entry
    return {"matched": matched, "weak": weak, "verdicts": verdicts,
            "unreviewed": unreviewed, "error": error}


def review_shotlist(cfg, data: dict, cues: list[dict], provider: str | None,
                    chunk_size: int = 20,
                    max_hold_seconds: float = SHOT_MAX_HOLD_DEFAULT,
                    style_guide: str = "", temperature: float = 1.0,
                    profile: "briefs.MotionProfile | None" = None) -> dict:
    """Structural faults + a per-scene prompt-completeness audit. Returns
    {faults, matched, total, weak, ratio, error, unreviewed, warnings,
    verdicts}. Never raises.

    `matched` counts shots whose prompt carries everything its cues require;
    `weak` lists the rest with the elements they fail to specify. `verdicts`
    maps every actually-judged asset to its verdict, for review_shotlist_patch
    to carry forward on a later patch attempt. `unreviewed` counts shots
    whose judge call itself failed (network/provider error) - those are
    excluded from BOTH `matched` and `weak`: they were never actually
    graded, so neither counting them as passed nor telling the next attempt
    to rewrite them would be honest. A caller should treat
    `unreviewed == total` (nothing could be judged at all) very differently
    from a partial `unreviewed` count mixed with real weak/matched verdicts."""
    faults = shotlist_structural_faults(data, len(cues))
    # Pacing is checked HERE, before any image renders: too few images for the
    # narration is far cheaper to fix in the plan than after 45 generations.
    pacing_faults, pacing_warnings = shotlist_pacing(
        data, cues, max_hold_seconds, profile)
    faults = faults + pacing_faults
    cue_text = {c["index"]: c["text"] for c in cues}
    prompt_by_file = {str(i.get("file")): i.get("prompt")
                      for i in (data.get("images") or [])
                      if isinstance(i, dict)}
    shots = judge_shots(data, prompt_by_file)
    total = len(shots)
    if not shots:
        return {"faults": faults, "matched": 0, "total": 0, "weak": [],
                "ratio": 0.0, "error": None, "unreviewed": 0,
                "warnings": pacing_warnings, "verdicts": {}}
    result = _judge_shot_chunks(cfg, shots, cue_text, provider, chunk_size,
                                style_guide, temperature)
    ratio = (result["matched"] / total) if total else 0.0
    return {"faults": faults, "matched": result["matched"], "total": total,
            "weak": result["weak"][:30], "ratio": ratio,
            "error": result["error"], "unreviewed": result["unreviewed"],
            "warnings": pacing_warnings, "verdicts": result["verdicts"]}


def review_shotlist_patch(cfg, data: dict, cues: list[dict],
                          provider: str | None, prior_verdicts: dict,
                          patched_assets: set,
                          chunk_size: int = 20,
                          max_hold_seconds: float = SHOT_MAX_HOLD_DEFAULT,
                          style_guide: str = "", temperature: float = 1.0,
                          profile: "briefs.MotionProfile | None" = None
                          ) -> dict:
    """Same shape and contract as review_shotlist, but only re-runs the judge
    on `patched_assets` - every other shot's alignment verdict is carried
    forward from `prior_verdicts` (the previous attempt's per-asset
    verdicts) unchanged.

    Patch mode (_run_shots) only ever rewrites the specific prompts the last
    review flagged as weak; every shot it did NOT touch already has a fresh
    verdict from that same review, so re-judging the whole plan again on
    every patch attempt was pure waste - on a 100+ shot plan, patching a
    handful of prompts still meant re-running the judge over every one of
    them for zero new information. A shot with no prior verdict (should not
    normally happen - patch mode never adds or removes shots - but handled
    defensively) is judged fresh rather than assumed to pass.

    Faults and pacing are still recomputed from scratch every time: they are
    free (no LLM call) and a patch could in principle create a new duplicate
    prompt or similar, so there is no saving worth the risk in skipping them."""
    faults = shotlist_structural_faults(data, len(cues))
    pacing_faults, pacing_warnings = shotlist_pacing(
        data, cues, max_hold_seconds, profile)
    faults = faults + pacing_faults
    cue_text = {c["index"]: c["text"] for c in cues}
    prompt_by_file = {str(i.get("file")): i.get("prompt")
                      for i in (data.get("images") or [])
                      if isinstance(i, dict)}
    shots = judge_shots(data, prompt_by_file)
    total = len(shots)
    if not shots:
        return {"faults": faults, "matched": 0, "total": 0, "weak": [],
                "ratio": 0.0, "error": None, "unreviewed": 0,
                "warnings": pacing_warnings, "verdicts": {}}
    to_judge_assets = {str(s.get("asset")) for s in shots
                       if str(s.get("asset")) in patched_assets
                       or str(s.get("asset")) not in prior_verdicts}
    to_judge = [s for s in shots if str(s.get("asset")) in to_judge_assets]
    carried = [s for s in shots if str(s.get("asset")) not in to_judge_assets]
    fresh = (_judge_shot_chunks(cfg, to_judge, cue_text, provider, chunk_size,
                                style_guide, temperature) if to_judge else
            {"matched": 0, "weak": [], "verdicts": {}, "unreviewed": 0,
             "error": None})
    matched = fresh["matched"]
    weak = list(fresh["weak"])
    verdicts = dict(fresh["verdicts"])
    for s in carried:
        asset = str(s.get("asset"))
        entry = prior_verdicts[asset]
        verdicts[asset] = entry
        if entry.get("verdict") == "ok":
            matched += 1
        else:
            weak.append(entry)
    ratio = (matched / total) if total else 0.0
    return {"faults": faults, "matched": matched, "total": total,
            "weak": weak[:30], "ratio": ratio, "error": fresh["error"],
            "unreviewed": fresh["unreviewed"], "warnings": pacing_warnings,
            "verdicts": verdicts}


# --------------------------------------------------- external tool hooks ---

def run_hook(command: str, subs: dict, timeout: int = 3600,
             env: dict | None = None) -> None:
    """Run a configured external command, substituting {placeholders}.
    Every value is command-line quoted, so working folders or filenames
    containing spaces, & , ^ or % cannot inject extra commands.

    `env` replaces the child's environment when given (used to hand a hook a
    secret resolved from the dashboard settings, e.g. the AI33 API key)."""
    cmd = command
    for key, val in subs.items():
        cmd = cmd.replace("{" + key + "}",
                          subprocess.list2cmdline([str(val)]))
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                            timeout=timeout, env=env)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "")[-400:]
        raise RuntimeError(f"command failed (exit {result.returncode}): {tail}")
