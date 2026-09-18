"""Local web dashboard for WhisperRadar.

Start with:  python wr.py serve          (http://127.0.0.1:8000)
"""

import io
import json
import logging
import re
import shutil
import threading
import time
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

from . import db, pipeline, studio, transcribe
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


def _studio_url(pid, msg: str | None = None, error: str | None = None):
    """Redirect back to the production page preserving the ?stage= being viewed.

    The stage comes from the referrer, so every studio action returns to the
    stage panel the user was working on instead of jumping to the current one.
    """
    parts = []
    m = re.search(r"[?&]stage=(\w+)", request.referrer or "")
    if m and m.group(1) in db.STAGES:
        parts.append(f"stage={m.group(1)}")
    if msg:
        parts.append(f"msg={quote(msg)}")
    if error:
        parts.append(f"error={quote(error)}")
    return redirect(f"/studio/{pid}" + ("?" + "&".join(parts) if parts else ""))


def _version_names(pdir: Path, kind: str,
                   stage: str | None = None) -> list[str]:
    """Named versions saved for a production (script / direction), newest first.

    Direction versions are per stage (versions/direction/<stage>/<name>.md);
    legacy flat files in versions/direction/ still show up.
    """
    base = pdir / "versions" / kind
    dirs = [base / stage] if stage else [base]
    if kind == "direction" and stage:
        dirs.append(base)  # legacy flat files from before per-stage directions
    names: list[str] = []
    for d in dirs:
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime,
                        reverse=True):
            if p.is_file() and p.stem not in names:
                names.append(p.stem)
    return names


def _version_path(pdir: Path, kind: str, stage: str, name: str) -> Path:
    """Resolve a version file path; direction versions live per stage."""
    sub = base = pdir / "versions" / kind
    if kind == "direction":
        sub = base / stage
    for d in (sub, base):
        f = d / f"{name}.md"
        if f.exists():
            return f
    return sub / f"{name}.md"


