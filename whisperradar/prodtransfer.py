"""Export a production to one zip and import it as a new production.

What travels: the text of the production (script, style, bible, research,
subtitles), the plans (elements, beats, shotlist, packaging), the render
options and the production's own settings. What does not: images (generated
or thumbnails), renders, the Flow job files (rebuilt from the shotlist when
Flow runs), chat logs and anything machine specific. Audio and the small
reference images are optional.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import db, prodfiles, studio

FORMAT = "whisperradar-production"
VERSION = 1
MANIFEST = "production.json"
# the production's own settings (db columns); nothing that points at a
# channel, a video, a Flow project or a folder on this machine
FIELDS = ("title", "genre", "notes", "extra_prompt", "stage", "llm_provider",
          "stage_extras", "stage_providers", "render_mode", "voice",
          "style_override", "bible_override", "style_variant", "visual_density",
          "handover", "opening_config")
ROOT_FILES = ("script.md", "style.md", "bible.md", "writing_style.md",
              "research_notes.json", "research_notes.md", "source_transcript.txt",
              "source_analysis.json", "script_readiness.json", "subtitles.srt",
              "shotlist.json", "beats.json", "elements.json", "elements.md",
              "elements_source.json", "visual_manifest.json", "imgtovideo.json",
              "imgtovideo.wr.json", "explainer.json", "motion_gate.json")
PACKAGING_FILES = ("packaging_plan.json", "publish_kit.json", "title.txt",
                   "titles.txt", "description.txt")
MAX_TEXT = 25 * 1024 * 1024
MAX_AUDIO = 400 * 1024 * 1024
MAX_REF = 10 * 1024 * 1024
MAX_TOTAL = 600 * 1024 * 1024
REF_EXTS = (".png", ".jpg", ".jpeg", ".webp")


class TransferError(Exception):
    pass


def export_production(cfg, pid: int, *, audio: bool = False, refs: bool = False) -> tuple[bytes, str]:
    """(zip bytes, file name) for production `pid`."""
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        prod = db.get_production(conn, pid)
        if prod is None:
            raise TransferError("Unknown production")
        fields = {k: prod[k] for k in FIELDS if k in prod.keys()}
        channel = None
        if prod["own_channel_id"]:
            oc = db.get_own_channel(conn, prod["own_channel_id"])
            channel = oc["name"] if oc else None
    finally:
        conn.close()
    pdir = studio.prod_dir(cfg, pid)
    buf = io.BytesIO()
    included: list[str] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        def add(src: Path, name: str, limit: int) -> None:
            if src.is_file() and src.stat().st_size <= limit:
                zf.write(src, name)
                included.append(name)

        for name in ROOT_FILES:
            add(pdir / name, name, MAX_TEXT)
        for name in PACKAGING_FILES:
            add(prodfiles.path(pdir, name), f"packaging/{name}", MAX_TEXT)
        versions = pdir / "versions"
        if versions.is_dir():
            for f in sorted(versions.rglob("*")):
                if f.is_file() and f.suffix.lower() in (".md", ".txt", ".json"):
                    add(f, "versions/" + f.relative_to(versions).as_posix(), MAX_TEXT)
        if audio:
            found = studio.find_audio(pdir)
            if found:
                add(found, f"audio{found.suffix.lower()}", MAX_AUDIO)
        if refs and (pdir / "refs").is_dir():
            for f in sorted((pdir / "refs").iterdir()):
                if f.suffix.lower() in REF_EXTS:
                    add(f, f"refs/{f.name}", MAX_REF)
        manifest = {"format": FORMAT, "version": VERSION,
                    "exported_at": datetime.now().isoformat(timespec="seconds"),
                    "source_id": pid, "channel": channel, "fields": fields,
                    "has_audio": any(n.startswith("audio.") for n in included),
                    "has_refs": any(n.startswith("refs/") for n in included)}
        zf.writestr(MANIFEST, json.dumps(manifest, indent=1, ensure_ascii=False))
    stem = studio.safe_folder_name(fields.get("title") or f"production {pid}")
    return buf.getvalue(), f"{stem}.wrprod.zip"


def _allowed(name: str) -> str | None:
    """The cleaned relative name when it is a file an export can contain."""
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or not p.parts or name.endswith("/"):
        return None
    s = p.as_posix()
    if s == MANIFEST:
        return s
    if s in ROOT_FILES:
        return s
    if len(p.parts) == 2 and p.parts[0] == "packaging" and p.parts[1] in PACKAGING_FILES:
        return s
    if p.parts[0] == "versions" and p.suffix.lower() in (".md", ".txt", ".json"):
        return s
    if len(p.parts) == 1 and re.fullmatch(r"audio\.(mp3|wav|m4a|flac|ogg)", s):
        return s
    if len(p.parts) == 2 and p.parts[0] == "refs" and p.suffix.lower() in REF_EXTS:
        return s
    return None


def read_manifest(data: bytes) -> dict:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            manifest = json.loads(zf.read(MANIFEST).decode("utf-8"))
    except (zipfile.BadZipFile, KeyError, ValueError):
        raise TransferError("This is not a WhisperRadar production export")
    if manifest.get("format") != FORMAT:
        raise TransferError("This is not a WhisperRadar production export")
    if int(manifest.get("version") or 0) > VERSION:
        raise TransferError("This export comes from a newer version of WhisperRadar")
    return manifest


def _stage_after_import(stage: str, has_refs: bool) -> str:
    """Images never travel, so a production that was past the image stages
    starts again where the pictures are made."""
    order = list(db.STAGES)
    limit = "images" if has_refs else "refs"
    if stage not in order:
        return "style"
    return stage if order.index(stage) <= order.index(limit) else limit


def import_production(cfg, data: bytes, own_channel_id=None, title: str | None = None) -> int:
    """Create a new production from an export. Returns its id."""
    manifest = read_manifest(data)
    fields = dict(manifest.get("fields") or {})
    new_title = (title or fields.get("title") or "Imported production").strip()[:300]
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        members = []
        total = 0
        for info in zf.infolist():
            clean = _allowed(info.filename)
            if clean is None or info.is_dir():
                continue
            limit = (MAX_AUDIO if clean.startswith("audio.") else
                     MAX_REF if clean.startswith("refs/") else MAX_TEXT)
            if info.file_size > limit:
                raise TransferError(f"{clean} is too large")
            total += info.file_size
            members.append((info, clean))
        if total > MAX_TOTAL:
            raise TransferError("The export is too large")

        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            channel_name = None
            oc = None
            if own_channel_id:
                oc = db.get_own_channel(conn, own_channel_id)
            if oc is None and manifest.get("channel"):
                oc = db.get_own_channel(conn, manifest["channel"])
            if oc is not None:
                channel_name = oc["name"]
            work_dir = studio.auto_work_dir(cfg, conn, new_title, channel_name)
            pid = db.create_production(conn, new_title, fields.get("genre") or "general",
                                       None, work_dir)
            keep = {k: fields[k] for k in FIELDS
                    if k in fields and k not in ("title", "stage") and fields[k] is not None}
            if oc is not None:
                keep["own_channel_id"] = oc["id"]
                if not fields.get("genre"):
                    keep["genre"] = oc["genre"]
            if keep:
                db.update_production(conn, pid, **keep)
            has_refs = any(c.startswith("refs/") for _, c in members)
            db.update_production(conn, pid, stage=_stage_after_import(
                str(fields.get("stage") or "style"), has_refs))
        finally:
            conn.close()
        pdir = studio.prod_dir(cfg, pid)
        for info, clean in members:
            if clean == MANIFEST:
                continue
            dst = pdir / clean
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(zf.read(info))
    return pid
