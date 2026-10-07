"""Local web dashboard for WhisperRadar.

Start with:  python wr.py serve          (http://127.0.0.1:8000)
"""

import io
import ipaddress
import glob
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import zipfile
from collections import deque
from pathlib import Path
from urllib.parse import quote, urlparse

from flask import (
    Flask,
    abort,
    g,
    has_request_context,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
)

from . import external_prompts
from . import packaging
from . import thumbnails
from . import (ai33, autorun, briefs, channel_io, db, pipeline, producer, remote,
               scheduler, services, settings, studio)
from .cli import _slugify, format_duration
from .watch import CHANNEL_ID_RE, resolve_channel


def _back(request, msg: str | None = None, error: str | None = None,
          base: str = "/"):
    """Redirect back to the dashboard (or `base`) preserving the current view.

    Filter and paging values come from hidden fields the POST forms carry, so
    acting on a row (queue, retry, delete, ...) returns to the same page and
    sort instead of jumping to page 1. `base` lets a form living on another
    page (e.g. Watched Channels) return there instead of the dashboard.
    """
    parts = []
    for key in ("status", "genre", "channel", "sort", "q", "per_page", "page"):
        val = request.form.get(key)
        if val:
            parts.append(f"{key}={quote(val)}")
    if msg:
        parts.append(f"msg={quote(msg)}")
    if error:
        parts.append(f"error={quote(error)}")
    return redirect(base + ("?" + "&".join(parts) if parts else ""))


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


# The job whose worker thread is running right now: code inside a worker reads
# the studio job (sjob) without a request, so the thread remembers which one.
_job_tls = threading.local()


class _TeeLog(deque):
    """Job log that also lands on disk: the in-memory deque dies with the
    server process - which is exactly when a stopped batch's real reason
    used to vanish."""

    def __init__(self, path: Path | None = None, maxlen: int | None = 400):
        super().__init__(maxlen=maxlen)
        self._path = path

    def append(self, item):
        super().append(item)
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(item) + "\n")
        except OSError:
            pass


def _studio_log_path(cfg) -> Path:
    return Path(cfg.db_path).parent / "logs" / "studio.log"


class _Job:
    """Tracks a pipeline job running in a background thread."""

    def __init__(self, channel_id: int = 0, label: str = "",
                 log_path: Path | None = None):
        self.channel_id = channel_id   # own channel this slot belongs to (0 = none)
        self.label = label             # shown in "also running" notes
        self.running = False
        self.kind = ""
        self.error = None
        self.log: deque = _TeeLog(log_path)
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
            _job_tls.job = self
            try:
                fn()
                self.log.append(f"=== {kind}: finished ===")
            except Exception as exc:
                self.error = str(exc)
                self.log.append(f"=== {kind}: FAILED: {exc} ===")
            finally:
                self.running = False
                _job_tls.job = None

        threading.Thread(target=worker, daemon=True).start()
        return True


def _stage(func, cfg):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        func(cfg, conn)
    finally:
        conn.close()


def _remember_stage_provider(cfg, pid: int, stage: str, chosen: str | None) -> None:
    """Persist the LLM picked in a stage's dropdown so later runs (and the page
    after a reload) keep it. Choosing the Default LLM CLEARS the override, so
    the stage resumes following the global default. An unknown/unready name is
    never stored - that is what used to resurrect deleted providers."""
    if not chosen:
        return
    try:
        if not studio.provider_ready(cfg, chosen):
            return
    except Exception:  # noqa: BLE001 - never block the run on validation
        return
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        default = autorun._default_provider(cfg, pid)
        db.set_stage_provider(conn, pid, stage,
                              None if chosen == default else chosen)
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


def _download_many(cfg, video_ids: list[str]):
    """Bulk counterpart of `_download_one`, for the dashboard's multi-select
    actions - one job, one `process_downloads` call for the whole batch."""
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        for video_id in video_ids:
            db.set_video(conn, video_id, status="new", auto=1, error=None)
        pipeline.process_downloads(cfg, conn, video_ids=video_ids)
    finally:
        conn.close()


def _transcribe_many(cfg, video_ids: list[str]):
    """Bulk counterpart of `_transcribe_one` - only videos with audio already
    downloaded are included; the rest are silently skipped (same as the
    single-video action, which requires `audio_path` too)."""
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        ready = []
        for video_id in video_ids:
            row = db.get_video(conn, video_id)
            if row and row["audio_path"]:
                db.set_video(conn, video_id, status="downloaded", error=None)
                ready.append(video_id)
        if ready:
            pipeline.process_transcripts(cfg, conn, video_ids=ready)
    finally:
        conn.close()


PAGE_SIZE = 50
PAGE_SIZES = (25, 50, 100, 200)
PER_PAGE_COOKIE = "wr_per_page"


def _safe_int(value, default: int, lo: int | None = None,
              hi: int | None = None) -> int:
    """int() that never raises; used for query/cookie params so a malformed
    ?page=abc (or a hand-edited URL) cannot 500 the dashboard."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        number = max(lo, number)
    if hi is not None:
        number = min(hi, number)
    return number


def _page_size(value, default: int) -> int:
    """A page-size value clamped to the offered sizes."""
    return value if value in PAGE_SIZES else default


# Generated artifacts per stage, deleted by the start-over reset. Named
# version libraries, refs, the bible and notes are inputs - kept.
RESET_FILES = {
    "style": ["writing_style.md"],
    "script": ["script.md"],
    "audio": ["audio.mp3", "audio.wav", "audio.m4a", "audio.flac",
              "audio.ogg"],
    "srt": ["subtitles.srt"],
    "shots": ["shotlist.json", "shotlist.json.bak", "batch_sheet.txt",
              "prompts.txt"],
    "images": [],
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


def format_views(value) -> str:
    """Compact view count for the dashboard: 1234567 -> '1.2M'."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return ""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M".replace(".0M", "M")
    if count >= 1_000:
        return f"{count / 1_000:.1f}K".replace(".0K", "K")
    return str(count)