class _Job:
    """Tracks a pipeline job running in a background thread."""

    def __init__(self):
        self.running = False
        self.kind = ""
        self.error = None
        self.log: deque = deque(maxlen=400)
        self._lock = threading.Lock()

    def start(self, fn, kind: str) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.kind = kind
            self.error = None
        self.log.clear()
        self.log.append(f"=== {kind}: started ===")

        def worker():
            try:
                fn()
                self.log.append(f"=== {kind}: finished ===")
            except Exception as exc:
                self.error = str(exc)
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
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1 GB uploads
    app.config["TEMPLATES_AUTO_RELOAD"] = True  # local app: pick up edits live
    app.jinja_env.filters["dur"] = format_duration
    job = _Job()
    sjob = _Job()  # studio jobs (LLM generation, SRT alignment)

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

    # ------------------------------------------------------------- studio --

    def _source_transcript(prod) -> str:
        if not prod["source_video_id"]:
            return ""
        conn = db.connect(cfg.db_path)
        try:
            row = db.get_video(conn, prod["source_video_id"])
        finally:
            conn.close()
        if row and row["transcript_path"] and Path(row["transcript_path"]).exists():
            return Path(row["transcript_path"]).read_text(encoding="utf-8")
        return ""

    @app.get("/studio")
    def studio_list():
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        prods = []
        for p in db.list_productions(conn):
            steps = db.latest_steps(conn, p["id"])
            done = sum(1 for s in db.STAGES if s in steps)
            prods.append({"row": p, "done": done, "total": len(db.STAGES)})
        sources = db.get_videos(conn, status="transcribed", limit=500)
        conn.close()
        return render_template("studio.html", prods=prods, sources=sources,
                               job=sjob, msg=request.args.get("msg"),
                               error=request.args.get("error"))

    @app.post("/studio/new")
    def studio_new():
        title = (request.form.get("title") or "").strip()
        genre = (request.form.get("genre") or "").strip() or "general"
        source = (request.form.get("source_video_id") or "").strip() or None
        work_dir = (request.form.get("work_dir") or "").strip() or None
        if work_dir:
            work_dir = str(Path(work_dir).expanduser().resolve())
        if not title:
            return redirect("/studio?error=Enter+a+title")
        if work_dir:
            try:
                work_dir = str(studio.validate_work_dir(cfg, work_dir))
            except RuntimeError as exc:
                return redirect(f"/studio?error={quote(str(exc)[:150])}")
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            pid = db.create_production(conn, title, genre, source, work_dir)
            row = db.get_video(conn, source) if source else None
        finally:
            conn.close()
        if row and row["transcript_path"] and Path(row["transcript_path"]).exists():
            pdir = studio.prod_dir(cfg, pid)
            shutil.copy(row["transcript_path"], pdir / "source_transcript.txt")
        return redirect(f"/studio/{pid}")

    @app.get("/studio/<int:pid>")
    def studio_detail(pid):
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
            if not prod:
                abort(404)
            steps = db.latest_steps(conn, pid)
            history = db.step_history(conn, pid)
            source_video = (db.get_video(conn, prod["source_video_id"])
                            if prod["source_video_id"] else None)
        finally:
            conn.close()

        stage = request.args.get("stage") or prod["stage"]
        if stage not in db.STAGES:
            stage = prod["stage"]
        pdir = studio.prod_dir(cfg, pid)

        def _read_text(path: Path | None) -> str:
            # a background working-folder move can make files vanish between
            # the exists() check and the read - treat that as empty
            if not path:
                return ""
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                return ""

        script = studio.find_script(pdir)
        script_text = _read_text(script)
        style = studio.find_style(pdir)
        style_text = _read_text(style)
        source_tr = studio.find_source_transcript(pdir)
        if not source_tr and prod["source_video_id"]:
            conn = db.connect(cfg.db_path)
            try:
                row = db.get_video(conn, prod["source_video_id"])
            finally:
                conn.close()
            if row and row["transcript_path"] and Path(row["transcript_path"]).exists():
                shutil.copy(row["transcript_path"], pdir / "source_transcript.txt")
                source_tr = studio.find_source_transcript(pdir)
        shotlist = pdir / "shotlist.json"
        shotlist_text = _read_text(shotlist if shotlist.exists() else None)
        audio = studio.find_audio(pdir)
        srt = studio.find_srt(pdir)
        srt_text = _read_text(srt)
        prompts = studio.find_prompts(pdir)
        prompts_text = _read_text(prompts)
        shotlist_count = None
        if shotlist.exists():
            try:
                shotlist_count = len(
                    json.loads(shotlist_text).get("images", []))
            except ValueError:
                pass
        cue_count = len([b for b in srt_text.split("\n\n") if b.strip()])
        images = [i.name for i in studio.find_images(pdir)]
        final = studio.find_final(pdir)
        final_url = (f"/studio/file/{pid}/"
                     f"{final.relative_to(pdir).as_posix()}") if final else None

        providers = cfg.studio_llm_providers
        default_provider = prod["llm_provider"] or cfg.studio_llm_default
        if providers:
            llm_ready = any(studio.provider_ready(cfg, p["name"])
                            for p in providers)
            llm_label = studio.llm_label(cfg, default_provider)
        elif cfg.studio_llm == "openai":
            import os

            llm_ready = bool(cfg.studio_llm_model and
                             (cfg.studio_llm_api_key or
                              os.environ.get("WR_LLM_API_KEY")))
            llm_label = studio.llm_label(cfg)
        else:
            llm_ready = False
            llm_label = studio.llm_label(cfg)
        renderly_ready = studio.renderly_ready(cfg.renderly_url)
        hooks = {
            "tts": bool(cfg.studio_tts_command),
            "imagegen": bool(cfg.studio_imagegen_command),
            "merge": bool(cfg.studio_merge_command) or
                     bool(cfg.imgtovideo_repo),
        }
        return render_template(
            "studio_detail.html", prod=prod, steps=steps, history=history,
            stages=db.STAGES, stage=stage, script_text=script_text,
            style=style, style_text=style_text, source_tr=source_tr,
            shotlist_text=shotlist_text, audio=audio, srt=srt,
            srt_text=srt_text, prompts_text=prompts_text, images=images,
            final=final, final_url=final_url,
            source_video=source_video, llm_ready=llm_ready,
            llm_label=llm_label, providers=providers,
            default_provider=default_provider, hooks=hooks,
            renderly_ready=renderly_ready, work_dir=str(pdir), job=sjob,
            script_versions=_version_names(pdir, "script"),
            stage_direction=db.stage_extra(prod, stage),
            direction_versions=_version_names(pdir, "direction", stage),
            shotlist_count=shotlist_count, cue_count=cue_count,
            msg=request.args.get("msg"), error=request.args.get("error"),
        )

    @app.post("/studio/<int:pid>/delete")
    def studio_delete(pid):
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
            if not prod:
                return redirect("/studio?error=Unknown+production")
            pdir = studio.prod_dir(cfg, pid)  # resolve BEFORE the row is gone
            # only wipe folders WhisperRadar created or explicitly adopted
            managed = studio.is_managed_dir(cfg, pdir)
            db.delete_production(conn, pid)
        finally:
            conn.close()
        if managed:
            shutil.rmtree(pdir, ignore_errors=True)
            return redirect("/studio?msg=Production+deleted")
        return redirect(
            "/studio?error=" + quote(
                f"Production deleted - files kept at {pdir} "
                "(folder was not created by WhisperRadar)"))

    @app.post("/studio/<int:pid>/workdir")
    def studio_workdir(pid):
        if sjob.running:
            return _studio_url(pid, error="A job is already running")
        new_dir = (request.form.get("work_dir") or "").strip()
        if new_dir:
            try:
                studio.validate_work_dir(cfg, new_dir)
            except RuntimeError as exc:
                return _studio_url(pid, error=str(exc)[:150])
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
            if not prod:
                return redirect("/studio?error=Unknown+production")
        finally:
            conn.close()

        def worker():
            dest, moved = studio.move_production_dir(cfg, prod, new_dir or None)
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            try:
                db.update_production(
                    conn, pid, work_dir=str(dest) if new_dir else None)
            finally:
                conn.close()
            sjob.log.append(f"Working folder set to {dest} "
                            f"({moved} item(s) moved)")

        sjob.start(worker, "working-folder move")
        return _studio_url(pid, msg="Working folder move started")

    @app.post("/studio/<int:pid>/notes")
    def studio_notes(pid):
        notes = request.form.get("notes") or ""
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            db.update_production(conn, pid, notes=notes)
        finally:
            conn.close()
        return _studio_url(pid, msg="Notes saved")

    @app.post("/studio/<int:pid>/advance")
    def studio_advance(pid):
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
            if not prod:
                return redirect("/studio?error=Unknown+production")
            if not db.stage_done(conn, pid, prod["stage"]):
                return redirect(f"/studio/{pid}?error=Finish+the+current+stage+first")
            idx = db.STAGES.index(prod["stage"])
            if prod["stage"] == "review":
                db.update_production(conn, pid, status="ready")
                msg = "Approved - production is ready"
            else:
                db.update_production(conn, pid, stage=db.STAGES[idx + 1])
                msg = f"Advanced to {db.STAGES[idx + 1]}"
        finally:
            conn.close()
        return redirect(f"/studio/{pid}?msg={quote(msg)}")

    @app.post("/studio/<int:pid>/back")
    def studio_back(pid):
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
            if not prod:
                return redirect("/studio?error=Unknown+production")
            idx = db.STAGES.index(prod["stage"])
            if idx == 0:
                return redirect(f"/studio/{pid}?error=Already+at+the+first+stage")
            updates = {"stage": db.STAGES[idx - 1]}
            if prod["status"] == "ready":
                updates["status"] = "active"
            db.update_production(conn, pid, **updates)
        finally:
            conn.close()
        return redirect(f"/studio/{pid}?msg=Sent+back+for+rework")

    def _script_overlap(prod, script_text: str) -> float:
        return studio.overlap_ratio(script_text, _source_transcript(prod))

    @app.post("/studio/<int:pid>/script/save")
    def studio_script_save(pid):
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
            if not prod:
                return redirect("/studio?error=Unknown+production")
            pdir = studio.prod_dir(cfg, pid)
            text = (request.form.get("script") or "").strip()
            f = request.files.get("script_file")
            if f and f.filename:
                text = f.read().decode("utf-8", "ignore").strip()
            if not text:
                return _studio_url(pid, error="Nothing to save")
            (pdir / "script.md").write_text(text + "\n", encoding="utf-8")
            ratio = _script_overlap(prod, text)
            warn = " | WARNING: high overlap with source" if ratio > 0.2 else ""
            db.add_step(conn, pid, "script", "manual",
                        detail=f"overlap {ratio:.1%}{warn}")
        finally:
            conn.close()
        return _studio_url(pid, msg="Script saved")

    @app.post("/studio/<int:pid>/style/generate")
    def studio_style_generate(pid):
        if sjob.running:
            return _studio_url(pid, error="A job is already running")
        provider = request.form.get("provider") or cfg.studio_llm_default

        def worker():
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            try:
                prod = db.get_production(conn, pid)
                if not prod:
                    return
                source_text = _source_transcript(prod)
                if not source_text:
                    raise RuntimeError(
                        "No source transcript - write the style guide manually"
                    )
                word_count = len(re.findall(r"\w+", source_text))
                prompt = studio.style_prompt(prod["title"], prod["genre"],
                                             source_text,
                                             extra_direction=db.stage_extra(
                                                 prod, "style"))
                text = studio.llm_generate(cfg, prompt, provider=provider)
                if not text:
                    raise RuntimeError("LLM returned an empty style guide")
                pdir = studio.prod_dir(cfg, pid)
                (pdir / "style.md").write_text(text + "\n", encoding="utf-8")
                db.add_step(conn, pid, "style", "auto",
                            detail=f"{provider}, source ~{word_count} words")
            finally:
                conn.close()

        sjob.start(worker, "style analysis")
        return _studio_url(pid, msg="Style analysis started")

    @app.post("/studio/<int:pid>/style/save")
    def studio_style_save(pid):
        pdir = studio.prod_dir(cfg, pid)
        text = (request.form.get("style") or "").strip()
        f = request.files.get("style_file")
        if f and f.filename:
            text = f.read().decode("utf-8", "ignore").strip()
        if not text:
            return _studio_url(pid, error="Nothing to save")
        (pdir / "style.md").write_text(text + "\n", encoding="utf-8")
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            db.add_step(conn, pid, "style", "manual")
        finally:
            conn.close()
        return _studio_url(pid, msg="Style guide saved")

    @app.post("/studio/<int:pid>/script/generate")
    def studio_script_generate(pid):
        if sjob.running:
            return _studio_url(pid, error="A job is already running")

        provider = request.form.get("provider") or cfg.studio_llm_default
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
            if not prod:
                return redirect("/studio?error=Unknown+production")
            source_text = _source_transcript(prod)
        finally:
            conn.close()
        style = studio.find_style(studio.prod_dir(cfg, pid))
        style_guide = style.read_text(encoding="utf-8") if style else ""
        import re as _re

        source_words = len(_re.findall(r"\w+", source_text)) if source_text else 0
        target_words = cfg.studio_script_words or source_words or 1200
        variation = studio.variation_nudge()
        prompt = studio.script_prompt(prod["title"], prod["genre"],
                                      source_text, style_guide,
                                      target_words=target_words,
                                      variation=variation,
                                      extra_direction=db.stage_extra(
                                          prod, "script"))

        def worker():
            t0 = time.monotonic()
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            try:
                prod = db.get_production(conn, pid)
                text = studio.llm_generate(cfg, prompt, provider=provider)
                if not text:
                    raise RuntimeError("LLM returned an empty script")
                pdir = studio.prod_dir(cfg, pid)
                script_path = pdir / "script.md"
                if script_path.exists():
                    auto_dir = pdir / "versions" / "script"
                    auto_dir.mkdir(parents=True, exist_ok=True)
                    stamp = time.strftime("%Y%m%d-%H%M%S")
                    shutil.copy(script_path, auto_dir / f"auto-{stamp}.md")
                script_path.write_text(text + "\n", encoding="utf-8")
                ratio = _script_overlap(prod, text)
                warn = " | WARNING: high overlap with source" if ratio > 0.2 else ""
                db.update_production(conn, pid, llm_provider=provider)
                db.add_step(conn, pid, "script", "auto",
                            detail=f"{provider}, overlap {ratio:.1%}, "
                                   f"target {target_words} words, "
                                   f"took {format_duration(time.monotonic() - t0)}"
                                   f"{warn}")
            finally:
                conn.close()

        sjob.start(worker, "script generation")
        return _studio_url(pid, msg="Script generation started")

    def _save_upload(file_storage, dest: Path) -> None:
        file_storage.save(dest)

    @app.post("/studio/<int:pid>/audio/upload")
    def studio_audio_upload(pid):
        f = request.files.get("audio_file")
        if not f or not f.filename:
            return _studio_url(pid, error="No audio file selected")
        ext = Path(f.filename).suffix.lower()
        if ext not in studio.AUDIO_EXTS:
            ext = ".mp3"
        pdir = studio.prod_dir(cfg, pid)
        # never destroy a previous upload: archive all existing audio.* files
        from datetime import datetime

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        prev_dir = pdir / "audio_previous"
        for old in pdir.glob("audio.*"):
            if old.suffix.lower() in studio.AUDIO_EXTS:
                prev_dir.mkdir(exist_ok=True)
                shutil.move(old, prev_dir / f"{stamp}{old.suffix}")
        dest = pdir / f"audio{ext}"
        _save_upload(f, dest)
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            db.add_step(conn, pid, "audio", "manual", detail=dest.name)
        finally:
            conn.close()
        return _studio_url(pid, msg="Audio uploaded")

    @app.post("/studio/<int:pid>/audio/generate")
    def studio_audio_generate(pid):
        if not cfg.studio_tts_command:
            return _studio_url(pid, error="No tts_command in config.yaml")
        if sjob.running:
            return _studio_url(pid, error="A job is already running")

        def worker():
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            try:
                pdir = studio.prod_dir(cfg, pid)
                script = studio.find_script(pdir)
                if not script:
                    raise RuntimeError("Write the script first")
                out = pdir / "audio.mp3"
                studio.run_hook(cfg.studio_tts_command,
                                {"script": script, "out": out})
                audio = studio.find_audio(pdir)
                if not audio:
                    raise RuntimeError("TTS produced no audio file")
                db.add_step(conn, pid, "audio", "auto", detail=audio.name)
            finally:
                conn.close()

        sjob.start(worker, "tts")
        return _studio_url(pid, msg="TTS started")

    @app.post("/studio/<int:pid>/srt/generate")
    def studio_srt_generate(pid):
        if sjob.running:
            return _studio_url(pid, error="A job is already running")

        def worker():
            t0 = time.monotonic()
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            try:
                pdir = studio.prod_dir(cfg, pid)
                audio = studio.find_audio(pdir)
                if not audio:
                    raise RuntimeError("Upload or generate the audio first")
                meta = transcribe.transcribe_to_srt(
                    audio, pdir / "subtitles.srt",
                    model_size=cfg.whisper_model,
                    language=cfg.whisper_language,
                )
                db.add_step(
                    conn, pid, "srt", "auto",
                    detail=f"{meta['cues']} cues, {meta['device']}, "
                           f"lang {meta['language']}, "
                           f"took {format_duration(time.monotonic() - t0)}",
                )
            finally:
                conn.close()

        sjob.start(worker, "srt alignment")
        return _studio_url(pid, msg="SRT alignment started")

    @app.post("/studio/<int:pid>/srt/save")
    def studio_srt_save(pid):
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            pdir = studio.prod_dir(cfg, pid)
            text = (request.form.get("srt") or "").strip()
            f = request.files.get("srt_file")
            if f and f.filename:
                text = f.read().decode("utf-8", "ignore").strip()
            if not text:
                return _studio_url(pid, error="Nothing to save")
            (pdir / "subtitles.srt").write_text(text + "\n", encoding="utf-8")
            db.add_step(conn, pid, "srt", "manual")
        finally:
            conn.close()
        return _studio_url(pid, msg="Subtitles saved")

    @app.post("/studio/<int:pid>/prompts/generate")
    def studio_prompts_generate(pid):
        if sjob.running:
            return _studio_url(pid, error="A job is already running")

        provider = request.form.get("provider") or cfg.studio_llm_default

        def worker():
            t0 = time.monotonic()
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            try:
                prod = db.get_production(conn, pid)
                pdir = studio.prod_dir(cfg, pid)
                script = studio.find_script(pdir)
                if not script:
                    raise RuntimeError("Write the script first")
                style = studio.find_style(pdir)
                style_guide = style.read_text(encoding="utf-8") if style else ""
                prompt = studio.image_prompts_prompt(
                    script.read_text(encoding="utf-8"), prod["genre"],
                    style_guide, extra_direction=db.stage_extra(prod, "shots"))
                text = studio.llm_generate(cfg, prompt, provider=provider)
                lines = studio.parse_image_prompts(text)
                if not lines:
                    raise RuntimeError("LLM returned no image prompts")
                db.update_production(conn, pid, llm_provider=provider)
                (pdir / "prompts.txt").write_text(
                    "\n".join(lines) + "\n", encoding="utf-8")
                db.add_step(conn, pid, "prompts", "manual",
                            detail=f"{len(lines)} prompt(s) via LLM, "
                                   f"took {format_duration(time.monotonic() - t0)}")
            finally:
                conn.close()

        sjob.start(worker, "image prompts")
        return _studio_url(pid, msg="Image prompts started")

    @app.post("/studio/<int:pid>/prompts/save")
    def studio_prompts_save(pid):
        pdir = studio.prod_dir(cfg, pid)
        text = (request.form.get("prompts") or "").strip()
        if not text:
            return _studio_url(pid, error="Nothing to save")
        (pdir / "prompts.txt").write_text(text + "\n", encoding="utf-8")
        return _studio_url(pid, msg="Prompts saved")

    @app.post("/studio/<int:pid>/prompts/extract")
    def studio_prompts_extract(pid):
        """Write prompts.txt straight from the shotlist (no LLM call)."""
        pdir = studio.prod_dir(cfg, pid)
        if not (pdir / "shotlist.json").exists():
            return _studio_url(pid, error="Generate the shotlist first")
        try:
            prompts = studio.shotlist_prompts(pdir)
        except ValueError as exc:
            return _studio_url(pid, error=f"Shotlist is not valid JSON: {exc}")
        if not prompts:
            return _studio_url(pid, error="Shotlist has no prompts to extract")
        (pdir / "prompts.txt").write_text(
            "\n".join(prompts) + "\n", encoding="utf-8")
        return _studio_url(
            pid, msg=f"{len(prompts)} prompt(s) extracted from the shotlist")

    @app.post("/studio/<int:pid>/extra/save")
    def studio_extra_save(pid):
        stage = request.form.get("stage") or ""
        extra = (request.form.get("extra_prompt") or "").strip()
        if stage not in db.STAGES:
            return _studio_url(pid, error="Unknown stage")
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
            try:
                data = json.loads(prod["stage_extras"] or "{}")
            except (ValueError, TypeError):
                data = {}
            data = data if isinstance(data, dict) else {}
            data[stage] = extra
            db.update_production(conn, pid, stage_extras=json.dumps(data))
        finally:
            conn.close()
        return _studio_url(pid, msg=f"Direction for '{stage}' saved")

    @app.post("/studio/<int:pid>/versions/save")
    def studio_versions_save(pid):
        """Save the current script / direction under a user-chosen name."""
        kind = request.form.get("kind") or ""
        raw_name = (request.form.get("name") or "").strip()
        name = _slugify(raw_name, 40) if raw_name else ""
        stage = request.form.get("stage") or ""
        if kind == "script":
            text = (request.form.get("content") or
                    request.form.get("script") or "").strip()
        elif kind == "direction":
            text = (request.form.get("content") or
                    request.form.get("extra_prompt") or "").strip()
            if stage not in db.STAGES:
                return _studio_url(pid, error="Unknown stage")
        else:
            text = ""
        if kind not in ("script", "direction") or not name or not text:
            return _studio_url(pid,
                               error="A version needs a name and content")
        dest = _version_path(studio.prod_dir(cfg, pid), kind, stage, name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text + "\n", encoding="utf-8")
        return _studio_url(pid, msg=f"Version '{name}' saved")

    @app.post("/studio/<int:pid>/versions/load")
    def studio_versions_load(pid):
        kind = request.form.get("kind") or ""
        raw_name = (request.form.get("name") or "").strip()
        name = _slugify(raw_name, 40) if raw_name else ""
        stage = request.form.get("stage") or ""
        if kind == "direction" and stage not in db.STAGES:
            return _studio_url(pid, error="Unknown stage")
        f = _version_path(studio.prod_dir(cfg, pid), kind, stage, name)
        if kind not in ("script", "direction") or not name or not f.exists():
            return _studio_url(pid, error="Version not found")
        text = f.read_text(encoding="utf-8")
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            if kind == "script":
                (studio.prod_dir(cfg, pid) / "script.md").write_text(
                    text, encoding="utf-8")
                db.add_step(conn, pid, "script", "manual",
                            detail=f"loaded version '{name}'")
            else:
                prod = db.get_production(conn, pid)
                try:
                    data = json.loads(prod["stage_extras"] or "{}")
                except (ValueError, TypeError):
                    data = {}
                data = data if isinstance(data, dict) else {}
                data[stage] = text.strip()
                db.update_production(conn, pid, stage_extras=json.dumps(data))
        finally:
            conn.close()
        return _studio_url(pid, msg=f"Loaded version '{name}'")

    @app.post("/studio/<int:pid>/versions/delete")
    def studio_versions_delete(pid):
        kind = request.form.get("kind") or ""
        raw_name = (request.form.get("name") or "").strip()
        name = _slugify(raw_name, 40) if raw_name else ""
        stage = request.form.get("stage") or ""
        f = _version_path(studio.prod_dir(cfg, pid), kind, stage, name)
        if kind in ("script", "direction") and name and f.exists():
            f.unlink()
            return _studio_url(pid, msg=f"Version '{name}' deleted")
        return _studio_url(pid, error="Version not found")

    @app.post("/studio/<int:pid>/shotlist/generate")
    def studio_shotlist_generate(pid):
        if sjob.running:
            return _studio_url(pid, error="A job is already running")
        provider = request.form.get("provider") or cfg.studio_llm_default

        def worker():
            t0 = time.monotonic()
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            try:
                prod = db.get_production(conn, pid)
                pdir = studio.prod_dir(cfg, pid)
                srt = studio.find_srt(pdir)
                if not srt:
                    raise RuntimeError("Generate or upload the subtitles first")
                style = studio.find_style(pdir)
                style_guide = style.read_text(encoding="utf-8") if style else ""
                brief = studio.load_manifest_brief(cfg)
                prompt = studio.shotlist_prompt(
                    brief, srt.read_text(encoding="utf-8"), style_guide,
                    extra_direction=db.stage_extra(prod, "shots"))
                text = studio.llm_generate(cfg, prompt, provider=provider)
                data, sheet = studio.parse_shotlist_output(text)
                (pdir / "shotlist.json").write_text(
                    json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
                if sheet:
                    (pdir / "batch_sheet.txt").write_text(
                        sheet + "\n", encoding="utf-8")
                db.update_production(conn, pid, llm_provider=provider)
                db.add_step(conn, pid, "shots", "auto",
                            detail=f"{len(data.get('images', []))} image(s) in "
                                   f"{len(data.get('shots', []))} shot(s) via "
                                   f"manifest brief, took "
                                   f"{format_duration(time.monotonic() - t0)}")
            finally:
                conn.close()

        sjob.start(worker, "shotlist planning")
        return _studio_url(pid, msg="Shotlist planning started")

    @app.post("/studio/<int:pid>/shotlist/save")
    def studio_shotlist_save(pid):
        pdir = studio.prod_dir(cfg, pid)
        text = (request.form.get("shotlist") or "").strip()
        if not text:
            return _studio_url(pid, error="Nothing to save")
        try:
            data = json.loads(text)
        except ValueError as exc:
            return _studio_url(
                pid, error=f"Invalid JSON: {quote(str(exc)[:120])}")
        (pdir / "shotlist.json").write_text(text + "\n", encoding="utf-8")
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            db.add_step(conn, pid, "shots", "manual",
                        detail=f"edited shotlist "
                               f"({len(data.get('images', []))} image(s))")
        finally:
            conn.close()
        return _studio_url(pid, msg="Shotlist saved")

    @app.post("/studio/<int:pid>/images/render")
    def studio_images_render(pid):
        if sjob.running:
            return _studio_url(pid, error="A job is already running")
        pdir = studio.prepare_project_folder(cfg, pid)
        if not (pdir / "shotlist.json").exists():
            return _studio_url(pid, error="Generate the shotlist first")

        def worker():
            count = studio.run_imagegen(cfg, pdir)
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            try:
                db.add_step(conn, pid, "images", "auto",
                            detail=f"{count} image(s) via Renderly")
            finally:
                conn.close()

        sjob.start(worker, "image rendering (Renderly)")
        return _studio_url(pid, msg="Image rendering started")

    @app.post("/studio/<int:pid>/stage/done")
    def studio_stage_done(pid):
        """Human override: mark the current stage done even if automation failed."""
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
            if not prod:
                return redirect("/studio?error=Unknown+production")
            stage = prod["stage"]
            if stage in db.latest_steps(conn, pid):
                return _studio_url(pid, error="Stage is already done")
            db.add_step(conn, pid, stage, "manual",
                        detail="marked done by human override")
        finally:
            conn.close()
        return _studio_url(pid, msg=f"{stage} marked done")

    @app.post("/studio/<int:pid>/video/render")
    def studio_video_render(pid):
        if sjob.running:
            return _studio_url(pid, error="A job is already running")
        pdir = studio.prepare_project_folder(cfg, pid)
        if not studio.find_audio(pdir) or not studio.find_srt(pdir) \
                or not studio.find_images(pid_dir=pdir):
            return _studio_url(
                pid, error="Need audio, subtitles and images first")

        def worker():
            final = studio.run_merge_render(cfg, pdir)
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            try:
                db.add_step(conn, pid, "merge", "auto", detail=final.name)
            finally:
                conn.close()

        sjob.start(worker, "final render (ImgToVideo)")
        return _studio_url(pid, msg="Final render started")

    @app.post("/studio/<int:pid>/images/upload")
    def studio_images_upload(pid):
        files = [f for f in request.files.getlist("image_files") if f.filename]
        if not files:
            return _studio_url(pid, error="No images selected")
        img_dir = studio.prod_dir(cfg, pid) / "images"
        img_dir.mkdir(parents=True, exist_ok=True)
        saved = 0
        for f in files:
            ext = Path(f.filename).suffix.lower() or ".jpg"
            if ext not in studio.IMAGE_EXTS:
                continue
            _save_upload(f, img_dir / f"{_slugify(Path(f.filename).stem)}{ext}")
            saved += 1
        if not saved:
            return redirect(f"/studio/{pid}?error=No+supported+image+files")
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            db.add_step(conn, pid, "images", "manual", detail=f"{saved} image(s)")
        finally:
            conn.close()
        return _studio_url(pid, msg=f"{saved} image(s) uploaded")

    @app.post("/studio/<int:pid>/images/generate")
    def studio_images_generate(pid):
        if not cfg.studio_imagegen_command:
            return _studio_url(pid, error="No imagegen_command in config.yaml")
        if sjob.running:
            return _studio_url(pid, error="A job is already running")

        def worker():
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            try:
                pdir = studio.prod_dir(cfg, pid)
                prompts = studio.find_prompts(pdir)
                if not prompts:
                    raise RuntimeError("Generate image prompts first")
                img_dir = pdir / "images"
                img_dir.mkdir(parents=True, exist_ok=True)
                before = {p.name for p in img_dir.iterdir()}
                studio.run_hook(cfg.studio_imagegen_command,
                                {"prompts": prompts, "outdir": img_dir})
                new = [p.name for p in img_dir.iterdir() if p.name not in before]
                if not new:
                    raise RuntimeError("imagegen produced no images")
                db.add_step(conn, pid, "images", "auto",
                            detail=f"{len(new)} image(s) rendered")
            finally:
                conn.close()

        sjob.start(worker, "image generation")
        return _studio_url(pid, msg="Image rendering started")

    @app.post("/studio/<int:pid>/images/delete")
    def studio_images_delete(pid):
        name = request.form.get("name") or ""
        img_dir = studio.prod_dir(cfg, pid) / "images"
        target = (img_dir / name).resolve()
        if target.parent == img_dir.resolve() and target.is_file():
            target.unlink()
        return _studio_url(pid, msg="Image removed")

    @app.post("/studio/<int:pid>/merge/upload")
    def studio_merge_upload(pid):
        f = request.files.get("video_file")
        if not f or not f.filename:
            return _studio_url(pid, error="No video file selected")
        pdir = studio.prod_dir(cfg, pid)
        _save_upload(f, pdir / "final.mp4")
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            db.add_step(conn, pid, "merge", "manual", detail="final.mp4")
        finally:
            conn.close()
        return _studio_url(pid, msg="Final video uploaded")

    @app.post("/studio/<int:pid>/merge/generate")
    def studio_merge_generate(pid):
        if not cfg.studio_merge_command:
            return _studio_url(pid, error="No merge_command in config.yaml")
        if sjob.running:
            return _studio_url(pid, error="A job is already running")

        def worker():
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            try:
                pdir = studio.prod_dir(cfg, pid)
                audio = studio.find_audio(pdir)
                srt = studio.find_srt(pdir)
                images = studio.find_images(pdir)
                if not audio or not srt or not images:
                    raise RuntimeError("Need audio, subtitles and images first")
                out = pdir / "final.mp4"
                studio.run_hook(cfg.studio_merge_command, {
                    "images": pdir / "images", "audio": audio, "srt": srt,
                    "out": out,
                }, timeout=7200)
                if not out.exists():
                    raise RuntimeError("merge produced no video")
                db.add_step(conn, pid, "merge", "auto", detail="final.mp4")
            finally:
                conn.close()

        sjob.start(worker, "merge")
        return _studio_url(pid, msg="Merge started")

    @app.post("/studio/<int:pid>/review/approve")
    def studio_review_approve(pid):
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            db.add_step(conn, pid, "review", "manual", detail="approved")
            db.update_production(conn, pid, status="ready")
        finally:
            conn.close()
        return _studio_url(pid, msg="Approved - ready to publish")

    @app.get("/studio/file/<int:pid>/<path:rel>")
    def studio_file(pid, rel):
        pdir = studio.prod_dir(cfg, pid).resolve()
        target = (pdir / rel).resolve()
        try:
            target.relative_to(pdir)
        except ValueError:
            abort(404)
        if not target.is_file():
            abort(404)
        return send_file(target)

    @app.get("/studio/job")
    def studio_job():
        return {"running": sjob.running, "kind": sjob.kind,
                "error": sjob.error, "log": list(sjob.log)[-40:]}

    # warm the Renderly readiness probe so the first page load is fast too
    threading.Thread(target=lambda: studio.renderly_ready(cfg.renderly_url),
                     daemon=True).start()
    return app
