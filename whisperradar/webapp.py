"""Local web dashboard for WhisperRadar.

Start with:  python wr.py serve          (http://127.0.0.1:8000)
"""

import io
import ipaddress
import json
import logging
import re
import shutil
import socket
import threading
import zipfile
from collections import deque
from pathlib import Path
from urllib.parse import quote, urlparse

from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    request,
    send_file,
)

from . import (ai33, autorun, db, pipeline, producer, scheduler, services,
               settings, studio)
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
        self.seq = 0               # increments per job start (poll reloads)
        self.pid = None            # production the current/last auto-run belongs to
        # auto-run (pipeline mode) state
        self.stage = None          # stage currently being executed
        self.pipeline = False      # True while an auto-run is active
        self.cancel = False        # stop requested - honored between stages
        self.pause_reason = None   # why the last auto-run paused
        self.resume = False        # last auto-run did not finish: offer Resume
        self.summary = None        # completion note from the last auto-run
        self.autorun_plan = None   # the plan the current/last auto-run used

    def start(self, fn, kind: str) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.kind = kind
            self.error = None
            self.seq += 1
            self.pid = None
            self.stage = None
            self.pipeline = False
            self.cancel = False
            self.pause_reason = None
            self.resume = False
            self.summary = None
            self.autorun_plan = None
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


