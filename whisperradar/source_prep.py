"""Getting the source transcript for a production made from a research video.

A research video that was never downloaded has no transcript, so there is
nothing to compare the plan/script with and no points to take. "Make this"
starts this job in its own slot (not the dashboard's, not a channel's): it
downloads the audio, transcribes it, and copies the text into every
production waiting on that video. The production's auto-run waits for it.
"""
from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path

from . import db, pipeline, studio

log = logging.getLogger(__name__)

# a video in one of these states is on its way to a transcript
_BUSY = ("new", "queued", "downloading", "downloaded", "transcribing")


def _video_transcript(row) -> Path | None:
    if row and row["transcript_path"] and Path(row["transcript_path"]).exists():
        return Path(row["transcript_path"])
    return None


def sync_transcript(cfg, pid: int) -> bool:
    """Copy the source video's transcript into the production if it exists
    now and the production does not have it yet. True = the production has it."""
    pdir = studio.prod_dir(cfg, pid)
    if studio.find_source_transcript(pdir):
        return True
    conn = db.connect(cfg.db_path)
    try:
        prod = db.get_production(conn, pid)
        row = (db.get_video(conn, prod["source_video_id"])
               if prod and prod["source_video_id"] else None)
    finally:
        conn.close()
    src = _video_transcript(row)
    if not src:
        return False
    shutil.copy(src, pdir / "source_transcript.txt")
    return True


def video_id_active(vid) -> bool:
    return vid in _active


def state(cfg, pid: int) -> dict | None:
    """None when the production needs no wait (no source video, or its
    transcript is in place). Else {"state": "waiting"|"error"|"idle", "text"}."""
    conn = db.connect(cfg.db_path)
    try:
        prod = db.get_production(conn, pid)
        vid = prod["source_video_id"] if prod else None
        row = db.get_video(conn, vid) if vid else None
    finally:
        conn.close()
    if not vid or not row:
        return None
    if sync_transcript(cfg, pid):
        return None
    status = row["status"]
    if video_id_active(vid):
        return {"state": "waiting", "video_id": vid,
                "text": "waiting for the source transcript "
                        "(download + transcription running)"}
    if vid in _failed:
        return {"state": "error", "video_id": vid,
                "text": f"the source transcript failed: {_failed[vid]}"}
    if status == "error":
        return {"state": "error", "video_id": vid,
                "text": f"the source transcript failed: {row['error'] or 'unknown error'}"}
    if status in _BUSY:
        return {"state": "waiting", "video_id": vid,
                "text": f"waiting for the source transcript ({status})"}
    return {"state": "idle", "video_id": vid,
            "text": "the source video has no transcript yet"}


def prepare(cfg, video_id: str, log_fn=None) -> None:
    """Download the audio if needed, transcribe, then hand the text to the
    productions waiting on this video. Raises when a step fails."""
    say = log_fn or (lambda m: None)
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        row = db.get_video(conn, video_id)
        if not row:
            raise RuntimeError("source video not found")
        if not _video_transcript(row):
            if not row["audio_path"] or not Path(row["audio_path"]).exists():
                say(f"source: downloading audio for {row['title']}")
                db.set_video(conn, video_id, status="new", auto=1, error=None)
                pipeline.process_downloads(cfg, conn, video_ids=[video_id])
                row = db.get_video(conn, video_id)
                if row["status"] == "error":
                    raise RuntimeError(row["error"] or "download failed")
            else:
                db.set_video(conn, video_id, status="downloaded", error=None)
            say("source: transcribing")
            pipeline.process_transcripts(cfg, conn, video_ids=[video_id])
            row = db.get_video(conn, video_id)
            if row["status"] == "error" or not _video_transcript(row):
                raise RuntimeError(row["error"] or "transcription failed")
        waiting = [p["id"] for p in db.list_productions(conn)
                   if p["source_video_id"] == video_id]
    finally:
        conn.close()
    for pid in waiting:
        sync_transcript(cfg, pid)
    say("source: transcript ready")


# ---- the job: its own thread, apart from the dashboard and the channel slots
_RUN_LOCK = threading.Lock()          # one source at a time, in click order
_active: set[str] = set()             # video ids queued or running
_failed: dict[str, str] = {}          # video id -> last error (cleared on retry)
_mutex = threading.Lock()


def start_background(cfg, video_id: str, log_fn=None) -> bool:
    """Queue the source transcript for a video. False when it is already
    queued/running or already has its transcript."""
    with _mutex:
        if video_id in _active:
            return False
        _active.add(video_id)
        _failed.pop(video_id, None)

    def worker():
        try:
            with _RUN_LOCK:
                prepare(cfg, video_id, log_fn)
        except Exception as exc:                      # shown on the production
            log.error("source transcript for %s failed: %s", video_id, exc)
            _failed[video_id] = str(exc)
        finally:
            with _mutex:
                _active.discard(video_id)

    threading.Thread(target=worker, daemon=True,
                     name=f"source-{video_id}").start()
    return True


def is_active(video_id: str) -> bool:
    return video_id in _active
