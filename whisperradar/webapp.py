"""Local web dashboard for WhisperRadar.

Start with:  python wr.py serve          (http://127.0.0.1:8000)
"""

import io
import logging
import threading
import zipfile
from collections import deque
from pathlib import Path
from urllib.parse import quote

from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    request,
    send_file,
)

from . import db, pipeline
from .cli import _slugify, format_duration
from .watch import CHANNEL_ID_RE, resolve_channel


def _back(request, msg: str | None = None, error: str | None = None):
    """Redirect back to the dashboard preserving status/genre/channel filters.

    Filter values come from hidden fields the POST forms carry.
    """
    parts = []
    for key in ("status", "genre", "channel"):
        val = request.form.get(key)
        if val:
            parts.append(f"{key}={quote(val)}")
    if msg:
        parts.append(f"msg={quote(msg)}")
    if error:
        parts.append(f"error={quote(error)}")
    return redirect("/" + ("?" + "&".join(parts) if parts else ""))


class _Job:
    """Tracks a pipeline job running in a background thread."""

    def __init__(self):
        self.running = False
        self.kind = ""
        self.log: deque = deque(maxlen=400)
        self._lock = threading.Lock()

    def start(self, fn, kind: str) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.kind = kind
        self.log.clear()
        self.log.append(f"=== {kind}: started ===")

        def worker():
            try:
                fn()
                self.log.append(f"=== {kind}: finished ===")
            except Exception as exc:
                self.log.append(f"=== {kind}: FAILED: {exc} ===")
            finally:
                self.running = False

        threading.Thread(target=worker, daemon=True).start()
        return True


def _stage(func, cfg):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        func(cfg, conn)
    finally:
        conn.close()


def _download_one(cfg, video_id: str):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        db.set_video(conn, video_id, status="new", auto=1, error=None)
        pipeline.process_downloads(cfg, conn, video_ids=[video_id])
    finally:
        conn.close()


def _transcribe_one(cfg, video_id: str):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        row = db.get_video(conn, video_id)
        if row and row["audio_path"]:
            db.set_video(conn, video_id, status="downloaded", error=None)
            pipeline.process_transcripts(cfg, conn, video_ids=[video_id])
    finally:
        conn.close()


PAGE_SIZE = 50


def _page_list(current: int, total: int) -> list:
    """Page numbers to display; None renders as an ellipsis."""
    if total <= 9:
        return list(range(1, total + 1))
    wanted = {1, total, current - 1, current, current + 1}
    out: list = []
    for p in range(1, total + 1):
        if p in wanted:
            out.append(p)
        elif out and out[-1] is not None:
            out.append(None)
    return out


