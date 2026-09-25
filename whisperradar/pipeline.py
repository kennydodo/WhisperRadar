"""Pipeline orchestration: refresh feeds -> download audio -> transcribe."""

import logging
import shutil
from pathlib import Path

from . import db, download, transcribe, watch

log = logging.getLogger("whisperradar")


def move_channel_files(cfg, conn, channel_row, new_genre: str) -> int:
    """Move a channel's audio/transcript files into its (new) genre folder
    and update the stored paths. Returns number of files moved."""
    old_genre = channel_row["genre"] or "general"
    new_genre = new_genre or "general"
    if old_genre == new_genre:
        return 0
    moved = 0
    videos = conn.execute(
        "SELECT * FROM videos WHERE channel_id = ?",
        (channel_row["channel_id"],),
    ).fetchall()
    for video in videos:
        for col, base_dir in (("audio_path", cfg.audio_dir),
                              ("transcript_path", cfg.transcripts_dir)):
            path = video[col]
            if not path:
                continue
            src = Path(path)
            dest_dir = base_dir / new_genre
            dest = dest_dir / src.name
            if src.exists() and src.resolve() != dest.resolve():
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
                moved += 1
            db.set_video(conn, video["video_id"], **{col: str(dest)})
    if moved:
        log.info("moved %d file(s) from '%s' to '%s'", moved, old_genre, new_genre)
    return moved


def import_history(cfg, conn, channel_row, limit: int | None = None) -> int:
    """Import a channel's full upload history into the backlog.

    The RSS feed only lists the latest 15 uploads, so a newly added channel
    would otherwise never see its older videos. New rows land in the backlog
    (auto=0) - nothing is downloaded until it is explicitly queued. Existing
    rows are left untouched. Returns the number of new rows."""
    channel_id = channel_row["channel_id"]
    videos = watch.fetch_channel_videos_full(
        channel_id, limit=limit,
        cookies_from_browser=getattr(cfg, "cookies_from_browser", None),
    )
    added = db.upsert_videos(conn, channel_id, videos, auto=0)
    # INSERT OR IGNORE leaves existing rows alone, so refresh their view counts
    db.set_view_counts(conn, videos)
    log.info("history %s: %d video(s) found, %d new (backlog)",
             channel_row["name"], len(videos), added)
    return added


def refresh_view_counts(cfg, conn, channel_row, limit: int | None = None) -> int:
    """Re-read a channel's history and update the stored view counts.

    View counts are not in the RSS feed, so they come from the yt-dlp listing
    (the same one the backfill uses). Returns the number of rows updated."""
    channel_id = channel_row["channel_id"]
    videos = watch.fetch_channel_videos_full(
        channel_id, limit=limit,
        cookies_from_browser=getattr(cfg, "cookies_from_browser", None),
    )
    updated = db.set_view_counts(conn, videos)
    log.info("views %s: %d count(s) updated", channel_row["name"], updated)
    return updated


def refresh_feeds(cfg, conn) -> int:
    """Check each active channel's feed; returns number of new videos."""
    new_total = 0
    channels = db.list_channels(conn, active_only=True)
    if not channels:
        log.warning("No channels configured. Add one with: python wr.py add <url>")
    for ch in channels:
        # first sync of a channel: existing uploads are backlog, not auto-queue.
        # similar channels never auto-download - their uploads always land in
        # backlog until explicitly queued.
        first_sync = conn.execute(
            "SELECT 1 FROM videos WHERE channel_id = ? LIMIT 1",
            (ch["channel_id"],),
        ).fetchone() is None
        try:
            videos = watch.fetch_channel_videos(ch["channel_id"])
        except Exception as exc:
            log.error("feed failed for %s: %s", ch["name"], exc)
            videos = []
        added = 0
        for video in videos:
            auto = 1 if (not first_sync and ch["kind"] != "similar") else 0
            if db.upsert_video(conn, ch["channel_id"], video, auto=auto):
                added += 1
                label = "backlog video" if auto == 0 else "new video"
                log.info("%s: [%s] %s", label, ch["name"], video["title"])
        if videos:
            log.info("feed %s: %d videos, %d new", ch["name"], len(videos), added)
        new_total += added
        # RSS only lists the latest 15, so on a channel's first sync import the
        # rest of its uploads too (opt out with history_backfill: false). Run
        # after the RSS pass so the newest videos keep their RSS dates.
        if first_sync and getattr(cfg, "history_backfill", True):
            try:
                import_history(cfg, conn, ch,
                               limit=getattr(cfg, "history_backfill_limit", None))
            except Exception as exc:
                log.warning("history import failed for %s: %s - RSS only",
                            ch["name"], exc)
    return new_total