# Generated artifacts per stage, deleted by the start-over reset. Named
# version libraries, refs, the bible and notes are inputs - kept.
RESET_FILES = {
    "style": ["style.md"],
    "script": ["script.md"],
    "audio": ["audio.mp3", "audio.wav", "audio.m4a", "audio.flac",
              "audio.ogg"],
    "srt": ["subtitles.srt"],
    "shots": ["shotlist.json", "shotlist.json.bak", "batch_sheet.txt"],
    "images": ["flow_batch.json"],
    "merge": ["final.mp4"],
}


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
    ai33.warm_cache(cfg)  # background prefetch so the voice picker is instant

    @app.before_request
    def _block_cross_origin_posts():
        """CSRF guard for a token-free local app: browsers always attach an
        Origin header to cross-site form POSTs, so a drive-by webpage cannot
        trigger jobs, deletes or channel changes against 127.0.0.1. Requests
        without Origin/Referer (local scripts, curl) still work. The Host
        header must also name the address the browser actually connected to,
        which defeats DNS rebinding (attacker hostname resolving to the
        local machine)."""
        if request.method != "POST":
            return None

        def same_site(url: str) -> bool:
            try:
                p = urlparse(url)
            except ValueError:
                return False
            return p.scheme in ("http", "https") and p.netloc == request.host

        def host_matches_connection() -> bool:
            """DNS-rebinding defense: the Host header must be a local
            address, an IP literal (LAN access), or this machine's own
            hostname - never an attacker-chosen domain resolving to the
            local machine."""
            raw = request.host or ""
            host = (raw[1:raw.index("]")] if raw.startswith("[")
                    else raw.split(":")[0]).lower()
            if host in ("127.0.0.1", "localhost", "::1"):
                return True
            try:
                ipaddress.ip_address(host)
                return True
            except ValueError:
                return host == socket.gethostname().lower()

        if not host_matches_connection():
            return "Blocked: host header mismatch", 403
        origin = request.headers.get("Origin") or ""
        referer = request.headers.get("Referer") or ""
        if origin and not same_site(origin):
            return "Blocked: cross-origin request", 403
        if not origin and referer and not same_site(referer):
            return "Blocked: cross-origin request", 403
        return None

    job = _Job()
    sjob = _Job()  # studio jobs (LLM generation, SRT alignment)

    class _DequeHandler(logging.Handler):
        def emit(self, record):
            job.log.append(record.getMessage())

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, _DequeHandler) for h in root.handlers):
        root.addHandler(_DequeHandler(level=logging.INFO))

    def _start_producer(channels: int, log_fn) -> None:
        """Start an Auto Run in the studio job slot (used by the scheduler)."""
        if sjob.running:
            log_fn("producer: a job is already running - skipping")
            return

        def worker():
            result = producer.run(cfg, log=sjob.log.append, job=sjob)
            sjob.log.append(
                f"=== scheduled produce: {len(result['created'])} created, "
                f"{len(result['skipped'])} skipped, result={result['result']} ===")

        sjob.start(worker, f"scheduled auto-run ({channels} channel(s))")

    sched = scheduler.Scheduler(cfg, sjob, _start_producer)
    sched.start()

    def _autostart_services() -> None:
        """Optionally bring the engine's services up with the dashboard, so
        there is no .bat to remember."""
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            if not settings.load(conn).get("services_autostart"):
                return
        finally:
            conn.close()
        try:
            services.MANAGER.ensure(cfg, ["renderly", "flow-driver"],
                                    log_fn=lambda m: logging.getLogger(
                                        "whisperradar").info("autostart: %s", m))
        except Exception as exc:  # noqa: BLE001 - never block the dashboard
            logging.getLogger("whisperradar").warning(
                "service autostart failed: %s", exc)

    threading.Thread(target=_autostart_services, name="wr-autostart",
                     daemon=True).start()

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

    # ---------------------------------------------------------- settings ---
    # Global Auto Run criteria. Own channels live on /my-channels; monitored
    # source channels stay on / (Dashboard).

    @app.get("/settings")
    def settings_page():
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            values = settings.load(conn)
        finally:
            conn.close()
        return render_template(
            "settings.html", values=values, spec=settings.SPEC,
            groups=settings.grouped_spec(),
            seed_dirs_text=settings.format_seed_dirs(values.get("seed_dirs")),
            providers=[p["name"] for p in cfg.studio_llm_providers],
            scheduler=sched.status(),
            services=services.MANAGER.status_cached(cfg),
            flowimagesgen_ready=studio.flowimagesgen_ready(cfg),
            msg=request.args.get("msg"), error=request.args.get("error"))

    @app.post("/services/<name>/<action>")
    def services_control(name, action):
        """Start/stop one external tool from the dashboard, so no .bat file is
        needed. Stop only ever touches a service WhisperRadar started."""
        if name not in ("renderly", "flow-driver"):
            return redirect("/settings?error=Unknown+service")
        log = logging.getLogger("whisperradar")
        notes: list[str] = []
        if action == "start":
            services.MANAGER.start(cfg, name, log_fn=notes.append)
            msg = f"{name}: " + (notes[-1] if notes else "started")
        elif action == "stop":
            ok = services.MANAGER.stop(cfg, name, log_fn=notes.append,
                                       force=bool(request.form.get("force")))
            msg = f"{name}: " + (notes[-1] if notes
                                 else ("stopped" if ok else "not stopped"))
        else:
            return redirect("/settings?error=Unknown+action")
        log.info("services: %s", msg)
        return redirect("/settings?msg=" + quote(msg))

    @app.post("/settings/save")
    def settings_save():
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            form = dict(request.form)
            form["autorun_enabled"] = ("1" if request.form.get("autorun_enabled")
                                       else "0")
            _, warnings = settings.save(conn, form)
        finally:
            conn.close()
        msg = "Settings saved"
        if warnings:
            msg += " - " + "; ".join(warnings)
        return redirect("/settings?msg=" + quote(msg))

    # ------------------------------------------------------- my channels ---
    # The channels the user publishes on - mirrored into Renderly lazily.

    def _own_channel_id():
        try:
            return int(request.form.get("id") or 0)
        except ValueError:
            return 0

    @app.get("/my-channels")
    def my_channels_page():
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            own_channels = db.list_own_channels(conn)
            # one shared lookup for every row: never one Renderly request per
            # channel, and never a blocking one
            channels, chan_error, known = studio.renderly_channel_list(cfg)
            links = {ch["id"]: studio.renderly_channel_status(
                cfg, ch, channels, chan_error, known)
                for ch in own_channels}
            # genres that actually exist on monitored channels, so a channel
            # can be pointed at real sources instead of guessing the spelling
            genres = [r["genre"] for r in conn.execute(
                "SELECT DISTINCT genre FROM channels"
                " WHERE genre IS NOT NULL AND genre <> ''"
                " ORDER BY genre COLLATE NOCASE")]
            source_genres = {g.lower() for g in genres}
        finally:
            conn.close()
        return render_template(
            "channels.html", own_channels=own_channels, links=links,
            renderly_url=cfg.renderly_url, genres=genres,
            source_genres=source_genres,
            providers=[p["name"] for p in cfg.studio_llm_providers],
            msg=request.args.get("msg"), error=request.args.get("error"))

    @app.post("/my-channels/add")
    def my_channels_add():
        name = (request.form.get("name") or "").strip()
        if not name:
            return redirect("/my-channels?error="
                            + quote("Channel name is required"))
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            if db.get_own_channel(conn, name):
                return redirect("/my-channels?error=" + quote(
                    f"A channel named '{name}' already exists"))
            oc_id = db.create_own_channel(
                conn, name,
                description=(request.form.get("description") or "").strip(),
                genre=(request.form.get("genre") or "").strip() or "general",
                youtube_handle=(request.form.get("youtube_handle") or "").strip(),
            )
            created = db.get_own_channel(conn, oc_id)
            result = studio.sync_renderly_channel(cfg, conn, created)
        finally:
            conn.close()
        if result.get("ok"):
            where = ("created in Renderly" if result.get("created")
                     else "linked to the existing Renderly channel")
            msg = f"Channel '{name}' saved - {where} (id {result['id']})"
        else:
            msg = (f"Channel '{name}' saved - Renderly link pending: "
                   f"{result.get('error')}")
        return redirect("/my-channels?msg=" + quote(msg))

    @app.post("/my-channels/edit")
    def my_channels_edit():
        oc_id = _own_channel_id()
        if not oc_id:
            return redirect("/my-channels?error=Unknown+channel")
        fields = {}
        for key in ("name", "description", "genre", "youtube_handle"):
            if key in request.form:
                fields[key] = (request.form.get(key) or "").strip()
        if "genre" in fields and not fields["genre"]:
            fields["genre"] = "general"
        if "active" in request.form:
            fields["active"] = 1 if request.form.get("active") in ("1", "on",
                                                                  "true") else 0
        # per-channel production defaults; empty means "inherit the global"
        for key in ("default_voice", "bible_dir", "refs_dir",
                    "flow_project_url"):
            if key in request.form:
                fields[key] = (request.form.get(key) or "").strip() or None
        for key in ("style", "bible"):   # multi-line text: keep newlines
            if key in request.form:
                fields[key] = (request.form.get(key) or "").strip() or None
        for key in ("default_engine", "default_render_mode", "topic_pick"):
            if key in request.form:
                fields[key] = (request.form.get(key) or "").strip() or None
        if "producer_llm_provider" in request.form:
            # only a configured provider is meaningful; anything else inherits
            raw = (request.form.get("producer_llm_provider") or "").strip()
            known = {p["name"] for p in cfg.studio_llm_providers}
            fields["producer_llm_provider"] = raw if raw in known else None
        for key in ("run_window_start", "run_window_end"):
            if key in request.form:
                raw = (request.form.get(key) or "").strip()
                fields[key] = raw if re.match(r"^([01]?\d|2[0-3]):[0-5]\d$",
                                              raw) else None
        if "candidate_window_days" in request.form:
            raw = (request.form.get("candidate_window_days") or "").strip()
            try:
                fields["candidate_window_days"] = (max(0, min(3650, int(raw)))
                                                   if raw else None)
            except ValueError:
                fields["candidate_window_days"] = None
        # script quality gate overrides
        for key, lo, hi in (("script_min_rating", 1.0, 10.0),
                            ("script_max_overlap", 0.0, 1.0)):
            if key in request.form:
                raw = (request.form.get(key) or "").strip()
                try:
                    fields[key] = max(lo, min(hi, float(raw))) if raw else None
                except ValueError:
                    fields[key] = None
        if "script_max_attempts" in request.form:
            raw = (request.form.get("script_max_attempts") or "").strip()
            try:
                fields["script_max_attempts"] = (max(1, min(10, int(raw)))
                                                 if raw else None)
            except ValueError:
                fields["script_max_attempts"] = None
        if "script_judge_provider" in request.form:
            raw = (request.form.get("script_judge_provider") or "").strip()
            known = {p["name"] for p in cfg.studio_llm_providers}
            fields["script_judge_provider"] = raw if raw in known else None
        # shotlist gate overrides
        if "shotlist_min_alignment" in request.form:
            raw = (request.form.get("shotlist_min_alignment") or "").strip()
            try:
                fields["shotlist_min_alignment"] = (max(0.0, min(1.0, float(raw)))
                                                    if raw else None)
            except ValueError:
                fields["shotlist_min_alignment"] = None
        if "shotlist_max_attempts" in request.form:
            raw = (request.form.get("shotlist_max_attempts") or "").strip()
            try:
                fields["shotlist_max_attempts"] = (max(1, min(5, int(raw)))
                                                   if raw else None)
            except ValueError:
                fields["shotlist_max_attempts"] = None
        if "shotlist_judge_provider" in request.form:
            raw = (request.form.get("shotlist_judge_provider") or "").strip()
            known = {p["name"] for p in cfg.studio_llm_providers}
            fields["shotlist_judge_provider"] = raw if raw in known else None
        if "default_upscale" in request.form:
            raw = (request.form.get("default_upscale") or "").strip()
            try:
                fields["default_upscale"] = (max(0, min(4, int(raw)))
                                             if raw else None)
            except ValueError:
                fields["default_upscale"] = None
        if "per_day" in request.form:
            raw = (request.form.get("per_day") or "").strip()
            try:
                fields["per_day"] = max(0, min(50, int(raw))) if raw else None
            except ValueError:
                fields["per_day"] = None
        if "autorun_enabled" in request.form:
            raw = (request.form.get("autorun_enabled") or "").strip()
            fields["autorun_enabled"] = (None if raw == ""
                                         else 1 if raw in ("1", "on", "true")
                                         else 0)
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            if not db.get_own_channel(conn, oc_id):
                return redirect("/my-channels?error=Unknown+channel")
            db.update_own_channel(conn, oc_id, **fields)
        finally:
            conn.close()
        return redirect("/my-channels?msg=" + quote("Channel updated"))

    @app.post("/my-channels/sync")
    def my_channels_sync():
        oc_id = _own_channel_id()
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            ch = db.get_own_channel(conn, oc_id)
            if not ch:
                return redirect("/my-channels?error=Unknown+channel")
            result = studio.sync_renderly_channel(cfg, conn, ch)
        finally:
            conn.close()
        if result.get("ok"):
            action = "created" if result.get("created") else "linked"
            msg = f"Renderly channel {action} (id {result['id']})"
        else:
            msg = f"Renderly sync failed: {result.get('error')}"
        return redirect("/my-channels?msg=" + quote(msg))

    @app.post("/my-channels/remove")
    def my_channels_remove():
        oc_id = _own_channel_id()
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            db.remove_own_channel(conn, oc_id)
        finally:
            conn.close()
        return redirect("/my-channels?msg=" + quote(
            "Channel removed from WhisperRadar - the Renderly channel was "
            "left untouched"))

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
        own_channels = db.list_own_channels(conn)
        own_by_id = {c["id"]: c["name"] for c in own_channels}
        prods = []
        for p in db.list_productions(conn):
            steps = db.latest_steps(conn, p["id"])
            done = sum(1 for s in db.STAGES if s in steps)
            prods.append({"row": p, "done": done, "total": len(db.STAGES),
                          "own_channel": own_by_id.get(p["own_channel_id"])})
        sources = db.get_videos(conn, status="transcribed", limit=500)
        conn.close()
        return render_template("studio.html", prods=prods, sources=sources,
                               own_channels=[c for c in own_channels
                                             if c["active"]],
                               job=sjob, msg=request.args.get("msg"),
                               error=request.args.get("error"))

    @app.post("/studio/new")
    def studio_new():
        title = (request.form.get("title") or "").strip()
        genre = (request.form.get("genre") or "").strip() or "general"
        source = (request.form.get("source_video_id") or "").strip() or None
        work_dir = (request.form.get("work_dir") or "").strip() or None
        own_channel = (request.form.get("own_channel_id") or "").strip() or None
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
            seeded = {"source": "", "bible": False, "refs": 0}
            if own_channel:
                oc = db.get_own_channel(conn, own_channel)
                if oc:
                    # the channel's genre is the production's genre unless the
                    # form said otherwise
                    if not (request.form.get("genre") or "").strip():
                        db.update_production(conn, pid, genre=oc["genre"])
                    db.update_production(conn, pid, own_channel_id=oc["id"])
                    prod = db.get_production(conn, pid)
                    seeded = studio.seed_production(cfg, conn, prod)
        finally:
            conn.close()
        if row and row["transcript_path"] and Path(row["transcript_path"]).exists():
            pdir = studio.prod_dir(cfg, pid)
            shutil.copy(row["transcript_path"], pdir / "source_transcript.txt")
        msg = ""
        if seeded["bible"] or seeded["style"] or seeded["refs"]:
            bits = []
            if seeded["bible"]:
                bits.append("bible.md")
            if seeded["style"]:
                bits.append("style.md")
            if seeded["refs"]:
                bits.append(f"{seeded['refs']} ref image(s)")
            msg = (f"?msg={quote('Seeded ' + ' + '.join(bits) + ' from ' + seeded['source'])}")
        return redirect(f"/studio/{pid}{msg}")

    @app.get("/studio/<int:pid>")
    def studio_detail(pid):
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
            if not prod:
                abort(404)
            eff = settings.for_production(conn, prod)
            own_channels = db.list_own_channels(conn)
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
        bible = studio.find_bible(pdir)
        bible_text = _read_text(bible)
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
            prod_voice=prod["voice"] or eff["voice"],
            voice_from=("this production" if prod["voice"]
                        else f"channel: {eff['own_channel_name']}"
                        if eff["own_channel_name"] and eff["voice"]
                        else "Settings" if eff["voice"] else ""), ai33_ready=bool(ai33.api_key(cfg)),
            flow_ready=studio.flow_driver_ready(cfg),
            flow_refs=[p.name for p in sorted((pdir / "refs").glob("*"))
                       if p.is_file()] if (pdir / "refs").exists() else [],
            flow_upscale_default=eff["upscale"],
            flow_channel_default=eff["renderly_channel_name"],
            default_render_mode=eff["render_mode"],
            default_engine=eff["engine"],
            own_channel_name=eff["own_channel_name"],
            flow_project_url_default=(eff["flow_project_url"]
                                      or cfg.flowimagesgen_project_url or ""),
            flowimagesgen_ready=studio.flowimagesgen_ready(cfg),
            own_channels=own_channels,
            script_versions=_version_names(pdir, "script"),
            stage_direction=db.stage_extra(prod, stage),
            bible_text=bible_text,
            batch_sheet=(pdir / "batch_sheet.txt").exists(),
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

    @app.post("/studio/<int:pid>/start-over")
    def studio_start_over(pid):
        """Start over: reset progress from a chosen stage onward - deletes
        that stage's generated artifacts and step history so auto-run (or
        the manual buttons) re-executes it. Inputs like the bible, refs,
        named versions and notes are kept."""
        if sjob.running:
            return _studio_url(pid, error="A job is already running")
        from_stage = request.form.get("from") or ""
        if from_stage not in ("style", "script", "images"):
            return _studio_url(pid, error="Unknown start-over scope")
        with_audio = request.form.get("with_audio") == "1"
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            if not db.get_production(conn, pid):
                return redirect("/studio?error=Unknown+production")
            reset = [s for s in db.STAGES[db.STAGES.index(from_stage):]
                     if s != "audio"]
            if with_audio:
                reset.append("audio")
            # a live Flow batch would repopulate images\ while we delete it
            studio.flow_stop(cfg)
            with sjob._lock:
                if sjob.running:  # re-check under the lock (check-then-act)
                    return _studio_url(pid, error="A job is already running")
                pdir = studio.prod_dir(cfg, pid)
                removed = []
                for stage in reset:
                    for name in RESET_FILES.get(stage, []):
                        f = pdir / name
                        if f.exists():
                            f.unlink()
                            removed.append(name)
                    if stage == "images":
                        img_dir = pdir / "images"
                        if img_dir.exists():
                            for f in img_dir.iterdir():
                                if f.is_file():
                                    f.unlink(missing_ok=True)
                            removed.append("images/*")
                    if stage == "merge":
                        out_dir = pdir / "out"
                        if out_dir.exists():
                            shutil.rmtree(out_dir, ignore_errors=True)
                            removed.append("out/")
                db.delete_steps(conn, pid, reset)
                db.update_production(conn, pid, stage=from_stage,
                                     status="active")
        finally:
            conn.close()
        detail = ", ".join(removed) if removed else "nothing on disk"
        return _studio_url(
            pid, msg=f"Start over from '{from_stage}' - cleared: {detail}")

    @app.post("/studio/<int:pid>/own-channel")
    def studio_own_channel(pid):
        """Assign (or clear) the production's own channel. Its per-channel
        defaults then drive the images/audio stages."""
        raw = (request.form.get("own_channel_id") or "").strip()
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            if not db.get_production(conn, pid):
                return _studio_url(pid, error="Unknown production")
            if not raw:
                db.update_production(conn, pid, own_channel_id=None)
                return _studio_url(
                    pid, msg="Channel cleared - using the legacy whisperradar "
                             "Renderly channel")
            ch = db.get_own_channel(conn, raw)
            if not ch:
                return _studio_url(pid, error="Unknown channel")
            db.update_production(conn, pid, own_channel_id=ch["id"])
        finally:
            conn.close()
        return _studio_url(pid, msg=f"Production assigned to '{ch['name']}'")

    @app.post("/studio/<int:pid>/seed")
    def studio_seed(pid):
        """Copy the channel's bible.md + refs into the production (idempotent)."""
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
            if not prod:
                return _studio_url(pid, error="Unknown production")
            result = studio.seed_production(cfg, conn, prod)
        finally:
            conn.close()
        if not result["source"]:
            return _studio_url(
                pid, error="No seed source - set a bible/refs folder on My "
                           "Channels, or a per-genre seed folder in Settings")
        bits = []
        if result["bible"]:
            bits.append("bible.md")
        if result.get("style"):
            bits.append("style.md")
        if result["refs"]:
            bits.append(f"{result['refs']} ref image(s)")
        if not bits:
            return _studio_url(pid, msg="Nothing to seed - bible.md, style.md "
                                        "and refs already exist")
        return _studio_url(
            pid, msg=f"Seeded {' + '.join(bits)} from {result['source']}")

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
            autorun.raise_result(autorun.run_stage(
                cfg, pid, "style", {"provider": provider}))

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
        finally:
            conn.close()

        def worker():
            autorun.raise_result(autorun.run_stage(
                cfg, pid, "script", {"provider": provider}))

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

    @app.get("/studio/voices.json")
    def studio_voices():
        """Voice catalog for the Studio audio stage - fetched server-side
        with the OpenSpeaker API key, cached ~10 min in the ai33 module.
        ?source=favorites lists the voices starred in the OpenSpeaker app."""
        provider = (request.args.get("provider") or "").strip() or None
        source = (request.args.get("source") or "").strip() or None
        try:
            items = ai33.voices(cfg, provider=provider, source=source)
            return {"ready": True, "voices": items, "source": source}
        except RuntimeError as exc:
            return {"ready": bool(ai33.api_key(cfg)), "voices": [],
                    "source": source, "error": str(exc)}

    @app.post("/studio/<int:pid>/voice")
    def studio_voice_save(pid):
        voice = (request.form.get("voice") or "").strip()[:300] or None
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            if not db.get_production(conn, pid):
                return _studio_url(pid, error="Unknown production")
            db.update_production(conn, pid, voice=voice)
        finally:
            conn.close()
        return _studio_url(pid, msg=f"Narration voice: {voice or 'default'}")

    @app.post("/studio/<int:pid>/audio/generate")
    def studio_audio_generate(pid):
        if not cfg.studio_tts_command:
            return _studio_url(pid, error="No tts_command in config.yaml")
        if sjob.running:
            return _studio_url(pid, error="A job is already running")

        voice = (request.form.get("voice") or "").strip()[:300] or None
        if voice:
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            try:
                db.update_production(conn, pid, voice=voice)
            finally:
                conn.close()

        def worker():
            autorun.raise_result(autorun.run_stage(cfg, pid, "audio"))

        sjob.start(worker, "tts")
        return _studio_url(pid, msg="TTS started")

    @app.post("/studio/<int:pid>/srt/generate")
    def studio_srt_generate(pid):
        if sjob.running:
            return _studio_url(pid, error="A job is already running")

        def worker():
            autorun.raise_result(autorun.run_stage(cfg, pid, "srt"))

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

    @app.post("/studio/<int:pid>/refs/upload")
    def studio_refs_upload(pid):
        """Upload character/reference images used by the Flow Driver."""
        pdir = studio.prod_dir(cfg, pid)
        rdir = pdir / "refs"
        rdir.mkdir(exist_ok=True)
        files = [f for f in request.files.getlist("ref_files") if f.filename]
        if not files:
            return _studio_url(pid, error="No reference images selected")
        saved = 0
        for f in files:
            ext = Path(f.filename).suffix.lower()
            if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                continue
            name = _slugify(Path(f.filename).stem, 60) + ext
            f.save(rdir / name)
            saved += 1
        if not saved:
            return _studio_url(pid, error="Only .png/.jpg/.jpeg/.webp refs")
        return _studio_url(pid, msg=f"{saved} reference image(s) added")

    @app.post("/studio/<int:pid>/refs/delete")
    def studio_refs_delete(pid):
        name = _slugify(request.form.get("name") or "", 60)
        rdir = studio.prod_dir(cfg, pid) / "refs"
        if name:
            for f in sorted(rdir.glob(f"{name}.*")) if rdir.exists() else []:
                if f.is_file() and f.suffix.lower() in (
                        ".png", ".jpg", ".jpeg", ".webp"):
                    f.unlink()
                    return _studio_url(pid, msg="Reference image removed")
        return _studio_url(pid, error="Reference image not found")

    @app.post("/studio/<int:pid>/flow/stop")
    def studio_flow_stop(pid):
        stopped = studio.flow_stop(cfg)
        if stopped:
            return _studio_url(pid, msg="Flow Driver batch stopped")
        return _studio_url(
            pid, error="Nothing to stop (service down or batch not running)")

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

    @app.post("/studio/<int:pid>/bible/save")
    def studio_bible_save(pid):
        """Optional character / reference bible fed to the shotlist planner."""
        pdir = studio.prod_dir(cfg, pid)
        text = (request.form.get("bible") or "").strip()
        f = request.files.get("bible_file")
        if f and f.filename:
            text = f.read().decode("utf-8", "ignore").strip()
        if not text:
            return _studio_url(pid, error="Nothing to save")
        (pdir / "bible.md").write_text(text + "\n", encoding="utf-8")
        return _studio_url(pid, msg="Character / reference bible saved")

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
        # the manifest-authoring brief's bible gate: the LLM refuses to plan
        # without a reference bible, so require it up front
        if not studio.find_bible(studio.prod_dir(cfg, pid)):
            return _studio_url(
                pid,
                error="The planning brief requires a character/reference "
                      "bible - write or upload one below first")
        provider = request.form.get("provider") or cfg.studio_llm_default

        def worker():
            autorun.raise_result(autorun.run_stage(
                cfg, pid, "shots", {"provider": provider}))

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
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            eff = settings.for_production(conn, db.get_production(conn, pid))
        finally:
            conn.close()
        mode = request.form.get("render_mode") or eff["render_mode"]
        if mode not in ("api", "flow"):
            mode = "flow" if studio.flow_driver_ready(cfg) else "api"
        engine = (request.form.get("engine") or eff["engine"] or "renderly")
        if engine not in ("renderly", "flowimagesgen"):
            engine = "renderly"
        flow_channel = (request.form.get("flow_channel")
                        or eff["renderly_channel_name"]).strip()
        flow_project = (request.form.get("flow_project") or "").strip()
        try:
            flow_upscale = max(0, min(4, int(request.form.get("flow_upscale")
                                             or eff["upscale"])))
        except ValueError:
            flow_upscale = eff["upscale"]
        flow_master = (request.form.get("flow_master") or "").strip()
        flow_project_url = (request.form.get("flow_project_url") or "").strip()
        renderly_channel = None
        if engine == "renderly" and mode == "api":
            renderly_channel = studio.resolve_renderly_channel(
                cfg, eff["own_channel"], create=True)

        def worker():
            autorun.raise_result(autorun.run_stage(cfg, pid, "images", {
                "mode": mode, "engine": engine, "flow_channel": flow_channel,
                "flow_project": flow_project,
                "flow_upscale": flow_upscale, "flow_master": flow_master,
                "flow_project_url": flow_project_url,
                "renderly_channel": renderly_channel,
                "log": sjob.log.append,
            }))

        label = ("FlowImagesGen" if engine == "flowimagesgen"
                 else "Flow Driver" if mode == "flow" else "Renderly")
        sjob.start(worker, f"image rendering ({label})")
        return _studio_url(pid, msg=f"Image rendering started ({label})")

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
            autorun.raise_result(autorun.run_stage(
                cfg, pid, "merge", {"mode": "cli"}))

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
            autorun.raise_result(autorun.run_stage(
                cfg, pid, "merge", {"mode": "hook"}))

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

    @app.get("/studio/produce/plan")
    def studio_produce_plan():
        """Dry-run: what the Auto Run producer would create right now. Makes
        no LLM calls and spends nothing."""
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            plan = producer.build_plan(cfg, conn)
        finally:
            conn.close()
        return {"plan": plan, "running": sjob.running}

    @app.post("/studio/produce")
    def studio_produce():
        """Create + run a production per runnable own channel (Auto Run)."""
        if sjob.running:
            return redirect("/studio?error=A+job+is+already+running")

        def worker():
            result = producer.run(cfg, log=sjob.log.append, job=sjob)
            sjob.log.append(
                f"=== produce: {len(result['created'])} created, "
                f"{len(result['skipped'])} skipped, result={result['result']} ===")
            if result["result"].startswith(("paused:", "failed:")):
                raise RuntimeError(result["result"].split(":", 1)[1])

        sjob.start(worker, "auto-run producer")
        return redirect("/studio?msg=Producer+started")

    @app.get("/studio/<int:pid>/auto-run/plan")
    def studio_autorun_plan(pid):
        """Dry-run: what auto-run would do from the current state."""
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            if not db.get_production(conn, pid):
                abort(404)
        finally:
            conn.close()
        return {"plan": autorun.build_plan(cfg, pid),
                "running": sjob.running,
                "paused": sjob.pause_reason if sjob.pid == pid else None}

    @app.post("/studio/<int:pid>/auto-run")
    def studio_autorun(pid):
        """Run every remaining pipeline stage in order, skipping the done
        ones, pausing on missing manual input, and always stopping before
        review."""
        if sjob.running:
            return _studio_url(pid, error="A job is already running")
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            if not db.get_production(conn, pid):
                return redirect("/studio?error=Unknown+production")
        finally:
            conn.close()
        plan = autorun.build_plan(cfg, pid)
        if not any(e["action"] == "run" for e in plan):
            pause = next((e for e in plan if e["action"] == "pause"), None)
            if pause:
                return _studio_url(pid,
                                   error=f"Nothing to run - {pause['detail']}")
            return _studio_url(
                pid, error="Every stage up to review is already done")

        def worker():
            result = autorun.run_pipeline(cfg, pid, job=sjob,
                                          log=sjob.log.append)
            if result.startswith("failed:"):
                raise RuntimeError(result.split(":", 1)[1])

        if not sjob.start(worker, "auto-run"):
            return _studio_url(pid, error="A job is already running")
        sjob.pid = pid  # pause/summary state belongs to this production
        return _studio_url(pid, msg="Auto-run started")

    @app.post("/studio/<int:pid>/auto-run/stop")
    def studio_autorun_stop(pid):
        if not sjob.running or sjob.kind != "auto-run":
            return _studio_url(pid, error="No auto-run is running")
        sjob.cancel = True
        return _studio_url(pid,
                           msg="Stopping after the current stage finishes")

    @app.get("/studio/job")
    def studio_job():
        return {"running": sjob.running, "kind": sjob.kind,
                "error": sjob.error, "log": list(sjob.log)[-40:],
                "stage": sjob.stage, "pipeline": sjob.pipeline,
                "paused": sjob.pause_reason, "pid": sjob.pid,
                "seq": sjob.seq}

    # warm the Renderly readiness probe so the first page load is fast too
    threading.Thread(target=lambda: studio.renderly_ready(cfg.renderly_url),
                     daemon=True).start()
    return app