def create_app(cfg) -> Flask:
    app = Flask(__name__)
    app.jinja_env.filters["dur"] = format_duration
    job = _Job()

    class _DequeHandler(logging.Handler):
        def emit(self, record):
            job.log.append(record.getMessage())

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, _DequeHandler) for h in root.handlers):
        root.addHandler(_DequeHandler(level=logging.INFO))

    @app.get("/")
    def index():
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        db.sync_channels(conn, cfg.channels)
        channels = db.list_channels(conn)
        status = request.args.get("status") or None
        backlog = status == "backlog"
        if backlog:
            status = None
        genre = request.args.get("genre") or None
        channel = request.args.get("channel") or None
        page = max(1, int(request.args.get("page") or 1))
        total = db.count_videos(conn, status=status, genre=genre,
                                backlog=backlog, channel=channel)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages)
        videos = db.get_videos(conn, status=status, genre=genre,
                               backlog=backlog, channel=channel,
                               limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
        genres = sorted({ch["genre"] for ch in channels})
        conn.close()
        raw_status = request.args.get("status")

        def qs(**overrides) -> str:
            """Query string preserving current filters; overrides replace them."""
            vals = {"status": raw_status, "genre": genre, "channel": channel}
            page_override = overrides.pop("page", None)
            vals.update(overrides)
            parts = [f"{k}={quote(v)}" for k, v in vals.items() if v]
            if page_override:
                parts.append(f"page={page_override}")
            return "&".join(parts)

        return render_template(
            "dashboard.html",
            channels=channels,
            videos=videos,
            genres=genres,
            status=raw_status,
            genre=genre,
            channel=channel,
            page=page,
            pages=pages,
            total=total,
            page_list=_page_list(page, pages),
            qs=qs,
            msg=request.args.get("msg"),
            error=request.args.get("error"),
            job=job,
            log_text="\n".join(list(job.log))[-4000:],
        )

    @app.post("/channels/edit")
    def channels_edit():
        channel_id = request.form.get("channel_id") or ""
        name = (request.form.get("name") or "").strip()
        genre = (request.form.get("genre") or "").strip() or "general"
        kind = request.form.get("kind") or "primary"
        active = 1 if request.form.get("active") == "1" else 0
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            row = db.get_channel(conn, channel_id)
            if not row:
                return redirect("/?error=Unknown+channel")
            db.update_channel(conn, channel_id, name=name or row["name"],
                              kind=kind, genre=genre, active=active)
            if row["genre"] != genre:
                pipeline.move_channel_files(cfg, conn, row, genre)
        finally:
            conn.close()
        return _back(request, msg="Channel updated")

    @app.get("/status")
    def status():
        return {
            "running": job.running,
            "kind": job.kind,
            "log": list(job.log)[-80:],
        }

    @app.post("/channels/add")
    def channels_add():
        raw = (request.form.get("url") or "").strip()
        name = (request.form.get("name") or "").strip()
        kind = request.form.get("kind") or "primary"
        genre = (request.form.get("genre") or "").strip() or "general"
        if not raw:
            return redirect("/?error=Enter+a+channel+URL")
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            if CHANNEL_ID_RE.match(raw):
                channel_id, resolved_name = raw, ""
            else:
                channel_id, resolved_name = resolve_channel(raw)
            db.add_channel(
                conn,
                name=name or resolved_name or f"channel-{channel_id[:8]}",
                channel_id=channel_id,
                kind=kind,
                genre=genre,
            )
            return _back(request, msg="Channel added")
        except Exception as exc:
            return _back(request, error=f"Could not resolve channel: {exc}")
        finally:
            conn.close()

    @app.post("/channels/remove")
    def channels_remove():
        key = (request.form.get("key") or "").strip()
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            db.remove_channel(conn, key)
        finally:
            conn.close()
        return redirect("/?msg=Channel+removed")

    @app.post("/run")
    def run():
        kind = request.form.get("kind") or "run"
        jobs = {
            "run": lambda: pipeline.run_all(cfg),
            "watch": lambda: _stage(pipeline.refresh_feeds, cfg),
            "download": lambda: _stage(pipeline.process_downloads, cfg),
            "transcribe": lambda: _stage(pipeline.process_transcripts, cfg),
        }
        if kind not in jobs:
            return _back(request, error="Unknown job")
        if not job.start(jobs[kind], kind):
            return _back(request, error="A job is already running")
        return _back(request)

    @app.post("/videos/action")
    def videos_action():
        video_id = request.form.get("video_id") or ""
        action = request.form.get("action") or ""
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            row = db.get_video(conn, video_id)
            if not row:
                return _back(request, error="Unknown video")
            if action == "queue":
                db.set_video(conn, video_id, auto=1, status="new", error=None)
            elif action == "retry":
                if row["audio_path"]:
                    db.set_video(conn, video_id, status="downloaded", error=None)
                else:
                    db.set_video(conn, video_id, status="new", error=None)
            elif action == "download":
                if not job.start(lambda: _download_one(cfg, video_id), "download"):
                    return _back(request, error="A job is already running")
            elif action == "transcribe":
                if not row["audio_path"]:
                    return _back(request, error="Audio not downloaded yet")
                if not job.start(lambda: _transcribe_one(cfg, video_id), "transcribe"):
                    return _back(request, error="A job is already running")
            else:
                return _back(request, error="Unknown action")
        finally:
            conn.close()
        return _back(request, msg="Done")

    @app.post("/videos/delete")
    def videos_delete():
        video_id = request.form.get("video_id") or ""
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        row = None
        try:
            row = db.get_video(conn, video_id)
            if row:
                db.delete_video(conn, video_id)
        finally:
            conn.close()
        if row:
            for key in ("audio_path", "transcript_path"):
                path = row[key]
                if path:
                    Path(path).unlink(missing_ok=True)
        return _back(request, msg="Video deleted")
    @app.get("/transcript/<video_id>")
    def transcript(video_id):
        conn = db.connect(cfg.db_path)
        row = db.get_video(conn, video_id)
        conn.close()
        if not row or not row["transcript_path"]:
            abort(404)
        path = Path(row["transcript_path"])
        if not path.exists():
            abort(404)
        text = path.read_text(encoding="utf-8")
        return render_template("transcript.html", row=row, text=text)

    @app.get("/file/<video_id>")
    def file_download(video_id):
        conn = db.connect(cfg.db_path)
        row = db.get_video(conn, video_id)
        conn.close()
        if not row or not row["transcript_path"]:
            abort(404)
        path = Path(row["transcript_path"])
        if not path.exists():
            abort(404)
        return send_file(
            path,
            as_attachment=True,
            download_name=f"{_slugify(row['title'])}.txt",
            mimetype="text/plain",
        )

    @app.get("/export_all")
    def export_all():
        genre = request.args.get("genre") or None
        channel = request.args.get("channel") or None
        conn = db.connect(cfg.db_path)
        try:
            rows = db.get_videos(conn, status="transcribed", genre=genre,
                                 channel=channel)
        finally:
            conn.close()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for row in rows:
                path = Path(row["transcript_path"]) if row["transcript_path"] else None
                if path and path.exists():
                    zf.write(path, f"{_slugify(row['title'])}__{row['video_id']}.txt")
        buf.seek(0)
        suffix = ""
        if genre:
            suffix += f"_{genre}"
        if channel:
            suffix += f"_{_slugify(channel)[:30]}"
        return send_file(
            buf,
            as_attachment=True,
            download_name=f"whisperradar_transcripts{suffix}.zip",
            mimetype="application/zip",
        )

    return app