def process_downloads(cfg, conn, video_ids: list[str] | None = None) -> int:
    """Download audio for pending videos; returns success count.

    video_ids=None processes the whole auto queue; a list processes those
    videos explicitly (manual queueing).
    """
    if video_ids is None:
        pending = db.get_pending_downloads(conn)
    else:
        pending = [
            row
            for row in (db.get_video(conn, vid) for vid in video_ids)
            if row and row["status"] in ("new", "error")
        ]
    done = 0
    for video in pending:
        log.info("downloading: %s", video["title"])
        db.set_video(conn, video["video_id"], status="downloading", error=None)
        try:
            # keep channels separated on disk: audio/<genre>/
            audio_dir = cfg.audio_dir / (video["channel_genre"] or "general")
            audio_path, duration = download.download_audio(
                video["url"], audio_dir, cfg.cookies_from_browser
            )
            if (
                cfg.max_video_seconds
                and duration
                and duration > cfg.max_video_seconds
            ):
                db.set_video(
                    conn,
                    video["video_id"],
                    status="skipped",
                    duration=duration,
                    error=f"video longer than {cfg.max_video_seconds}s",
                )
                log.info("skipped (too long): %s", video["title"])
                continue
            db.set_video(
                conn,
                video["video_id"],
                status="downloaded",
                audio_path=str(audio_path),
                duration=duration,
                error=None,
            )
            done += 1
            log.info("downloaded: %s", video["title"])
        except Exception as exc:
            db.set_video(conn, video["video_id"], status="error", error=str(exc))
            log.error("download failed for %s: %s", video["title"], exc)
    return done


def process_transcripts(cfg, conn, video_ids: list[str] | None = None) -> int:
    """Transcribe downloaded videos; returns success count.

    video_ids=None processes everything pending; a list processes those
    videos explicitly (manual transcription / retries).
    """
    if video_ids is None:
        pending = db.get_pending_transcripts(conn)
    else:
        pending = [
            row
            for row in (db.get_video(conn, vid) for vid in video_ids)
            if row and row["status"] == "downloaded"
        ]
    done = 0
    for video in pending:
        log.info("transcribing: %s", video["title"])
        db.set_video(conn, video["video_id"], status="transcribing", error=None)
        try:
            audio_path = video["audio_path"]
            # keep channels separated on disk: transcripts/<genre>/
            out_txt = (cfg.transcripts_dir / (video["channel_genre"] or "general")
                       / f"{video['video_id']}.txt")
            meta = transcribe.transcribe_audio(
                audio_path,
                out_txt,
                model_size=cfg.whisper_model,
                language=cfg.whisper_language,
            )
            db.set_video(
                conn,
                video["video_id"],
                status="transcribed",
                transcript_path=str(out_txt),
                language=meta["language"],
                error=None,
            )
            done += 1
            log.info(
                "transcribed (%s, %.0fs audio): %s",
                meta["device"],
                meta["duration"] or 0,
                video["title"],
            )
        except Exception as exc:
            db.set_video(conn, video["video_id"], status="error", error=str(exc))
            log.error("transcription failed for %s: %s", video["title"], exc)
    return done


def run_all(cfg, retry: bool = False) -> dict:
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    db.sync_channels(conn, cfg.channels)
    run_id = db.start_run(conn)
    try:
        if retry:
            retried = db.reset_errors(conn)
            if retried:
                log.info("re-queued %d errored videos", retried)
        new_videos = refresh_feeds(cfg, conn)
        downloaded = process_downloads(cfg, conn)
        transcribed = process_transcripts(cfg, conn)
        failed = conn.execute(
            "SELECT COUNT(*) FROM videos WHERE status = 'error'"
        ).fetchone()[0]
        db.finish_run(
            conn,
            run_id,
            new_videos=new_videos,
            downloaded=downloaded,
            transcribed=transcribed,
            failed=failed,
        )
        return {
            "new_videos": new_videos,
            "downloaded": downloaded,
            "transcribed": transcribed,
            "failed": failed,
        }
    finally:
        conn.close()