def create_app(cfg) -> Flask:
    app = Flask(__name__)
    remote.install(app, cfg)    # password gate for anything not local (phone access)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1 GB uploads
    app.config["TEMPLATES_AUTO_RELOAD"] = True  # local app: pick up edits live
    app.jinja_env.filters["dur"] = format_duration
    app.jinja_env.globals["flow_prompt_limit"] = studio.FLOWBATCH_MAX_PROMPT_CHARS
    app.jinja_env.filters["views"] = format_views
    app.jinja_env.filters["fromjson"] = (
        lambda v: briefs.normalize_custom(v) or {})
    app.jinja_env.filters["typesjson"] = (
        lambda v: briefs.normalize_types(v) or {})
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
                # this PC's own name, or its Tailscale MagicDNS name
                # (<pc>.<tailnet>.ts.net) - still behind the password gate
                mine = socket.gethostname().lower()
                return (host == mine
                        or (host.endswith(".ts.net")
                            and host.split(".")[0] == mine))

        if not host_matches_connection():
            return "Blocked: host header mismatch", 403
        origin = request.headers.get("Origin") or ""
        referer = request.headers.get("Referer") or ""
        if origin and not same_site(origin):
            return "Blocked: cross-origin request", 403
        if not origin and referer and not same_site(referer):
            return "Blocked: cross-origin request", 403
        return None

    job = _Job(log_path=_studio_log_path(cfg))

    # Studio jobs (LLM generation, SRT alignment, auto-run, rendering...) run
    # in ONE slot PER OWN CHANNEL: a channel does one thing at a time, but a
    # run in channel A never blocks work in channel B. `sjob` is a handle that
    # resolves to the right slot: the worker's own slot inside a job thread,
    # else the slot of the request's production's channel, else of the channel
    # selected in the studio (cookie), else the "no channel" slot (0).
    channel_jobs: dict[int, _Job] = {}
    channel_jobs_lock = threading.Lock()
    CHANNEL_COOKIE = "wr_channel"

    def _job_for_channel(cid: int) -> _Job:
        with channel_jobs_lock:
            slot = channel_jobs.get(cid)
            if slot is None:
                slot = channel_jobs[cid] = _Job(cid,
                                                log_path=_studio_log_path(cfg))
            return slot

    def _active_channel_id() -> int:
        """The own channel selected in the studio (0 = none selected)."""
        try:
            return max(0, int(request.cookies.get(CHANNEL_COOKIE) or 0))
        except (ValueError, RuntimeError):
            return 0

    def _selected_channel() -> int | None:
        """The studio's selected channel: an own channel id, 0 for the
        'no channel' bucket, None when nothing is selected yet."""
        raw = request.cookies.get(CHANNEL_COOKIE)
        if raw is None or raw == "":
            return None
        try:
            return max(0, int(raw))
        except ValueError:
            return None

    def _remember_channel(resp, cid: int):
        resp.set_cookie(CHANNEL_COOKIE, str(int(cid or 0)),
                        max_age=60 * 60 * 24 * 365, samesite="Lax")
        return resp

    def _current_job() -> _Job:
        tls_job = getattr(_job_tls, "job", None)
        if tls_job is not None:
            return tls_job
        if has_request_context():
            cached = getattr(g, "_wr_job", None)
            if cached is not None:
                return cached
            cid = 0
            pid = ((request.view_args or {}).get("pid")
                   or request.args.get("pid", type=int))
            if pid:
                conn = db.connect(cfg.db_path)
                try:
                    row = db.get_production(conn, pid)
                    cid = int(row["own_channel_id"] or 0) if row else 0
                finally:
                    conn.close()
            else:
                cid = _active_channel_id()
            slot = _job_for_channel(cid)
            g._wr_job = slot
            return slot
        return _job_for_channel(0)

    class _JobHandle:
        """Forwards every attribute to the current slot (see above)."""

        def __getattr__(self, name):
            return getattr(_current_job(), name)

        def __setattr__(self, name, value):
            setattr(_current_job(), name, value)

        def _real(self) -> _Job:
            return _current_job()

    sjob = _JobHandle()
    # the scheduled "produce from channels" run spans channels, so it keeps a
    # slot of its own
    scheduler_job = _Job(-1, "scheduled auto-run",
                         log_path=_studio_log_path(cfg))

    def _other_running_jobs() -> list[dict]:
        """Jobs running in channels other than the current one, for the
        'also running' note."""
        here = _current_job()
        with channel_jobs_lock:
            slots = list(channel_jobs.values())
        slots.append(scheduler_job)
        return [{"channel_id": j.channel_id, "kind": j.kind, "pid": j.pid,
                 "scheduled": j.channel_id == -1}
                for j in slots if j is not here and j.running]

    # Manual batch: queue productions that auto-run one after another, each
    # stopping after merge (review stays a human decision). Optionally shut the
    # PC down a few minutes after the LAST one finishes, so an unattended run
    # can power the machine off.
    BATCH_SHUTDOWN_SECONDS = 300
    batches: dict[int, dict] = {}

    def _batch_for(cid: int) -> dict:
        with channel_jobs_lock:
            if cid not in batches:
                batches[cid] = {"queue": [], "lock": threading.Lock(),
                                "shutdown_at": None}
            return batches[cid]

    class _BatchHandle:
        """The batch queue of the current channel (same indexing as a dict)."""

        def __getitem__(self, key):
            return _batch_for(_current_job().channel_id)[key]

        def __setitem__(self, key, value):
            _batch_for(_current_job().channel_id)[key] = value

    batch = _BatchHandle()

    def _batch_shutdown_cmd() -> list[str]:
        if os.name == "nt":
            return ["shutdown", "/s", "/t", str(BATCH_SHUTDOWN_SECONDS)]
        return ["shutdown", "-h", f"+{max(1, BATCH_SHUTDOWN_SECONDS // 60)}"]

    def _batch_cancel_shutdown_cmd() -> list[str]:
        return ["shutdown", "/a"] if os.name == "nt" else ["shutdown", "-c"]

    def _batch_spawn(cmd: list[str]) -> None:
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception as exc:  # noqa: BLE001 - never crash the batch
            sjob.log.append(f"[batch] could not run {' '.join(cmd)}: {exc}")

    def _run_batch(pids: list[int], shutdown: bool) -> None:
        """Auto-run each queued production to merge, one after another."""
        done = []
        for pid in pids:
            if sjob.cancel:
                sjob.log.append("[batch] stopped by user - remaining job(s) "
                                "left in the queue")
                break
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            try:
                prod = db.get_production(conn, pid)
                title = prod["title"] if prod else f"#{pid}"
            finally:
                conn.close()
            sjob.pid = pid
            sjob.log.append(f"[batch] production {pid} ({title}): auto-run")
            result = autorun.run_pipeline(cfg, pid, job=sjob._real(),
                                          log=sjob.log.append)
            done.append((pid, result))
            sjob.log.append(f"[batch] production {pid}: {result}")
        ok = sum(1 for _, r in done if r == "ok")
        sjob.log.append(f"[batch] finished: {len(done)} job(s) run, {ok} "
                        f"reached merge - review stays manual")
        if shutdown and not sjob.cancel and _other_running_jobs():
            sjob.log.append("[batch] not shutting down: another channel is "
                            "still running a job")
        elif shutdown and not sjob.cancel:
            _batch_spawn(_batch_shutdown_cmd())
            batch["shutdown_at"] = time.time() + BATCH_SHUTDOWN_SECONDS
            cancel = ("shutdown /a" if os.name == "nt" else "shutdown -c")
            sjob.log.append(
                f"[batch] shutting down in {BATCH_SHUTDOWN_SECONDS // 60} "
                f"minute(s) - cancel it with: {cancel}")

    class _DequeHandler(logging.Handler):
        def emit(self, record):
            job.log.append(record.getMessage())

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, _DequeHandler) for h in root.handlers):
        root.addHandler(_DequeHandler(level=logging.INFO))

    def _start_producer(channels: int, log_fn) -> None:
        """Start an Auto Run in the studio job slot (used by the scheduler)."""
        if scheduler_job.running:
            log_fn("producer: a scheduled run is already going - skipping")
            return

        def worker():
            result = producer.run(cfg, log=scheduler_job.log.append,
                                  job=scheduler_job)
            scheduler_job.log.append(
                f"=== scheduled produce: {len(result['created'])} created, "
                f"{len(result['skipped'])} skipped, result={result['result']} ===")

        scheduler_job.start(worker,
                            f"scheduled auto-run ({channels} channel(s))")

    sched = scheduler.Scheduler(cfg, scheduler_job, _start_producer)
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
            services.MANAGER.ensure(cfg, ["renderly"],
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
        # paused channels are pruned from the dashboard: their videos stay in
        # the DB but neither the list nor the dropdown/genre chips show them
        channels = db.list_channels(conn, active_only=True)
        status = request.args.get("status") or None
        backlog = status == "backlog"
        if backlog:
            status = None
        genre = request.args.get("genre") or None
        channel = request.args.get("channel") or None
        sort = request.args.get("sort") or None
        q = (request.args.get("q") or "").strip() or None
        # page size: an explicit ?per_page= wins and is remembered in a cookie
        per_page_arg = request.args.get("per_page")
        remember_size = per_page_arg is not None
        if remember_size:
            per_page = _page_size(_safe_int(per_page_arg, PAGE_SIZE),
                                  PAGE_SIZE)
        else:
            per_page = _page_size(
                _safe_int(request.cookies.get(PER_PAGE_COOKIE), PAGE_SIZE),
                PAGE_SIZE)
        page = _safe_int(request.args.get("page"), 1, lo=1)

        def fetch(page_no: int):
            return db.get_videos_page(
                conn, status=status, genre=genre, backlog=backlog,
                channel=channel, q=q, sort=sort,
                limit=per_page, offset=(page_no - 1) * per_page,
                active_only=True)

        videos, total = fetch(page)
        pages = max(1, (total + per_page - 1) // per_page)
        if page > pages:  # out-of-range page: clamp to the last page
            page = pages
            videos, total = fetch(page)
        genres = sorted({ch["genre"] for ch in channels})
        conn.close()
        raw_status = request.args.get("status")

        def qs(**overrides) -> str:
            """Query string preserving current filters; overrides replace them."""
            vals = {"status": raw_status, "genre": genre, "channel": channel,
                    "sort": sort, "q": q,
                    "per_page": per_page if per_page != PAGE_SIZE else None}
            page_override = overrides.pop("page", None)
            vals.update(overrides)
            parts = [f"{k}={quote(str(v))}" for k, v in vals.items() if v]
            if page_override:
                parts.append(f"page={page_override}")
            return "&".join(parts)

        resp = make_response(render_template(
            "dashboard.html",
            channels=channels,
            videos=videos,
            genres=genres,
            status=raw_status,
            genre=genre,
            channel=channel,
            sort=sort,
            q=q,
            per_page=per_page,
            per_page_options=PAGE_SIZES,
            page=page,
            pages=pages,
            total=total,
            page_list=_page_list(page, pages),
            qs=qs,
            msg=request.args.get("msg"),
            error=request.args.get("error"),
            job=job,
            log_text="\n".join(list(job.log))[-4000:],
        ))
        if remember_size:
            resp.set_cookie(PER_PAGE_COOKIE, str(per_page),
                            max_age=60 * 60 * 24 * 365, samesite="Lax")
        return resp

    @app.get("/research")
    def research():
        """What to make: videos that beat their own channel's norm (see
        outliers.py). Built from the stored view counts of the watched
        channels - refresh them from the Watched Channels page."""
        from . import outliers
        args = request.args
        tab = "outliers"

        def _num(name, default, cast=float):
            try:
                return cast(args.get(name, default))
            except (TypeError, ValueError):
                return default

        min_mult = max(1.0, _num("mult", 3.0))
        age_raw = (args.get("age") or "").strip()
        max_age = float(age_raw) if age_raw.isdigit() else None
        sort = args.get("sort") if args.get("sort") in outliers.SORTS \
            else "multiplier"
        channel_id = (args.get("channel") or "").strip()
        genre = (args.get("genre") or "").strip()
        hide_shorts = args.get("shorts") != "show"
        q = (args.get("q") or "").strip()
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        rows = conn.execute(
            "SELECT v.video_id, v.channel_id, c.name AS channel_name,"
            " c.genre AS genre, v.title, v.url, v.published_at,"
            " v.view_count, v.duration, v.status FROM videos v"
            " JOIN channels c ON c.channel_id = v.channel_id"
            " WHERE c.active = 1 AND v.view_count IS NOT NULL").fetchall()
        channels = [c for c in db.list_channels(conn) if c["active"]]
        own = [o for o in db.list_own_channels(conn) if o["active"]]
        total_videos = conn.execute(
            "SELECT COUNT(*) FROM videos v JOIN channels c ON"
            " c.channel_id = v.channel_id WHERE c.active = 1").fetchone()[0]
        undated = conn.execute(
            "SELECT COUNT(*) FROM videos v JOIN channels c ON"
            " c.channel_id = v.channel_id WHERE c.active = 1 AND"
            " v.view_count IS NOT NULL AND v.published_at IS NULL"
            ).fetchone()[0]
        conn.close()
        items = outliers.build(rows)
        shown, matched = outliers.filter_sort(
            items, min_multiplier=min_mult, max_age_days=max_age,
            channel_id=channel_id, genre=genre, hide_shorts=hide_shorts,
            q=q, sort=sort)
        genres = sorted({c["genre"] for c in channels if c["genre"]})
        return render_template(
            "research.html", tab=tab, rows=shown, matched=matched,
            scored=len(items), with_views=len(rows), total_videos=total_videos,
            undated=undated, channels=channels, genres=genres, own=own,
            selected_own=_selected_channel(), fmt_views=outliers.fmt_views,
            fmt_age=outliers.fmt_age,
            f={"mult": f"{min_mult:g}", "age": age_raw, "sort": sort,
               "channel": channel_id, "genre": genre, "q": q,
               "shorts": "hide" if hide_shorts else "show"},
            msg=request.args.get("msg"), error=request.args.get("error"))

    @app.get("/watched")
    def watched_channels():
        """The competitor/source channels WhisperRadar monitors for new
        uploads - split out of the dashboard (2026-09-27) so a long watch
        list doesn't push the videos list below the fold.

        ?state=paused shows the pruned (paused) channels instead of the
        active ones; both views keep the same edit/remove actions."""
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        db.sync_channels(conn, cfg.channels)
        all_channels = db.list_channels(conn)
        conn.close()
        state = "paused" if request.args.get("state") == "paused" else "active"
        channels = [c for c in all_channels
                    if (c["active"] if state == "active" else not c["active"])]
        return render_template(
            "watched.html",
            channels=channels,
            active_count=sum(1 for c in all_channels if c["active"]),
            paused_count=sum(1 for c in all_channels if not c["active"]),
            state=state,
            msg=request.args.get("msg"),
            error=request.args.get("error"),
        )

    @app.post("/channels/state")
    def channels_state():
        """One-click pause/activate from the watched list (the edit row's
        dropdown does the same via /channels/edit)."""
        channel_id = request.form.get("channel_id") or ""
        active = 1 if request.form.get("active") == "1" else 0
        state = "paused" if request.form.get("state") == "paused" else "active"
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            if not db.get_channel(conn, channel_id):
                return redirect("/watched?error=Unknown+channel")
            db.update_channel(conn, channel_id, active=active)
        finally:
            conn.close()
        view = "?state=paused" if state == "paused" else ""
        msg = quote("Channel activated" if active else "Channel paused")
        return redirect(f"/watched{view}&msg={msg}" if view else f"/watched?msg={msg}")

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
                return redirect("/watched?error=Unknown+channel")
            db.update_channel(conn, channel_id, name=name or row["name"],
                              kind=kind, genre=genre, active=active)
            if row["genre"] != genre:
                pipeline.move_channel_files(cfg, conn, row, genre)
        finally:
            conn.close()
        return _back(request, msg="Channel updated", base="/watched")

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
            return redirect("/watched?error=Enter+a+channel+URL")
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
            return _back(request, msg="Channel added", base="/watched")
        except Exception as exc:
            return _back(request, error=f"Could not resolve channel: {exc}",
                         base="/watched")
        finally:
            conn.close()

    @app.post("/channels/backfill")
    def channels_backfill():
        """Import a channel's full upload history into the backlog (the RSS
        feed only lists the latest 15). Runs in the job slot - yt-dlp can take
        a while on a big channel."""
        key = (request.form.get("key") or "").strip()
        limit_raw = (request.form.get("limit") or "").strip()
        try:
            limit = int(limit_raw) if limit_raw else None
        except ValueError:
            limit = None
        log = logging.getLogger("whisperradar")

        def worker():
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            try:
                if key and key != "all":
                    row = db.get_channel(conn, key)
                    rows = [row] if row else []
                else:
                    rows = db.list_channels(conn, active_only=True)
                if not rows:
                    log.warning("backfill: no matching channel")
                    return
                total = 0
                for ch in rows:
                    total += pipeline.import_history(cfg, conn, ch, limit=limit)
                log.info("backfill finished: %d new backlog video(s)", total)
            finally:
                conn.close()

        if not job.start(worker, "history backfill"):
            return _back(request, error="A job is already running",
                         base="/watched")
        return _back(request, msg="Importing channel history - watch the log",
                     base="/watched")

    @app.post("/channels/views")
    def channels_views():
        """Refresh a channel's stored view counts (they are not in the RSS
        feed) so the "most viewed" filter has data."""
        key = (request.form.get("key") or "").strip()
        log = logging.getLogger("whisperradar")

        def worker():
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            try:
                if key and key != "all":
                    row = db.get_channel(conn, key)
                    rows = [row] if row else []
                else:
                    rows = db.list_channels(conn, active_only=True)
                if not rows:
                    log.warning("views: no matching channel")
                    return
                total = 0
                for ch in rows:
                    total += pipeline.refresh_view_counts(cfg, conn, ch)
                log.info("view counts refreshed: %d row(s)", total)
            finally:
                conn.close()

        if not job.start(worker, "view counts"):
            return _back(request, error="A job is already running",
                         base="/watched")
        return _back(request, msg="Refreshing view counts - watch the log",
                     base="/watched")

    @app.post("/channels/remove")
    def channels_remove():
        key = (request.form.get("key") or "").strip()
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            db.remove_channel(conn, key)
        finally:
            conn.close()
        return redirect("/watched?msg=Channel+removed")

    # ---------------------------------------------------------- settings ---
    # Global Auto Run criteria. Own channels live on /my-channels; monitored
    # source channels stay on / (Dashboard).

    def _global_display_values() -> dict:
        """Global value of every apply-to-all field, as the strings the channel
        form's <option value>s use, so an "inherit" choice can say what it
        inherits."""
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            values = settings.load(conn)
        finally:
            conn.close()
        out = {}
        for key in db.APPLY_ALL_FIELDS:
            v = values.get(key)
            out[key] = ("" if v is None else "1" if v is True
                        else "0" if v is False else str(v))
        return out

    @app.get("/settings")
    def settings_page():
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            values = settings.load(conn)
            override_counts = {k: len(db.channels_overriding(conn, k))
                               for k in db.APPLY_ALL_FIELDS}
        finally:
            conn.close()
        return render_template(
            "settings.html", values=values, spec=settings.SPEC,
            groups=settings.grouped_spec(),
            apply_all_keys=db.APPLY_ALL_FIELDS,
            override_counts=override_counts,
            providers=[p["name"] for p in studio.providers(cfg)],
            providers_nested=studio.providers_nested(cfg),
            scheduler=sched.status(),
            services=services.MANAGER.status_cached(cfg),
            flowbatch_ready=studio.flowbatch_ready(cfg),
            msg=request.args.get("msg"), error=request.args.get("error"))

    @app.post("/services/<name>/<action>")
    def services_control(name, action):
        """Start/stop one external tool from the dashboard, so no .bat file is
        needed. Stop only ever touches a service WhisperRadar started."""
        if name != "renderly":
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
            # every checkbox posts a hidden "0" first, so an unticked box
            # arrives as "0"; the LAST value of a key is the real one
            form = {k: request.form.getlist(k)[-1] for k in request.form}
            _, warnings = settings.save(conn, form)
            # "apply to all channels": make every channel inherit the value
            applied = []
            wanted = request.form.getlist("apply_all")
            if wanted and not settings.load(conn)["show_apply_all"]:
                warnings.append("apply to all channels is switched off "
                                "(Settings > Service handling)")
                wanted = []
            for key in settings.with_partners(wanted):
                if key not in db.APPLY_ALL_FIELDS:
                    warnings.append(f"{key}: cannot be applied to all channels")
                    continue
                names = db.reset_channel_overrides(conn, key)
                label = settings.SPEC_BY_KEY[key]["label"]
                shown = settings.load(conn)[key]
                if names:
                    applied.append(
                        f"{label}: all channels now follow the global value"
                        f" ({shown}) - {len(names)} reset: "
                        + ", ".join(names))
                else:
                    applied.append(
                        f"{label}: no channel had its own value - every "
                        f"channel already follows the global value ({shown})")
        finally:
            conn.close()
        msg = "Settings saved"
        if applied:
            msg += " - " + "; ".join(applied)
        if warnings:
            msg += " - " + "; ".join(warnings)
        return redirect("/settings?msg=" + quote(msg))

    @app.post("/settings/providers")
    def settings_providers_save():
        """Save the nested gateway/model list the Providers editor posts.

        One entry per gateway + key, each with several models; studio.providers()
        flattens it to the flat {name, base_url, model} entries the pipeline uses,
        so several models share a key."""
        raw = (request.form.get("providers_json") or "").strip()
        try:
            data = json.loads(raw) if raw else []
        except ValueError:
            return redirect("/settings?error=Providers+must+be+valid+JSON")
        if not isinstance(data, list):
            return redirect("/settings?error=Providers+must+be+a+list")
        clean = []
        for prov in data:
            if not isinstance(prov, dict):
                continue
            name = str(prov.get("name") or "").strip()
            base_url = str(prov.get("base_url") or "").strip()
            if not name or not base_url:
                continue
            models = []
            for model in (prov.get("models") or []):
                if isinstance(model, str):
                    model = {"id": model}
                if not isinstance(model, dict):
                    continue
                mid = str(model.get("id") or "").strip()
                if not mid:
                    continue
                models.append({"id": mid,
                               "name": str(model.get("name") or mid).strip()})
            if not models:
                continue
            clean.append({
                "name": name,
                "base_url": base_url,
                "api_key": str(prov.get("api_key") or "").strip(),
                "env_key": str(prov.get("env_key") or "WR_LLM_API_KEY").strip(),
                "models": models,
            })
        if not clean:
            # an empty list would leave the pipeline with NO providers (there
            # is no config.yaml fallback) - refuse instead of saving
            return redirect("/settings?error="
                            + quote("No valid gateway in the list (each needs "
                                    "a name, base URL and a model) - nothing "
                                    "was saved"))
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            try:
                old_names = {p.get("name") for p in json.loads(
                    db.get_setting(conn, "llm_providers") or "[]")}
            except ValueError:
                old_names = set()
            removed = sorted(n for n in old_names
                             if n and n not in {p["name"] for p in clean})
            db.set_setting(conn, "llm_providers", json.dumps(clean))
            if removed:
                # a deleted provider must not survive in the places that
                # pointed at it (Default LLM, judge picks, channel producer
                # LLM, production pins) - they would keep raising errors
                # about a provider that no longer exists
                marks = ",".join("?" for _ in removed)
                conn.execute(
                    "UPDATE settings SET value = '' WHERE key IN "
                    "('llm_default', 'script_judge_provider', "
                    "'shotlist_judge_provider', 'llm_fallback_provider') "
                    f"AND value IN ({marks})", tuple(removed))
                conn.execute(
                    "UPDATE own_channels SET producer_llm_provider = NULL "
                    f"WHERE producer_llm_provider IN ({marks})",
                    tuple(removed))
                conn.execute(
                    "UPDATE productions SET llm_provider = NULL "
                    f"WHERE llm_provider IN ({marks})", tuple(removed))
            conn.commit()
        finally:
            conn.close()
        label = ", ".join(p["name"] for p in clean) or "none"
        logging.getLogger("whisperradar").info("providers saved: %s", label)
        return redirect("/settings?msg=" + quote(f"Providers saved ({label})"))

    @app.post("/settings/providers/reset")
    def settings_providers_reset():
        """A full fresh start for the LLM setup: remove every configured
        provider AND every reference to them - the gateway list, the Default
        LLM, both judge picks, the pinned fallback LLM, the channels'
        producer LLM and any production pins. Stale references to a deleted
        provider otherwise keep surfacing (preselected dropdowns,
        judge/fallback picks) even after the provider itself is gone."""
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            conn.execute("DELETE FROM settings WHERE key IN "
                         "('llm_providers', 'llm_default', "
                         "'script_judge_provider', 'shotlist_judge_provider', "
                         "'llm_fallback_provider')")
            conn.execute("UPDATE own_channels "
                         "SET producer_llm_provider = NULL")
            conn.execute("UPDATE productions SET llm_provider = NULL")
            conn.commit()
        finally:
            conn.close()
        logging.getLogger("whisperradar").info(
            "LLM providers reset - all providers and references removed")
        return redirect("/settings?msg=" + quote(
            "LLM providers reset - add your gateway, then pick a Default LLM"))

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
            # can be pointed at real sources instead of guessing the spelling.
            # Paused channels do not count: Auto Run skips them, so a genre
            # only they carry would silently yield no candidates.
            genres = [r["genre"] for r in conn.execute(
                "SELECT DISTINCT genre FROM channels"
                " WHERE genre IS NOT NULL AND genre <> '' AND active = 1"
                " ORDER BY genre COLLATE NOCASE")]
            source_genres = {g.lower() for g in genres}
            _loaded = settings.load(conn)
            global_render_resolution = _loaded["render_resolution"]
            global_apply_all = bool(_loaded["show_apply_all"])
            # paused watched channels are pruned from the tick list too;
            # already-stored picks of them survive via hidden inputs
            watched_channels = db.list_channels(conn, active_only=True)
            watched_by_oc = {c["id"]: db.own_channel_watched(c)
                             for c in own_channels}
        finally:
            conn.close()
        return render_template(
            "channels.html", own_channels=own_channels, links=links,
            watched_channels=watched_channels, watched_by_oc=watched_by_oc,
            renderly_url=cfg.renderly_url, genres=genres,
            source_genres=source_genres,
            providers=[p["name"] for p in studio.providers(cfg)],
            render_targets=studio.RENDER_TARGETS,
            render_target_labels=studio.RENDER_TARGET_LABELS,
            render_resolutions=studio.RENDER_RESOLUTIONS,
            render_resolution_labels=studio.RENDER_RESOLUTION_LABELS,
            global_render_resolution=global_render_resolution,
            apply_all_fields=(sorted(db.APPLY_ALL_FIELDS)
                              if global_apply_all else []),
            global_values=_global_display_values(),
            upscale_labels=settings.UPSCALE_LABELS,
            native_tiers=settings.FLOW_NATIVE_TIERS,
            native_tier_labels=settings.FLOW_NATIVE_TIER_LABELS,
            brief_presets=briefs.MOTION_PRESETS,
            brief_custom_label=briefs.CUSTOM_LABEL,
            brief_motion_codes=briefs.MOTION_CODE_INFO,
            brief_starters=briefs.PRESENTATION_STARTERS,
            brief_type_codes=briefs.TYPE_CODES,
            msg=request.args.get("msg"), error=request.args.get("error"))

    @app.get("/my-channels/export")
    def my_channels_export():
        """Download channel settings as JSON: the channels picked with
        repeated ?id= or, with none, all of them."""
        keys = [k.strip() for k in request.args.getlist("id") if k.strip()]
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            doc = channel_io.export_channels(conn, keys or None)
        finally:
            conn.close()
        if not doc["channels"]:
            return redirect("/my-channels?error=" + quote(
                "Unknown channel" if keys else "No channels to export"))
        if len(doc["channels"]) == 1:
            slug = _slugify(doc["channels"][0]["name"]) or "channel"
        else:
            slug = (f"channels-{len(doc['channels'])}-"
                    + time.strftime("%Y%m%d"))
        resp = make_response(json.dumps(doc, indent=2, ensure_ascii=False))
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="whisperradar-{slug}.json"')
        return resp

    @app.post("/my-channels/import")
    def my_channels_import():
        """Load channel settings from an exported JSON file. A channel is
        matched by name; existing ones are kept unless 'overwrite' is ticked."""
        f = request.files.get("channels_file")
        if f is None or not f.filename:
            return redirect("/my-channels?error=" + quote("Choose a JSON file"))
        try:
            payload = json.loads(f.read().decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError):
            return redirect("/my-channels?error=" + quote(
                "That file is not valid JSON"))
        overwrite = request.form.get("overwrite") in ("1", "on", "true")
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            res = channel_io.import_channels(conn, cfg, payload, overwrite)
        finally:
            conn.close()
        if res["error"]:
            return redirect("/my-channels?error=" + quote(res["error"]))
        parts = []
        for label, key in (("created", "created"), ("updated", "updated"),
                           ("kept as they were", "skipped")):
            if res[key]:
                parts.append(f"{label}: {', '.join(res[key])}")
        msg = "Import done - " + ("; ".join(parts) or "nothing to import")
        if res["notes"]:
            msg += " | " + " | ".join(res["notes"][:6])
            if len(res["notes"]) > 6:
                msg += f" | +{len(res['notes']) - 6} more"
        return redirect("/my-channels?msg=" + quote(msg))

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

    def _brief_fields(form):
        """The planning-brief settings posted in `form` -> (fields, error).
        Only keys present in the form are read, so the same parsing serves
        the whole channel form and the planning-brief section's own save."""
        fields = {}
        if "brief_motion" in form:
            raw = (form.get("brief_motion") or "").strip().lower()
            fields["brief_motion"] = (
                raw if (raw in briefs.MOTION_PRESETS
                        and raw != briefs.STANDARD.key)
                or raw == briefs.CUSTOM_KEY
                else None)   # empty/standard = default
            if raw == briefs.CUSTOM_KEY:
                spec = briefs.normalize_custom({
                    "allowed": form.getlist("custom_allowed"),
                    "st_max_share": form.get("custom_st_share"),
                    "st_max_hold": form.get("custom_st_hold"),
                    "code_max_share": form.get("custom_code_share"),
                    "pan_max_share": form.get("custom_pan_share"),
                    "tilt_max_share": form.get("custom_tilt_share"),
                    "rules": form.get("custom_rules"),
                })
                problem = briefs.custom_error(spec)
                if spec and not problem:
                    for key, codes in briefs.CODE_GROUPS.items():
                        if spec.get(key):    # (ZI/ZO take what is left)
                            continue
                        if any(c in spec["allowed"] for c in codes):
                            problem = (f"enter a max share for "
                                       f"{' / '.join(codes)}")
                            break
                if problem:
                    return fields, (f"Custom motion: {problem} - "
                                    "nothing was saved")
                # kept when the channel switches back to a preset, so the
                # custom settings are still there next time
                fields["brief_custom"] = json.dumps(spec)
        hold = {}
        for key in ("brief_min_hold", "brief_max_hold"):
            if key in form:
                raw = (form.get(key) or "").strip()
                try:   # empty = the preset's / the global value
                    hold[key] = (max(1.0, min(300.0, float(raw)))
                                 if raw else None)
                except ValueError:
                    hold[key] = None
        if hold.get("brief_min_hold") and hold.get("brief_max_hold") \
                and hold["brief_min_hold"] >= hold["brief_max_hold"]:
            return fields, ("Planning brief: the minimum hold must be "
                            "shorter than the maximum hold - nothing was saved")
        fields.update(hold)
        if "types_form" in form:   # the shot-type block was on the form
            raw = {"max": {}, "host": {},
                   "host_ref": (form.get("type_host_ref") or "").strip()}
            gv = (form.get("type_max_graphics") or "").strip()
            if gv:
                raw["graphics_max"] = gv
            for code in briefs.TYPE_CODES:
                mv = (form.get(f"type_max_{code}") or "").strip()
                hv = (form.get(f"type_host_{code}") or "").strip()
                if mv:
                    raw["max"][code] = mv
                if hv:
                    raw["host"][code] = hv
            spec = briefs.normalize_types(raw)
            fields["brief_types"] = json.dumps(spec) if spec else None
        if "brief_reveal" in form:
            level = (form.get("brief_reveal") or "").strip().lower()
            fields["brief_reveal"] = (
                2 if level == "2" else
                1 if level in ("1", "on", "true") else None)
        if "brief_sfx" in form:
            name = (form.get("brief_sfx") or "").strip()
            fields["brief_sfx"] = (
                name if studio.SFX_NAME_RE.match(name) and name != "pop"
                else None)
        if "brief_presentation" in form:   # multi-line: keep newlines
            fields["brief_presentation"] = (
                (form.get("brief_presentation") or "").strip() or None)
        return fields, None

    @app.post("/my-channels/brief")
    def my_channels_brief():
        """Save only the planning-brief settings (motion preset or custom
        spec, hold range, presentation) - nothing else on the channel form is
        read, so a half-edited name or gate elsewhere is left alone."""
        oc_id = _own_channel_id()
        if not oc_id:
            return redirect("/my-channels?error=Unknown+channel")
        fields, error = _brief_fields(request.form)
        if error:
            return redirect("/my-channels?error=" + quote(error)
                            + f"#edit-{oc_id}-brief")
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            if not db.get_own_channel(conn, oc_id):
                return redirect("/my-channels?error=Unknown+channel")
            if fields:
                db.update_own_channel(conn, oc_id, **fields)
        finally:
            conn.close()
        return redirect("/my-channels?msg=" + quote("Planning brief saved")
                        + f"#edit-{oc_id}-brief")

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
        if "watched_present" in request.form:
            conn = db.connect(cfg.db_path)
            try:
                known = {c["channel_id"] for c in db.list_channels(conn)}
            finally:
                conn.close()
            picked = [w for w in request.form.getlist("watched") if w in known]
            fields["watched_channels"] = json.dumps(picked) if picked else None
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
            known = {p["name"] for p in studio.providers(cfg)}
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
            known = {p["name"] for p in studio.providers(cfg)}
            fields["script_judge_provider"] = raw if raw in known else None
        if "render_target" in request.form:
            raw = (request.form.get("render_target") or "").strip().lower()
            fields["render_target"] = (raw if raw in studio.RENDER_TARGETS
                                       else None)   # empty = inherit
        if "render_resolution" in request.form:
            raw = (request.form.get("render_resolution") or "").strip().lower()
            fields["render_resolution"] = (raw if raw in studio.RENDER_RESOLUTIONS
                                           else None)   # empty = inherit
        if "flow_native_upscale" in request.form:
            raw = (request.form.get("flow_native_upscale") or "").strip().lower()
            fields["flow_native_upscale"] = (
                raw if raw in settings.FLOW_NATIVE_TIERS else None)  # ""=inherit
        brief_fields, brief_error = _brief_fields(request.form)
        if brief_error:
            return redirect("/my-channels?error=" + quote(brief_error))
        fields.update(brief_fields)
        if "generate_references" in request.form:
            raw = (request.form.get("generate_references") or "").strip()
            fields["generate_references"] = (None if raw == ""
                                             else 1 if raw in ("1", "on", "true")
                                             else 0)
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
                fields["shotlist_max_attempts"] = (max(1, min(8, int(raw)))
                                                   if raw else None)
            except ValueError:
                fields["shotlist_max_attempts"] = None
        if "shotlist_judge_provider" in request.form:
            raw = (request.form.get("shotlist_judge_provider") or "").strip()
            known = {p["name"] for p in studio.providers(cfg)}
            fields["shotlist_judge_provider"] = raw if raw in known else None
        if "default_upscale" in request.form:
            raw = (request.form.get("default_upscale") or "").strip()
            try:
                fields["default_upscale"] = (max(0, min(4, int(raw)))
                                             if raw else None)
            except ValueError:
                fields["default_upscale"] = None
        # Render resolution + upscale tier are one decision; the resolution wins
        pair_warning = ""
        if "render_resolution" in fields or "default_upscale" in fields:
            res, up, pair_warning = settings.reconcile_resolution_upscale(
                fields.get("render_resolution"), fields.get("default_upscale"),
                "render_resolution" in fields, "default_upscale" in fields)
            if "render_resolution" in fields:
                fields["default_upscale"] = up
            elif res:
                fields["render_resolution"] = res
                fields["default_upscale"] = up
            elif "default_upscale" in fields:
                fields["default_upscale"] = up
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
            # "apply to all channels": copy this channel's saved value
            applied, rejected = [], []
            wanted = request.form.getlist("apply_all")
            if wanted and not settings.load(conn)["show_apply_all"]:
                rejected.append("(apply to all channels is switched off in "
                                "Settings > Service handling)")
                wanted = []
            for key in settings.with_partners(wanted):
                if key not in db.APPLY_ALL_FIELDS:
                    rejected.append(key)
                    continue
                names = db.copy_channel_value_to_all(conn, oc_id, key)
                what = key.replace("_", " ")
                if names:
                    applied.append(f"{what} copied to {len(names)} other "
                                   f"channel(s): " + ", ".join(names))
                else:
                    applied.append(f"{what}: every other channel already "
                                   f"had this value")
        finally:
            conn.close()
        msg = "Channel updated"
        if pair_warning:
            msg += " - " + pair_warning
        if applied:
            msg += " - " + "; ".join(applied)
        if rejected:
            msg += " - not applicable to all channels: " + ", ".join(rejected)
        return redirect("/my-channels?msg=" + quote(msg))

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
            elif action == "mark_produced":
                db.set_video(conn, video_id, produced=1)
            elif action == "reenable":
                db.set_video(conn, video_id, produced=0)
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

    @app.post("/videos/bulk")
    def videos_bulk():
        """Multi-select counterpart of /videos/action and /videos/delete -
        the dashboard's row checkboxes post their video_ids here in one
        request instead of one page reload per video."""
        video_ids = [v for v in request.form.getlist("video_ids") if v]
        action = request.form.get("action") or ""
        if not video_ids:
            return _back(request, error="No videos selected")
        if action in ("download", "transcribe"):
            fn = ((lambda: _download_many(cfg, video_ids)) if action == "download"
                  else (lambda: _transcribe_many(cfg, video_ids)))
            if not job.start(fn, action):
                return _back(request, error="A job is already running")
            verb = "Downloading" if action == "download" else "Transcribing"
            return _back(request, msg=f"{verb} {len(video_ids)} video(s)")
        if action not in ("queue", "retry", "delete"):
            return _back(request, error="Unknown action")
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            n = 0
            for video_id in video_ids:
                row = db.get_video(conn, video_id)
                if not row:
                    continue
                if action == "queue":
                    db.set_video(conn, video_id, auto=1, status="new", error=None)
                elif action == "retry":
                    db.set_video(
                        conn, video_id,
                        status="downloaded" if row["audio_path"] else "new",
                        error=None)
                elif action == "delete":
                    db.delete_video(conn, video_id)
                    for key in ("audio_path", "transcript_path"):
                        p = row[key]
                        if p:
                            Path(p).unlink(missing_ok=True)
                n += 1
        finally:
            conn.close()
        verb = {"queue": "Queued", "retry": "Retrying", "delete": "Deleted"}[action]
        return _back(request, msg=f"{verb} {n} video(s)")
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
        active_channels = [c for c in own_channels if c["active"]]
        own_by_id = {c["id"]: c["name"] for c in own_channels}
        selected = _selected_channel()
        # no own channels yet: nothing to choose between, show everything as
        # before. Otherwise the studio works inside ONE channel at a time.
        gated = bool(active_channels)
        if gated and selected is not None and selected \
                and selected not in {c["id"] for c in active_channels}:
            selected = None          # a removed / deactivated channel
        in_channel = (not gated) or selected is not None
        prods = []
        finished_count = 0
        counts: dict[int, int] = {}
        for p in db.list_productions(conn):
            if p["status"] in ("ready", "published"):
                # Approved/published productions move to their own
                # "Finished" page instead of staying mixed into this
                # in-progress list.
                if (not gated) or selected is None \
                        or (p["own_channel_id"] or 0) == selected:
                    finished_count += 1
                continue
            cid = p["own_channel_id"] or 0
            counts[cid] = counts.get(cid, 0) + 1
            if gated and (selected is None or cid != selected):
                continue
            steps = db.latest_steps(conn, p["id"])
            done = sum(1 for s in db.STAGES if s in steps)
            prods.append({"row": p, "done": done, "total": len(db.STAGES),
                          "own_channel": own_by_id.get(p["own_channel_id"])})
        # Every transcribed video is listed; the ones this channel has already
        # built (in production, failed, ready, published) are greyed out with
        # a tag instead of hidden. Per channel: another channel's production
        # of the same video does not block this one.
        sources = db.get_videos(conn, status="transcribed", limit=500)
        # a channel linked to specific watched channels draws sources only
        # from them (none linked = all of them, as before)
        selected_row = (db.get_own_channel(conn, selected)
                        if selected else None)
        watched = db.own_channel_watched(selected_row) if selected_row else []
        if watched:
            sources = [v for v in sources if v["channel_id"] in set(watched)]
        source_tags = db.source_use_tags(conn, selected, sources)
        conn.close()
        title_by_id = {p["row"]["id"]: p["row"]["title"] for p in prods}
        with batch["lock"]:
            queued = [{"id": pid, "title": title_by_id.get(pid, f"#{pid}")}
                      for pid in batch["queue"]]
        shutdown_pending = bool(batch["shutdown_at"]
                                and batch["shutdown_at"] > time.time())
        if batch["shutdown_at"] and not shutdown_pending:
            batch["shutdown_at"] = None  # the countdown already elapsed
        also_running = []
        for o in _other_running_jobs():
            who = ("Scheduled auto-run" if o["scheduled"]
                   else own_by_id.get(o["channel_id"]) or "No channel")
            also_running.append({"who": who, "kind": o["kind"]})
        response = make_response(render_template(
            "studio.html", prods=prods, sources=sources,
            source_tags=source_tags,
            own_channels=active_channels,
            gated=gated, in_channel=in_channel,
            selected_channel=selected,
            selected_name=(own_by_id.get(selected) if selected
                           else ("No channel" if selected == 0 else None)),
            channel_counts=counts,
            unassigned_count=counts.get(0, 0),
            also_running=also_running,
            job=sjob._real(), msg=request.args.get("msg"),
            error=request.args.get("error"),
            batch_queue=queued,
            batch_running=(sjob.running and sjob.kind == "batch auto-run"),
            batch_shutdown_pending=shutdown_pending,
            finished_count=finished_count))
        if gated and selected is None and request.cookies.get(CHANNEL_COOKIE):
            response.delete_cookie(CHANNEL_COOKIE)
        return response

    @app.post("/studio/channel")
    def studio_select_channel():
        """Select the channel the studio works in (0 = productions with no
        channel)."""
        raw = (request.form.get("own_channel_id") or "").strip()
        try:
            cid = max(0, int(raw or 0))
        except ValueError:
            cid = 0
        if cid:
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            try:
                ok = db.get_own_channel(conn, cid) is not None
            finally:
                conn.close()
            if not ok:
                return redirect("/studio?error=Unknown+channel")
        return _remember_channel(redirect("/studio"), cid)

    @app.post("/studio/channel/leave")
    def studio_leave_channel():
        """Back to the channel picker."""
        response = redirect("/studio")
        response.delete_cookie(CHANNEL_COOKIE)
        return response

    @app.get("/finished")
    def finished_list():
        """Productions that passed review sit here, off the in-progress
        Studio list, sortable by channel or date so a growing backlog of
        approved-but-not-yet-published videos stays browsable. Covers both
        "ready" (approved, waiting to be published) and "published"."""
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        own_channels = db.list_own_channels(conn)
        own_by_id = {c["id"]: c["name"] for c in own_channels}

        sort = request.args.get("sort") or "date"
        if sort not in ("date", "channel", "title"):
            sort = "date"
        direction = request.args.get("dir") or "desc"
        if direction not in ("asc", "desc"):
            direction = "desc"
        status_filter = request.args.get("status") or "all"
        if status_filter not in ("all", "ready", "published"):
            status_filter = "all"

        # inside a selected channel the page shows that channel's videos only
        # (?all=1 shows every channel)
        scope = None if request.args.get("all") else _selected_channel()
        all_items, ready_count, published_count = [], 0, 0
        for p in db.list_productions(conn):
            if p["status"] not in ("ready", "published"):
                continue
            if scope is not None and (p["own_channel_id"] or 0) != scope:
                continue
            if p["status"] == "ready":
                ready_count += 1
            else:
                published_count += 1
            kit = packaging.load_kit(studio.prod_dir(cfg, p["id"]))
            tdata = thumbnails.load_thumbs(studio.prod_dir(cfg, p["id"]))
            all_items.append({
                "thumbs": ({"count": len(tdata["concepts"]),
                            "chosen": bool(tdata.get("chosen"))}
                           if tdata["concepts"] else None),
                "row": p,
                "own_channel": own_by_id.get(p["own_channel_id"]) or "",
                "kit": kit if kit.get("title") else None,
            })
        conn.close()

        if status_filter == "all":
            items = all_items
        else:
            items = [it for it in all_items if it["row"]["status"] == status_filter]

        key_fns = {
            "date": lambda it: it["row"]["updated_at"] or "",
            "channel": lambda it: (it["own_channel"] or "").lower(),
            "title": lambda it: (it["row"]["title"] or "").lower(),
        }
        items.sort(key=key_fns[sort], reverse=(direction == "desc"))

        def qs(**overrides) -> str:
            vals = {"sort": sort, "dir": direction, "status": status_filter}
            vals.update(overrides)
            return "&".join(f"{k}={quote(str(v))}" for k, v in vals.items()
                            if v and v != "all")

        return render_template(
            "finished.html", items=items, sort=sort, dir=direction, qs=qs,
            status_filter=status_filter, ready_count=ready_count,
            published_count=published_count,
            scope_name=(None if scope is None
                        else own_by_id.get(scope) or "No channel"),
            msg=request.args.get("msg"), error=request.args.get("error"))

    @app.post("/studio/<int:pid>/publish")
    def studio_publish(pid):
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
            if not prod:
                return redirect("/finished?error=" + quote("Unknown production"))
            if prod["status"] != "ready":
                return redirect("/finished?error="
                                + quote("Only a ready production can be published"))
            db.mark_production_published(conn, pid, published=True)
            db.add_step(conn, pid, "review", "manual", detail="published")
        finally:
            conn.close()
        return redirect("/finished?msg=" + quote("Marked published"))

    @app.post("/studio/<int:pid>/unpublish")
    def studio_unpublish(pid):
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
            if not prod:
                return redirect("/finished?error=" + quote("Unknown production"))
            if prod["status"] != "published":
                return redirect("/finished?error="
                                + quote("Only a published production can be un-published"))
            db.mark_production_published(conn, pid, published=False)
        finally:
            conn.close()
        return redirect("/finished?msg=" + quote("Moved back to ready"))

    @app.post("/studio/batch/queue")
    def studio_batch_queue():
        pid = _safe_int(request.form.get("pid"), 0, lo=1)
        with batch["lock"]:
            if pid and pid not in batch["queue"]:
                batch["queue"].append(pid)
        return redirect("/studio?msg=" + quote(f"Production {pid} queued"))

    @app.post("/studio/batch/unqueue")
    def studio_batch_unqueue():
        pid = _safe_int(request.form.get("pid"), 0)
        with batch["lock"]:
            batch["queue"] = [p for p in batch["queue"] if p != pid]
        return redirect("/studio?msg=" + quote("Removed from the batch"))

    @app.post("/studio/batch/clear")
    def studio_batch_clear():
        with batch["lock"]:
            batch["queue"].clear()
        return redirect("/studio?msg=" + quote("Batch queue cleared"))

    @app.post("/studio/batch/start")
    def studio_batch_start():
        if sjob.running:
            return redirect("/studio?error=" + quote("A job is already running"))
        with batch["lock"]:
            pids = list(batch["queue"])
            batch["queue"] = []
        if not pids:
            return redirect("/studio?error=" + quote("The batch queue is empty"))
        shutdown = request.form.get("shutdown") == "on"
        batch["shutdown_at"] = None

        def worker():
            _run_batch(pids, shutdown)

        if not sjob.start(worker, "batch auto-run"):
            with batch["lock"]:  # could not start: put the queue back
                batch["queue"] = pids + batch["queue"]
            return redirect("/studio?error=" + quote("A job is already running"))
        note = ", shutting down when done" if shutdown else ""
        return redirect("/studio?msg=" + quote(
            f"Batch started ({len(pids)} job(s)){note}"))

    @app.post("/studio/batch/stop")
    def studio_batch_stop():
        if not sjob.running or sjob.kind != "batch auto-run":
            return redirect("/studio?error=" + quote("No batch is running"))
        sjob.cancel = True
        return redirect("/studio?msg=" + quote(
            "Stopping the batch after the current stage"))

    @app.post("/studio/batch/cancel-shutdown")
    def studio_batch_cancel_shutdown():
        _batch_spawn(_batch_cancel_shutdown_cmd())
        batch["shutdown_at"] = None
        return redirect("/studio?msg=" + quote("Shutdown cancelled"))

    @app.post("/studio/pick-folder")
    def studio_pick_folder():
        """Open the server's native folder chooser and return the real path.

        The browser cannot give a page an absolute filesystem path, so the
        local server opens the OS dialog itself and hands the path back as JSON
        for the form's input. Cancelling returns {"path": null}."""
        initial = (request.form.get("initial") or "").strip()
        try:
            path = studio.pick_folder(initial or None)
        except RuntimeError as exc:
            return jsonify({"path": None, "error": str(exc)})
        return jsonify({"path": path})

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
            if source:
                taken = db.source_in_channel(
                    conn, source, int(own_channel) if own_channel else 0)
                if taken:
                    return redirect("/studio?error=" + quote(
                        f"That topic already has a production in this "
                        f"channel: #{taken['id']} {taken['title'][:60]} "
                        f"({db.SOURCE_USE_LABELS.get(taken['status'], taken['status'])})"))
            pid = db.create_production(conn, title, genre, source, work_dir)
            row = db.get_video(conn, source) if source else None
            seeded = {"source": "", "bible": False, "style": False, "refs": 0}
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
        # stage 1 / script stage show the WRITING style guide; the art style
        # (style.md) is managed on the channel and used by the shots stage
        style = studio.find_writing_style(pdir)
        style_text = _read_text(style)
        bible = studio.find_bible(pdir)
        bible_text = _read_text(bible)
        # which value the planner will actually use: the live channel value,
        # or this production's own file when it has overridden
        _c = db.connect(cfg.db_path)
        db.init_db(_c)
        try:
            visual_style_text, style_src, _bt, bible_src = autorun.style_bible(
                cfg, _c, prod, pdir)
        finally:
            _c.close()
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
        # source_tr above is a Path (used only for truthiness elsewhere, e.g.
        # gating the Generate Style button) - the "view transcript" panel
        # needs the actual text, same as script_text/style_text/etc below.
        source_tr_text = _read_text(source_tr)
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
        preview = studio.find_preview(pdir)
        review_video = studio.find_review_video(pdir)

        def _rel_url(path: Path | None) -> str | None:
            return (f"/studio/file/{pid}/{path.relative_to(pdir).as_posix()}"
                    if path else None)

        final_url = _rel_url(final)
        preview_url = _rel_url(preview)
        review_video_url = _rel_url(review_video)
        nle_projects = []
        for target, path in studio.find_nle_projects(pdir):
            nle_projects.append({
                "target": target,
                "label": studio.RENDER_TARGET_LABELS[target],
                "rel": path.relative_to(pdir).as_posix(),
                "url": _rel_url(path) if path.is_file() else None,
                "zip_url": (f"/studio/{pid}/capcut.zip"
                            if target == "capcut" else None),
            })

        providers = studio.providers(cfg)
        default_provider = prod["llm_provider"] or autorun._default_provider(cfg, pid)
        ready_names = {p["name"] for p in providers}
        # Per-stage picks that still exist and are ready; anything else falls
        # back to the Default LLM in provider_select().
        stage_providers = {
            s: (db.stage_provider(prod, s)
                if db.stage_provider(prod, s) in ready_names else None)
            for s in ("style", "script", "shots")
        }
        stage_labels = {
            s: studio.llm_label(cfg, stage_providers.get(s) or default_provider)
            for s in ("style", "script", "shots")
        }
        llm_ready = any(studio.provider_ready(cfg, p["name"]) for p in providers)
        llm_label = studio.llm_label(cfg, default_provider)
        renderly_ready = studio.renderly_ready(cfg.renderly_url)
        hooks = {
            "tts": bool(cfg.studio_tts_command),
            "imagegen": bool(cfg.studio_imagegen_command),
            "merge": bool(cfg.studio_merge_command) or
                     bool(cfg.imgtovideo_repo),
        }
        page = render_template(
            "studio_detail.html", prod=prod, steps=steps, history=history,
            stages=db.STAGES, stage=stage, script_text=script_text,
            style=style, style_text=style_text, source_tr=source_tr,
            style_src=style_src, bible_src=bible_src,
            visual_style_text=visual_style_text,
            source_tr_text=source_tr_text,
            shotlist_text=shotlist_text, audio=audio, srt=srt,
            coverage_gap=autorun.coverage_gap(pdir),
            has_research_notes=bool(_read_text(pdir / autorun.RESEARCH_NOTES_FILE).strip()),
            srt_text=srt_text, prompts_text=prompts_text, images=images,
            final=final, final_url=final_url,
            preview=preview, preview_url=preview_url,
            review_video=review_video, review_video_url=review_video_url,
            nle_projects=nle_projects,
            refs=studio.shotlist_refs(pdir),
            generate_references=eff["generate_references"],
            brief_profile=briefs.resolve_profile(
                eff.get("brief_motion"), eff.get("brief_min_hold"),
                eff.get("brief_max_hold"),
                default_max=eff["shotlist_max_hold_seconds"],
                custom=eff.get("brief_custom"),
                types=eff.get("brief_types"),
                reveal=eff.get("brief_reveal"),
                sfx=eff.get("brief_sfx")),
            brief_presentation=eff.get("brief_presentation") or "",
            render_target=eff["render_target"],
            render_target_label=studio.RENDER_TARGET_LABELS[eff["render_target"]],
            source_video=source_video, llm_ready=llm_ready,
            llm_label=llm_label, providers=providers,
            default_provider=default_provider, stage_providers=stage_providers,
            stage_labels=stage_labels, hooks=hooks,
            renderly_ready=renderly_ready, work_dir=str(pdir), job=sjob._real(),
            prod_voice=prod["voice"] or eff["voice"],
            voice_from=("this production" if prod["voice"]
                        else f"channel: {eff['own_channel_name']}"
                        if eff["own_channel_name"] and eff["voice"]
                        else ""), ai33_ready=bool(ai33.api_key(cfg)),
            flow_refs=[p.name for p in sorted((pdir / "refs").glob("*"))
                       if p.is_file()] if (pdir / "refs").exists() else [],
            flow_upscale_default=autorun._upscale_for(eff),
            local_upscale_tier=autorun.manual_upscale_tier(eff),
            native_download=autorun.is_flow_native(eff),
            default_render_mode=eff["render_mode"],
            default_engine=studio.effective_engine(eff["engine"],
                                                   eff["render_mode"]),
            own_channel_name=eff["own_channel_name"],
            flow_project_url_default=(eff["flow_project_url"]
                                      or cfg.flowbatch_project_url or ""),
            flowbatch_ready=studio.flowbatch_ready(cfg),
            own_channels=own_channels,
            script_versions=_version_names(pdir, "script"),
            stage_direction=db.stage_extra(prod, stage),
            bible_text=bible_text,
            batch_sheet=(pdir / "shotlist.json").exists(),
            direction_versions=_version_names(pdir, "direction", stage),
            shotlist_count=shotlist_count, cue_count=cue_count,
            msg=request.args.get("msg"), error=request.args.get("error"),
            channel_locked=bool(prod["own_channel_id"]),
        )
        # opening a production selects (and locks you into) its channel
        return _remember_channel(make_response(page),
                                 prod["own_channel_id"] or 0)

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
        # Any already-reached stage is a valid reset point - "review" is
        # excluded (nothing generated there to clear); the caller
        # (studio_detail.html's modal) only ever offers stages the
        # production has actually reached, but this is re-checked here too.
        if from_stage not in db.STAGES or from_stage == "review":
            return _studio_url(pid, error="Unknown start-over scope")
        with_audio = request.form.get("with_audio") == "1"
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            if not db.get_production(conn, pid):
                return redirect("/studio?error=Unknown+production")
            # "audio" is normally kept when resetting from an earlier stage
            # (a new style/script does not invalidate existing narration) -
            # unless the reset target IS "audio" itself (it's the thing being
            # redone) or the user explicitly opted in via with_audio.
            reset = [s for s in db.STAGES[db.STAGES.index(from_stage):]
                     if s != "audio" or from_stage == "audio" or with_audio]
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
                    if stage == "script":
                        # the script stage's own output lives in versions/script
                        # too: every attempt, the archived previous scripts and
                        # the judge's review. They fill the "Saved versions"
                        # list and (auto-*) make the next run look like a
                        # regenerate. Versions the user saved by name stay.
                        sdir = pdir / "versions" / "script"
                        if sdir.is_dir():
                            for f in list(sdir.glob("attempt-*.md")) + \
                                    list(sdir.glob("auto-*.md")) + \
                                    [sdir / "review.json"]:
                                if f.is_file():
                                    f.unlink()
                                    removed.append(f"versions/script/{f.name}")
                    if stage == "shots":
                        # the saved best-ever plan, its review and the brief it
                        # was planned with are generated output too: left behind,
                        # a fresh run is ranked against (and can be replaced by)
                        # the very plan the user just threw away
                        for name in ("best_ever.json", "review.json",
                                     "brief_used.md"):
                            f = pdir / "versions" / "shotlist" / name
                            if f.exists():
                                f.unlink()
                                removed.append(f"versions/shotlist/{name}")
                    if stage == "refs":
                        # only the refs WE generated (tracked in the manifest):
                        # supplied refs are inputs and must survive a reset
                        manifest = pdir / studio.REFS_GENERATED_MANIFEST
                        names = []
                        try:
                            names = json.loads(
                                manifest.read_text(encoding="utf-8"))
                        except (OSError, ValueError):
                            names = []
                        for name in names if isinstance(names, list) else []:
                            f = pdir / "refs" / f"{name}.png"
                            if f.is_file():
                                f.unlink()
                                removed.append(f"refs/{name}.png")
                        if manifest.exists():
                            manifest.unlink()
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
                if prod["source_video_id"]:
                    db.set_video(conn, prod["source_video_id"], produced=1)
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
            autorun._advance(cfg, pid, "script")
        finally:
            conn.close()
        return _studio_url(pid, msg="Script saved")

    @app.post("/studio/<int:pid>/style/generate")
    def studio_style_generate(pid):
        if sjob.running:
            return _studio_url(pid, error="A job is already running")
        provider = autorun._stage_provider(
            cfg, pid, "style",
            override=(request.form.get("provider") or "").strip() or None)
        _remember_stage_provider(cfg, pid, "style", provider)

        def worker():
            autorun.raise_result(autorun.run_stage_and_advance(
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
        (pdir / "writing_style.md").write_text(text + "\n", encoding="utf-8")
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            # the writing guide is per production; it does not touch the
            # channel's art style or the style_override flag
            db.add_step(conn, pid, "style", "manual")
            autorun._advance(cfg, pid, "style")
        finally:
            conn.close()
        return _studio_url(pid, msg="Writing style guide saved")

    @app.post("/studio/<int:pid>/script/generate")
    def studio_script_generate(pid):
        if sjob.running:
            return _studio_url(pid, error="A job is already running")

        provider = autorun._stage_provider(
            cfg, pid, "script",
            override=(request.form.get("provider") or "").strip() or None)
        _remember_stage_provider(cfg, pid, "script", provider)
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
            if not prod:
                return redirect("/studio?error=Unknown+production")
        finally:
            conn.close()

        def worker():
            autorun.raise_result(autorun.run_stage_and_advance(
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
        ?source=favorites lists the voices starred in the OpenSpeaker app
        plus the account's cloned voices."""
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
            autorun.raise_result(autorun.run_stage_and_advance(cfg, pid, "audio"))

        sjob.start(worker, "tts")
        return _studio_url(pid, msg="TTS started")

    @app.post("/studio/<int:pid>/srt/generate")
    def studio_srt_generate(pid):
        if sjob.running:
            return _studio_url(pid, error="A job is already running")

        def worker():
            autorun.raise_result(autorun.run_stage_and_advance(cfg, pid, "srt"))

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
        """Upload character/reference images used by the image engines."""
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
        raw = (request.form.get("name") or "").strip()
        rdir = studio.prod_dir(cfg, pid) / "refs"
        # the exact file stem first (seeded refs keep their own names, e.g.
        # CH_REIKO or "Reiko Ref"), then the slugified upload name
        stems = [s for s in (raw, _slugify(raw, 60))
                 if s and "/" not in s and "\\" not in s and ".." not in s]
        for name in stems:
            for f in sorted(rdir.glob(f"{glob.escape(name)}.*")) \
                    if rdir.exists() else []:
                if f.is_file() and f.suffix.lower() in (
                        ".png", ".jpg", ".jpeg", ".webp") \
                        and f.stem == name:
                    f.unlink()
                    return _studio_url(pid, msg="Reference image removed")
        return _studio_url(pid, error="Reference image not found")

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
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            db.update_production(conn, pid, bible_override=1)
        finally:
            conn.close()
        return _studio_url(pid, msg="Bible saved (this production now "
                                    "overrides the channel bible)")

    @app.post("/studio/<int:pid>/style-bible/use-channel")
    def studio_style_bible_use_channel(pid):
        """Drop a production's style/bible overrides so it follows the live
        channel value again."""
        which = (request.form.get("which") or "both").strip().lower()
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            if which in ("style", "both"):
                db.update_production(conn, pid, style_override=0)
            if which in ("bible", "both"):
                db.update_production(conn, pid, bible_override=0)
            conn.commit()
        finally:
            conn.close()
        return _studio_url(pid, msg="Now using the channel value")

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
                autorun._advance(cfg, pid, "script")
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

    def _drop_rejected_script(pid: int, pdir: Path) -> str:
        """Clearing the script versions must also clear what a regenerate
        would otherwise be measured against. The judge's review of the old
        attempts goes, and so does the current script when it is a REJECTED
        result (the run kept its best draft with a warning): left in place,
        the next regenerate rates it as the baseline to beat and asks the
        writer to differ from it - a script the user just threw away. An
        accepted or hand-written script is never touched."""
        (pdir / "versions" / "script" / "review.json").unlink(missing_ok=True)
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
            warning = (prod["warning"] or "") if prod else ""
            script = pdir / "script.md"
            if not script.exists() or not warning.startswith(
                    ("script gate failed", "regenerate did not beat")):
                return ""
            script.unlink()
            db.update_production(conn, pid, warning="")
            db.delete_steps(conn, pid, ["script"])
            conn.commit()
        finally:
            conn.close()
        return " and the rejected script"

    @app.post("/studio/<int:pid>/versions/clear")
    def studio_versions_clear(pid):
        """Delete every saved version of one kind (script, or one stage's
        direction) in a single click - the same files the list shows."""
        kind = request.form.get("kind") or ""
        stage = request.form.get("stage") or ""
        if kind not in ("script", "direction"):
            return _studio_url(pid, error="Unknown version type")
        if kind == "direction" and stage not in db.STAGES:
            return _studio_url(pid, error="Unknown stage")
        pdir = studio.prod_dir(cfg, pid)
        names = _version_names(pdir, kind, stage or None)
        for name in names:
            _version_path(pdir, kind, stage, name).unlink(missing_ok=True)
        dropped = ""
        if kind == "script":
            dropped = _drop_rejected_script(pid, pdir)
        if not names and not dropped:
            return _studio_url(pid, error="No saved versions to clear")
        return _studio_url(
            pid, msg=f"Cleared {len(names)} saved version(s){dropped}")

    @app.post("/studio/<int:pid>/shotlist/generate")
    def studio_shotlist_generate(pid):
        if sjob.running:
            return _studio_url(pid, error="A job is already running")
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
            if not prod:
                return redirect("/studio?error=Unknown+production")
            # seed from the channel first: a bible/style filled in AFTER the
            # production was created must still reach it (the old pre-check
            # errored before that seeding could ever run)
            studio.seed_production(cfg, conn, prod)
        finally:
            conn.close()
        # the manifest-authoring brief's bible gate: the LLM refuses to plan
        # without a reference bible, so require it up front
        if not studio.find_bible(studio.prod_dir(cfg, pid)):
            return _studio_url(
                pid,
                error="The planning brief requires a character/reference "
                      "bible - write or upload one below first")
        provider = autorun._stage_provider(
            cfg, pid, "shots",
            override=(request.form.get("provider") or "").strip() or None)
        _remember_stage_provider(cfg, pid, "shots", provider)

        def worker():
            autorun.raise_result(autorun.run_stage_and_advance(
                cfg, pid, "shots", {"provider": provider}))

        sjob.start(worker, "shotlist planning")
        return _studio_url(pid, msg="Shotlist planning started")

    @app.post("/studio/<int:pid>/external-prompt")
    def studio_external_prompt(pid):
        """Build a copy-paste prompt for running a stage in an external LLM
        (see external_prompts.py). Returns JSON; never calls an LLM."""
        kind = request.form.get("kind") or ""
        title = request.form.get("title") or ""
        mode = request.form.get("mode") or "auto"
        if mode not in ("auto", "inline", "files"):
            mode = "auto"
        if kind == "save_notes":
            try:
                n = external_prompts.save_notes(
                    cfg, pid, request.form.get("notes") or "")
            except external_prompts.PromptError as exc:
                return jsonify({"error": str(exc)}), 400
            return jsonify({"saved": n, "message":
                            f"Saved {n} words as the research notes"})

        local_note = [""]

        def build(files):
            """(text, local_faults) with `files` None (inline) or a list."""
            if kind == "style":
                return external_prompts.style_extraction_prompt(
                    cfg, pid, title), None
            if kind == "notes":
                return external_prompts.notes_extraction_prompt(
                    cfg, pid, title), None
            if kind in ("script_writer", "script_judge"):
                style = request.form.get("style")
                notes = request.form.get("notes")
                style = style if (style or "").strip() else None
                notes = notes if (notes or "").strip() else None
                try:
                    words = int(request.form.get("words") or 0) or None
                except ValueError:
                    words = None
                if kind == "script_writer":
                    return external_prompts.script_writer_prompt(
                        cfg, pid, title, words, style, notes, files), None
                script = request.form.get("script") or ""
                text = external_prompts.script_judge_prompt(
                    cfg, pid, script, title, style, notes, words, files)
                return text, None

            if kind == "shot_planner":
                return external_prompts.shotlist_planner_prompt(
                    cfg, pid, files), None
            if kind == "shot_judge":
                return external_prompts.shotlist_judge_prompt(
                    cfg, pid, request.form.get("shotlist") or "", files)
            raise KeyError(kind)

        try:
            files = [] if mode == "files" else None
            text, local_faults = build(files)
            if mode == "auto" and len(text) > external_prompts.AUTO_FILES_CHARS:
                files = []
                text, local_faults = build(files)
        except KeyError:
            return jsonify({"error": "Unknown prompt kind"}), 400
        except external_prompts.PromptError as exc:
            return jsonify({"error": str(exc)}), 400
        out = {"prompt": text, "chars": len(text),
               "files": [{"name": f["name"], "text": f["text"],
                          "about": f["about"]} for f in (files or [])]}
        if local_faults is not None:
            out["local_faults"] = local_faults
            out["local_note"] = local_note[0]
        return jsonify(out)

    @app.post("/studio/<int:pid>/webchat/<stage>")
    def studio_webchat_run(pid, stage):
        """Run the script or shotlist stage in web chats (webstages.py): a
        writer chat and a judge chat, the judge's feedback going back to the
        writer. Uses the external-LLM prompts; the API loop is not involved."""
        if stage not in ("script", "shots"):
            return _studio_url(pid, error="Unknown web-chat stage")
        if sjob.running:
            return _studio_url(pid, error="A job is already running")
        from . import webchat, webstages
        writer = (request.form.get("writer") or "zai").strip()
        judge = (request.form.get("judge") or "deepseek").strip()
        if writer not in webchat.SITES or judge not in webchat.SITES:
            return _studio_url(pid, error="Unknown chat site")
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            if not db.get_production(conn, pid):
                return redirect("/studio?error=Unknown+production")
        finally:
            conn.close()
        log = sjob.log.append
        run = (webstages.script_job if stage == "script"
               else webstages.shotlist_job)

        job = sjob._real()
        level = (request.form.get("zai_thinking") or "Low").strip()
        model = (request.form.get("zai_model") or "flash").strip()
        options = {
            "zai": {"thinking": level if level in ("Low", "High", "Max")
                    else "Low",
                    "model": model if model in ("flash", "5.3", "5.2")
                    else "flash"},
            "deepseek": {
                "deepthink": (request.form.get("deepseek_deepthink")
                              or "on") != "off",
                "search": (request.form.get("deepseek_search")
                           or "off") == "on"}}

        resume = (stage != "script"
                  and request.form.get("resume_plan") == "on")
        same_chats = request.form.get("same_chats") == "on"

        def worker():
            if stage == "script":
                run(cfg, pid, writer, judge, log, lambda: job.cancel,
                    options)
            else:
                run(cfg, pid, writer, judge, log, lambda: job.cancel,
                    options, resume, same_chats)

        label = ("script" if stage == "script" else "shotlist")
        sjob.start(worker, f"{label} in web chat ({writer} writes, "
                           f"{judge} judges)")
        return _studio_url(pid, msg=f"Web-chat {label} run started - a "
                                    f"browser window will open")

    @app.post("/studio/<int:pid>/webchat-stop")
    def studio_webchat_stop(pid):
        """Stop a web-chat run after its current round (the loop has no round
        limit: it runs until the judge passes the work, or you stop it)."""
        if not sjob.running or "web chat" not in str(sjob.kind or ""):
            return _studio_url(pid, error="No web-chat run is going")
        sjob.cancel = True
        return _studio_url(pid, msg="Stopping after the current round - the "
                                    "best result so far will be saved")

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
        # Saved, but not accepted as the finished stage while a narration cue
        # has no shot (a plan cut off at cue 460 of 521 looks complete).
        gap = autorun.coverage_gap(pdir)
        if gap:
            return _studio_url(pid, error="Shotlist saved but NOT accepted: "
                               + gap)
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            db.add_step(conn, pid, "shots", "manual",
                        detail=f"edited shotlist "
                               f"({len(data.get('images', []))} image(s))")
            autorun._advance(cfg, pid, "shots")
        finally:
            conn.close()
        return _studio_url(pid, msg="Shotlist saved")
    def _images_params(pid, form):
        """mode/engine/upscale/channel for the images stage, from a form (the
        Render images button) or the production's settings (form = {})."""
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            eff = settings.for_production(conn, db.get_production(conn, pid))
        finally:
            conn.close()
        mode = form.get("render_mode") or eff["render_mode"]
        if mode not in ("api", "flow"):
            mode = "api"
        engine = (form.get("engine") or eff["engine"] or "renderly")
        if engine not in ("renderly", "flowbatch"):
            engine = "renderly"
        engine = studio.effective_engine(engine, mode)
        # Flow native productions default to 0: the stills download at native
        # size and the Flow-native level runs as one local pass afterwards
        try:
            flow_upscale = max(0, min(4, int(form.get("flow_upscale")
                                             or autorun._upscale_for(eff))))
        except ValueError:
            flow_upscale = autorun._upscale_for(eff)
        flow_project_url = (form.get("flow_project_url") or "").strip()
        renderly_channel = None
        if engine == "renderly":
            renderly_channel = studio.resolve_renderly_channel(
                cfg, eff["own_channel"], create=True)
        return {"mode": mode, "engine": engine,
                "flow_upscale": flow_upscale,
                "flow_project_url": flow_project_url,
                "renderly_channel": renderly_channel}, engine

    @app.post("/studio/<int:pid>/images/render")
    def studio_images_render(pid):
        if sjob.running:
            return _studio_url(pid, error="A job is already running")

        pdir = studio.prepare_project_folder(cfg, pid)
        if not (pdir / "shotlist.json").exists():
            return _studio_url(pid, error="Generate the shotlist first")
        gap = autorun.coverage_gap(pdir)
        if gap:
            return _studio_url(pid, error="Not rendering: " + gap)
        base_params, engine = _images_params(pid, request.form)

        _job = sjob._real()   # this channel's slot, read from the worker thread

        def worker():
            autorun.raise_result(autorun.run_stage_and_advance(cfg, pid, "images", {
                **base_params,
                "log": sjob.log.append,
                "cancel": (lambda _j=_job: _j.cancel),
            }))

        label = ("FlowBatch" if engine == "flowbatch"
                 else "Renderly API + FlowBatch")
        sjob.start(worker, f"image rendering ({label})")
        return _studio_url(pid, msg=f"Image rendering started ({label})")

    _IMAGE_JOB_KINDS = ("image rendering", "image generation",
                        "gallery recovery", "local upscale", "auto-run")

    @app.post("/studio/<int:pid>/images/stop")
    def studio_images_stop(pid):
        """KILL the running image job from the images stage (render, gallery
        recovery, local upscale, or the auto-run that is on this stage).
        Destructive and final, one mechanism for every engine: the job's
        cancel flag is set FIRST (so the resume loop exits instead of
        pausing and trying again), then the process tree WhisperRadar spawned
        for this production is killed (FlowBatch + its Chrome, or the Renderly
        API ImageGen run). Nothing is retried and nothing is re-rendered;
        images already rendered are kept and a later Render images only
        fills the gaps. The reply says what was actually killed."""
        if not sjob.running or not str(sjob.kind or "").startswith(
                _IMAGE_JOB_KINDS):
            return _studio_url(pid, error="No image job is running")
        sjob.cancel = True
        report = studio.kill_image_batch(studio.prod_dir(cfg, pid))
        if report["nothing_running"]:
            return _studio_url(
                pid, msg="Cancelled - no image process was running (the run "
                         "was between steps or waiting), so nothing needed "
                         "killing. It will not retry.")
        return _studio_url(
            pid, msg="Killed: " + ", ".join(report["killed"])
                     + ". Nothing will retry; rendered images are kept.")

    @app.post("/studio/<int:pid>/images/recover")
    def studio_images_recover(pid):
        """Manual only: pull the images a stopped batch already generated in
        Flow out of the project gallery into images\\. Never automatic, never
        generates anything."""
        if sjob.running:
            return _studio_url(pid, error="A job is already running")
        pdir = studio.prepare_project_folder(cfg, pid)
        if not (pdir / "shotlist.json").exists():
            return _studio_url(pid, error="Generate the shotlist first")
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
            eff = settings.for_production(conn, prod)
        finally:
            conn.close()
        # same resolution the images stage uses (production choice ->
        # channel default -> global)
        mode = autorun._default_render_mode(cfg, prod)
        engine = studio.effective_engine(eff["engine"] or "renderly", mode)
        if engine == "renderly":
            return _studio_url(
                pid, error="The Renderly API has no gallery to recover from "
                           "- its results live in Renderly itself. Use the "
                           "FlowBatch engine (or render mode Flow) instead.")

        def worker():
            autorun.recover_images(cfg, pid, log=sjob.log.append,
                                   cancel=(lambda _j=sjob._real(): _j.cancel))

        label = "FlowBatch"
        sjob.start(worker, f"gallery recovery ({label})")
        return _studio_url(
            pid, msg=f"Gallery recovery started ({label}) - nothing will "
                     f"be generated; check the job log below")

    @app.post("/studio/<int:pid>/images/upscale")
    def studio_images_upscale(pid):
        """Manual: upscale the stills already in images\\ with the LOCAL
        engine - no Flow, no download, no re-render. This is the same pass
        a Flow native production runs at the end of a batch, and it is safe
        to re-run: files already at the tier are skipped."""
        if sjob.running:
            return _studio_url(pid, error="A job is already running")
        pdir = studio.prepare_project_folder(cfg, pid)
        img_dir = pdir / "images"
        if not img_dir.exists() or not any(img_dir.glob("*.png")):
            return _studio_url(pid, error="No rendered images to upscale yet")
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            eff = settings.for_production(conn, db.get_production(conn, pid))
        finally:
            conn.close()
        tier = autorun.manual_upscale_tier(eff)
        if tier == "off":
            return _studio_url(
                pid, error="No upscale level is set - pick one under Render "
                           "resolution: Flow native (My Channels or Settings), "
                           "or set an upscale tier first")

        def worker():
            autorun.upscale_images(cfg, pid, log=sjob.log.append,
                                   cancel=(lambda _j=sjob._real(): _j.cancel))

        sjob.start(worker, f"local upscale ({tier})")
        return _studio_url(
            pid, msg="Local upscale started - nothing is downloaded or "
                     "re-rendered; check the job log below")

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
            autorun._advance(cfg, pid, stage)
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
        gap = autorun._merge_pause_reason(cfg, pid)
        _job = sjob._real()

        def worker():
            # the merge itself renders any missing images first
            autorun.raise_result(autorun.run_stage_and_advance(
                cfg, pid, "merge", {
                    "mode": "cli", "log": sjob.log.append,
                    "cancel": (lambda _j=_job: _j.cancel)}))

        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            # the production's own channel may override the global target
            eff = settings.for_production(conn, db.get_production(conn, pid))
            target = eff["render_target"]
        finally:
            conn.close()
        label = studio.RENDER_TARGET_LABELS[target]
        if gap:
            sjob.start(worker, f"image rendering (missing images), then "
                               f"preview + {label} export")
            return _studio_url(
                pid, msg="Images are missing - rendering them first, then "
                         f"the preview + {label} export")
        sjob.start(worker, f"preview + {label} export (ImgToVideo)")
        return _studio_url(pid, msg=f"Preview build + {label} export started")

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
            autorun._advance(cfg, pid, "images")
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
            autorun._advance(cfg, pid, "merge")
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
            autorun.raise_result(autorun.run_stage_and_advance(
                cfg, pid, "merge", {"mode": "hook"}))

        sjob.start(worker, "merge")
        return _studio_url(pid, msg="Merge started")

    @app.post("/studio/<int:pid>/review/approve")
    def studio_review_approve(pid):
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
            db.add_step(conn, pid, "review", "manual", detail="approved")
            db.update_production(conn, pid, status="ready")
            if prod and prod["source_video_id"]:
                db.set_video(conn, prod["source_video_id"], produced=1)
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
        if rel == "batch_sheet.txt" and (pdir / "shotlist.json").is_file():
            # built from shotlist.json every time, so it never goes stale
            try:
                data = json.loads((pdir / "shotlist.json").read_text(
                    encoding="utf-8"))
            except ValueError:
                abort(404)
            resp = make_response(studio.batch_sheet_text(data))
            resp.headers["Content-Type"] = "text/plain; charset=utf-8"
            return resp
        if not target.is_file():
            abort(404)
        return send_file(target)

    @app.get("/studio/<int:pid>/capcut.zip")
    def studio_capcut_zip(pid):
        """Download the exported CapCut draft folder as a zip. CapCut wants
        the folder (not a file), so this is how it leaves the dashboard."""
        pdir = studio.prod_dir(cfg, pid).resolve()
        src = (pdir / "out" / "capcut" / pdir.name).resolve()
        try:
            src.relative_to(pdir)
        except ValueError:
            abort(404)
        if not src.is_dir():
            abort(404)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(src.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(src.parent))
        buf.seek(0)
        return send_file(buf, mimetype="application/zip", as_attachment=True,
                         download_name=f"{pdir.name}-capcut.zip")

    @app.get("/studio/produce/plan")
    def studio_produce_plan():
        """Dry-run: what the Auto Run producer would create right now. Makes
        no LLM calls and spends nothing."""
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            plan = producer.build_plan(cfg, conn,
                                       _selected_channel() or None)
        finally:
            conn.close()
        return {"plan": plan, "running": sjob.running}

    @app.post("/studio/produce")
    def studio_produce():
        """Create + run a production per runnable own channel (Auto Run)."""
        if sjob.running:
            return redirect("/studio?error=A+job+is+already+running")

        only = _selected_channel() or None   # read before the thread starts

        def worker():
            result = producer.run(cfg, log=sjob.log.append,
                                  job=sjob._real(), only_channel=only)
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
                "providers": [p["name"] for p in studio.providers(cfg)],
                "provider": autorun._default_provider(cfg, pid) or "",
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

        provider = (request.form.get("provider") or "").strip() or None

        def worker():
            result = autorun.run_pipeline(cfg, pid, job=sjob._real(),
                                          log=sjob.log.append,
                                          provider=provider)
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

    # ---- publish kit (packaging.py): title, description, chapters ----------

    def _chat_options(form):
        """The z.ai / DeepSeek settings a web-chat form posts."""
        level = (form.get("zai_thinking") or "Low").strip()
        model = (form.get("zai_model") or "flash").strip()
        return {
            "zai": {"thinking": level if level in ("Low", "High", "Max")
                    else "Low",
                    "model": model if model in ("flash", "5.3", "5.2")
                    else "flash"},
            "deepseek": {
                "deepthink": (form.get("deepseek_deepthink")
                              or "on") != "off",
                "search": (form.get("deepseek_search")
                           or "off") == "on"}}

    def _kit_page_data(pid):
        from . import packaging
        pdir = studio.prod_dir(cfg, pid)
        kit = packaging.load_kit(pdir)
        _cues, chapters = packaging.load_chapters(pdir)
        items = packaging.checks(kit, chapters)
        return pdir, kit, chapters, items

    @app.get("/studio/<int:pid>/kit")
    def studio_kit(pid):
        from . import packaging
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
        finally:
            conn.close()
        if not prod:
            return redirect("/finished?error=Unknown+production")
        pdir, kit, chapters, items = _kit_page_data(pid)
        names = list(kit.get("chapter_titles") or [])
        names += [""] * (len(chapters) - len(names))
        return render_template(
            "kit.html", prod=prod, kit=kit, chapters=chapters,
            chapter_names=names[:len(chapters)] if chapters else [],
            checks=items, score=packaging.kit_score(items),
            has_script=(pdir / "script.md").exists(),
            description_full=packaging.assemble_description(kit, chapters),
            fmt_ts=packaging.fmt_ts, job=sjob._real(),
            msg=request.args.get("msg"), error=request.args.get("error"))

    @app.post("/studio/<int:pid>/kit/generate")
    def studio_kit_generate(pid):
        from . import packaging, webchat
        if sjob.running:
            return redirect(f"/studio/{pid}/kit?error=A+job+is+already+running")
        writer = (request.form.get("writer") or "zai").strip()
        judge = (request.form.get("judge") or "deepseek").strip()
        if writer not in webchat.SITES or judge not in webchat.SITES:
            return redirect(f"/studio/{pid}/kit?error=Unknown+chat+site")
        if not (studio.prod_dir(cfg, pid) / "script.md").exists():
            return redirect(f"/studio/{pid}/kit?error=This+production+has+no+script+yet")
        options = _chat_options(request.form)
        log = sjob.log.append
        job = sjob._real()

        def worker():
            packaging.kit_job(cfg, pid, writer, judge, log,
                              lambda: job.cancel, options)

        sjob.start(worker, f"publish kit in web chat ({writer} writes, "
                           f"{judge} judges)")
        return redirect(f"/studio/{pid}/kit?msg=Publish+kit+run+started+-+a+"
                        f"browser+window+will+open")

    @app.post("/studio/<int:pid>/kit/save")
    def studio_kit_save(pid):
        """Keep the user's edits: title, description body, chapter titles,
        hashtags, tags, pinned comment."""
        from . import packaging
        pdir, kit, chapters, _items = _kit_page_data(pid)
        form = request.form
        kit["title"] = (form.get("title") or "").strip()
        kit["keyword"] = (form.get("keyword") or "").strip()
        kit["description"] = (form.get("description") or "").strip()
        kit["pinned_comment"] = (form.get("pinned_comment") or "").strip()
        kit["chapter_titles"] = [
            (form.get(f"chapter_{i}") or "").strip()
            for i in range(len(chapters))]
        parsed = packaging.parse_kit({
            "hashtags": form.get("hashtags") or "",
            "tags": form.get("tags") or ""})
        kit["hashtags"] = parsed["hashtags"]
        kit["tags"] = parsed["tags"]
        faults = packaging.local_faults(kit, chapters)
        kit["status"] = "ready" if (kit["title"] and not faults) else "draft"
        packaging.save_kit(pdir, kit)
        note = ("Saved - the kit is ready" if kit["status"] == "ready"
                else "Saved - still to fix: " + "; ".join(faults[:3]))
        return redirect(f"/studio/{pid}/kit?msg=" + quote(note))

    # ---- thumbnails (thumbnails.py) -------------------------------------------

    def _thumbs_back(pid, **kw):
        key, val = next(iter(kw.items()))
        return redirect(f"/studio/{pid}/thumbnails?{key}=" + quote(val))

    @app.get("/studio/<int:pid>/thumbnails")
    def studio_thumbnails(pid):
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
        finally:
            conn.close()
        if not prod:
            return redirect("/finished?error=Unknown+production")
        pdir = studio.prod_dir(cfg, pid)
        data = thumbnails.load_thumbs(pdir)
        stamp = int(time.time())
        for c in data["concepts"]:      # bust the browser cache after a redo
            c["art_url"] = (f"/studio/file/{pid}/{c['art_file']}?t={stamp}"
                            if c.get("art_file") else "")
            c["final_url"] = (f"/studio/file/{pid}/{c['final']}?t={stamp}"
                              if c.get("final") else "")
        sheet = thumbnails.thumbs_dir(pdir) / "sheet.jpg"
        return render_template(
            "thumbs.html", prod=prod, data=data, layouts=thumbnails.LAYOUTS,
            positions=thumbnails.POSITIONS, job=sjob._real(),
            has_script=(pdir / "script.md").exists(),
            sheet_url=(f"/studio/file/{pid}/thumbnails/sheet.jpg?t={stamp}"
                       if sheet.is_file() else ""),
            images=thumbnails.production_images(pdir),
            inspiration=[
                {"id": f[:-4], "url": f"/studio/file/{pid}/thumbnails/"
                 f"inspiration/{f}"}
                for f in thumbnails.inspiration_files(pdir)],
            msg=request.args.get("msg"), error=request.args.get("error"))

    @app.post("/studio/<int:pid>/thumbnails/inspiration")
    def studio_thumbnails_inspiration(pid):
        """Pull the thumbnails of the source video and the niche's best
        outliers, to look at while choosing the concept."""
        got = thumbnails.fetch_inspiration(cfg, pid)
        if not got:
            return _thumbs_back(pid, error="No thumbnails could be fetched "
                                           "- is there a source video?")
        return _thumbs_back(pid, msg=f"{len(got)} thumbnail(s) pulled")

    @app.post("/studio/<int:pid>/thumbnails/concepts")
    def studio_thumbnails_concepts(pid):
        from . import webchat
        if sjob.running:
            return _thumbs_back(pid, error="A job is already running")
        writer = (request.form.get("writer") or "zai").strip()
        judge = (request.form.get("judge") or "deepseek").strip()
        if writer not in webchat.SITES or judge not in webchat.SITES:
            return _thumbs_back(pid, error="Unknown chat site")
        if not (studio.prod_dir(cfg, pid) / "script.md").exists():
            return _thumbs_back(pid, error="This production has no script yet")
        options = _chat_options(request.form)
        log = sjob.log.append
        job = sjob._real()

        def worker():
            thumbnails.concepts_job(cfg, pid, writer, judge, log,
                                    lambda: job.cancel, options)

        sjob.start(worker, f"thumbnail concepts in web chat ({writer} "
                           f"writes, {judge} judges)")
        return _thumbs_back(pid, msg="Concept run started - a browser "
                                     "window will open")

    @app.post("/studio/<int:pid>/thumbnails/art")
    def studio_thumbnails_art(pid):
        if sjob.running:
            return _thumbs_back(pid, error="A job is already running")
        data = thumbnails.load_thumbs(studio.prod_dir(cfg, pid))
        if not data["concepts"]:
            return _thumbs_back(pid, error="Design the concepts first")
        ids = request.form.getlist("ids") or None
        log = sjob.log.append
        job = sjob._real()

        def worker():
            thumbnails.generate_art(cfg, pid, ids, log, lambda: job.cancel)

        sjob.start(worker, "thumbnail pictures")
        return _thumbs_back(pid, msg="Making the pictures with your image "
                                     "engine")

    @app.post("/studio/<int:pid>/thumbnails/save")
    def studio_thumbnails_save(pid):
        """Edits to the words, their place and colours, and the art prompt;
        re-composes every thumbnail."""
        pdir = studio.prod_dir(cfg, pid)
        data = thumbnails.load_thumbs(pdir)
        form = request.form
        for c in data["concepts"]:
            i = c["id"]
            if f"text_{i}" in form:
                c["text"] = thumbnails.clean_text(form.get(f"text_{i}"))
            pos = (form.get(f"pos_{i}") or "").strip()
            if pos in thumbnails.POSITIONS:
                c["text_pos"] = pos
            c["text_color"] = thumbnails._color(form.get(f"color_{i}"),
                                                c["text_color"])
            c["accent"] = thumbnails._color(form.get(f"accent_{i}"),
                                            c["accent"])
            prompt = (form.get(f"prompt_{i}") or "").strip()
            if prompt:
                c["art_prompt"] = prompt
        thumbnails.save_thumbs(pdir, data)
        thumbnails.compose_all(pdir)
        return _thumbs_back(pid, msg="Saved and re-composed")

    @app.post("/studio/<int:pid>/thumbnails/choose")
    def studio_thumbnails_choose(pid):
        pdir = studio.prod_dir(cfg, pid)
        data = thumbnails.load_thumbs(pdir)
        cid = (request.form.get("id") or "").strip()
        if cid not in [c["id"] for c in data["concepts"]]:
            return _thumbs_back(pid, error="Unknown thumbnail")
        data["chosen"] = None if data.get("chosen") == cid else cid
        thumbnails.save_thumbs(pdir, data)
        return _thumbs_back(pid, msg="Chosen - this is the one to upload"
                            if data["chosen"] else "Choice cleared")

    @app.post("/studio/<int:pid>/thumbnails/picture")
    def studio_thumbnails_picture(pid):
        """Use your own picture (upload) or one of the production's images
        as the art for a concept."""
        pdir = studio.prod_dir(cfg, pid)
        cid = (request.form.get("id") or "").strip()
        up = request.files.get("file")
        src = None
        tmp = None
        if up and up.filename:
            ext = Path(up.filename).suffix.lower()
            if ext not in (".png", ".jpg", ".jpeg"):
                return _thumbs_back(pid, error="Use a PNG or JPG")
            tmp = thumbnails.thumbs_dir(pdir) / ("upload" + ext)
            tmp.parent.mkdir(parents=True, exist_ok=True)
            up.save(tmp)
            src = tmp
        else:
            name = Path(request.form.get("image") or "").name
            if name and name in thumbnails.production_images(pdir):
                src = pdir / "images" / name
        ok = bool(src) and thumbnails.set_art(pdir, cid, src)
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        return (_thumbs_back(pid, msg="Picture set") if ok
                else _thumbs_back(pid, error="Could not use that picture"))

    # ---- packaging plan (plan.py): title + promise + hook before the script --

    def _plan_back(pid, **kw):
        key, val = next(iter(kw.items()))
        return redirect(f"/studio/{pid}/plan?{key}=" + quote(val))

    @app.get("/studio/<int:pid>/plan")
    def studio_plan(pid):
        from . import plan as packplan
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, pid)
        finally:
            conn.close()
        if not prod:
            return redirect("/studio?error=Unknown+production")
        plan = packplan.load_plan(studio.prod_dir(cfg, pid))
        return render_template(
            "plan.html", prod=prod, plan=plan, job=sjob._real(),
            faults=packplan.local_faults(plan) if plan["title"] else [],
            layouts=thumbnails.LAYOUTS,
            msg=request.args.get("msg"), error=request.args.get("error"))

    @app.post("/studio/<int:pid>/plan/generate")
    def studio_plan_generate(pid):
        from . import plan as packplan, webchat
        if sjob.running:
            return _plan_back(pid, error="A job is already running")
        writer = (request.form.get("writer") or "zai").strip()
        judge = (request.form.get("judge") or "deepseek").strip()
        if writer not in webchat.SITES or judge not in webchat.SITES:
            return _plan_back(pid, error="Unknown chat site")
        options = _chat_options(request.form)
        log = sjob.log.append
        job = sjob._real()

        def worker():
            packplan.plan_job(cfg, pid, writer, judge, log,
                              lambda: job.cancel, options)

        sjob.start(worker, f"packaging plan in web chat ({writer} writes, "
                           f"{judge} judges)")
        return _plan_back(pid, msg="Plan run started - a browser window "
                                   "will open")

    @app.post("/studio/<int:pid>/plan/save")
    def studio_plan_save(pid):
        """Keep the user's edits; "apply" also makes the title the
        production's title."""
        from . import plan as packplan
        pdir = studio.prod_dir(cfg, pid)
        plan = packplan.load_plan(pdir)
        form = request.form
        plan["title"] = (form.get("title") or "").strip()
        plan["keyword"] = (form.get("keyword") or "").strip()
        plan["promise"] = (form.get("promise") or "").strip()
        plan["hook"] = (form.get("hook") or "").strip()
        layout = (form.get("layout") or "").strip()
        plan["thumbnail"] = {
            "layout": layout if layout in packplan.LAYOUTS
            else plan["thumbnail"]["layout"],
            "text": thumbnails.clean_text(form.get("thumb_text")),
            "idea": (form.get("thumb_idea") or "").strip()}
        faults = packplan.local_faults(plan)
        plan["status"] = "ready" if not faults else "draft"
        packplan.save_plan(pdir, plan)
        if form.get("apply") and plan["title"]:
            packplan.apply_plan(cfg, pid)
            return _plan_back(pid, msg="Saved - the title is now this "
                                       "production's title, and the script "
                                       "will be written to this plan")
        return _plan_back(pid, msg="Saved" + (
            "" if not faults else " - still to fix: " + "; ".join(faults[:3])))

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
