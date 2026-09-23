"""Per-stage service management for the images stage.

The images stage is the only stage that needs external services (Renderly's
backend for imports/upscaling, and the Flow Driver for Flow automation). Once
a batch finishes those processes are dead weight - the Flow Driver keeps a
Chrome profile warm and Renderly holds its backend open - so Auto Run should
stop what it started.

Rules, deliberately conservative:
- A service that already answers is treated as YOURS: it is never tracked and
  never stopped, so manually started Renderly/Flow Driver windows are safe.
- Only processes this module spawned are stopped, and only when
  `services_managed` is on in Settings.
- Renderly started through its own start.bat (which opens console windows for
  the backend + frontend) is marked unmanaged, because killing it would close
  windows you may be using.
"""

import logging
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("whisperradar")

READY_TIMEOUT = 120


def _up(url: str, timeout: int = 4) -> bool:
    """True when something is listening and speaking HTTP at `url`.

    A 4xx/5xx still proves the service is up, and urllib RAISES HTTPError for
    those, so an error response must not be read as "down" - that mistake made
    the manager start a second Flow Driver, which died with EADDRINUSE and
    failed the images stage.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 500
    except urllib.error.HTTPError as exc:
        return 200 <= exc.code < 600
    except (urllib.error.URLError, OSError):
        return False


def _backend_url(cfg) -> str:
    return (cfg.renderly_url or "http://127.0.0.1:8022").rstrip("/") + "/api/channels"


def _driver_url(cfg) -> str:
    # the driver exposes /api/status (server.js), not /status
    return (cfg.flow_driver_url or "http://127.0.0.1:8030").rstrip("/") + "/api/status"


def _renderly_dir(cfg) -> Path | None:
    """The Renderly checkout, derived from the Flow Driver folder."""
    d = getattr(cfg, "flow_driver_dir", None)
    if not d:
        return None
    root = Path(d).expanduser().parent
    return root if root.exists() else None


def _driver_dir(cfg) -> Path | None:
    d = getattr(cfg, "flow_driver_dir", None)
    if not d:
        return None
    p = Path(d).expanduser()
    return p if p.exists() else None


class ServiceManager:
    def __init__(self):
        self._started: dict[str, subprocess.Popen] = {}
        self._probe_cache: dict = {"at": 0.0, "data": {}, "loading": False}

    # ---------------------------------------------------------- probes --

    def status(self, cfg) -> dict:
        """Live probes - only use where a small wait is acceptable."""
        return {
            "renderly": _up(_backend_url(cfg)),
            "flow-driver": _up(_driver_url(cfg)),
            "managed": [n for n in self._started if self._alive(n)],
        }

    def _refresh_probe_cache(self, cfg) -> None:
        try:
            data = {"renderly": _up(_backend_url(cfg)),
                    "flow-driver": _up(_driver_url(cfg))}
        except Exception as exc:  # noqa: BLE001
            log.warning("service probe failed: %s", exc)
            data = {}
        self._probe_cache.update({"at": time.monotonic(), "data": data,
                                  "loading": False})

    def _refresh_in_background(self, cfg) -> None:
        """Spawn the refresh without ever leaving `loading` stuck on."""
        try:
            threading.Thread(target=self._refresh_probe_cache, args=(cfg,),
                             daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not start the service probe: %s", exc)
            self._probe_cache["loading"] = False

    def status_cached(self, cfg, ttl: int = 15) -> dict:
        """Status for a page render: never blocks. Refreshes in the
        background when stale, and reports `checking` until it has data."""
        now = time.monotonic()
        data = self._probe_cache["data"]
        known = bool(data)
        if not known or now - self._probe_cache["at"] >= ttl:
            if not self._probe_cache["loading"]:
                self._probe_cache["loading"] = True
                self._refresh_in_background(cfg)
        managed = [n for n in self._started if self._alive(n)]
        return {"renderly": data.get("renderly"),
                "flow-driver": data.get("flow-driver"),
                "known": known, "managed": managed}

    def _alive(self, name: str) -> bool:
        proc = self._started.get(name)
        return bool(proc and proc.poll() is None)

    # ------------------------------------------------------- manual use --

    def start(self, cfg, name: str, log_fn=None) -> bool:
        """Explicit Start button: bring one service up now."""
        log_fn = log_fn or (lambda m: None)
        if name in self._started and self._alive(name):
            return True
        if name == "renderly":
            if _up(_backend_url(cfg)):
                log_fn("Renderly backend is already running (not managed)")
                return True
            return self._start_renderly(cfg, log_fn)
        if name == "flow-driver":
            if _up(_driver_url(cfg)):
                log_fn("Flow Driver is already running (not managed)")
                return True
            return self._start_driver(cfg, log_fn)
        raise ValueError(f"unknown service '{name}'")

    def stop(self, cfg, name: str, log_fn=None, force: bool = False) -> bool:
        """Stop a service. Only ever stops one WE started, unless force."""
        log_fn = log_fn or (lambda m: None)
        proc = self._started.get(name)
        if proc is None or proc.poll() is not None:
            if not force:
                log_fn(f"{name} was not started by WhisperRadar - left alone")
                return False
            return self._kill_by_name(cfg, name, log_fn)
        log_fn(f"stopping the {name} service")
        self._kill(proc)
        self._started.pop(name, None)
        return True

    def _kill(self, proc) -> None:
        try:
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               capture_output=True, timeout=30)
            else:
                proc.terminate()
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("could not stop pid %s: %s", proc.pid, exc)

    def _kill_by_name(self, cfg, name: str, log_fn) -> bool:
        """Force-stop a service we did not start (the pid is unknown, so this
        matches on the listening port)."""
        url = _backend_url(cfg) if name == "renderly" else _driver_url(cfg)
        port = url.rsplit(":", 1)[-1].split("/")[0]
        if not hasattr(subprocess, "CREATE_NO_WINDOW"):
            log_fn(f"force stop is Windows-only - use your own tool for {name}")
            return False
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-NetTCPConnection -LocalPort {port} -State Listen"
                 f" -ErrorAction SilentlyContinue).OwningProcess"],
                capture_output=True, text=True, timeout=30).stdout.split()
            for pid in {p.strip() for p in out if p.strip().isdigit()}:
                subprocess.run(["taskkill", "/T", "/F", "/PID", pid],
                               capture_output=True, timeout=30)
            log_fn(f"force-stopped whatever listened on :{port}")
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            log_fn(f"could not force-stop {name}: {exc}")
            return False

    # ---------------------------------------------------------- start ---

    def _spawn(self, name: str, cmd: list[str], cwd: Path,
               logfile: str | None = None) -> None:
        out = subprocess.DEVNULL
        if logfile:
            out = open(cwd / logfile, "a", encoding="utf-8")  # noqa: SIM115
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if logfile else \
            getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        self._started[name] = subprocess.Popen(
            cmd, cwd=str(cwd), stdout=out, stderr=out, creationflags=flags)

    def ensure(self, cfg, names: list[str], log_fn=None) -> list[str]:
        """Start the named services that are not already answering.
        Returns the names actually started (so callers can report)."""
        log_fn = log_fn or (lambda m: None)
        started = []
        for name in names:
            if name in self._started and self._alive(name):
                continue
            if name == "renderly":
                if _up(_backend_url(cfg)):
                    continue
                if self._start_renderly(cfg, log_fn):
                    started.append(name)
            elif name == "flow-driver":
                if _up(_driver_url(cfg)):
                    continue
                if self._start_driver(cfg, log_fn):
                    started.append(name)
        return started

    def _start_renderly(self, cfg, log_fn) -> bool:
        root = _renderly_dir(cfg)
        if root is None:
            log_fn("Renderly folder not found - images will keep Flow's "
                   "native size (no import/upscale)")
            return False
        backend = root / "backend"
        venv_python = backend / ".venv" / "Scripts" / "python.exe"
        port = (cfg.renderly_url or "http://127.0.0.1:8022").rsplit(":", 1)[-1]
        if venv_python.exists():
            log_fn("starting the Renderly backend (managed)...")
            self._spawn("renderly",
                        [str(venv_python), "-m", "uvicorn", "main:app",
                         "--port", port],
                        backend, logfile="whisperradar-backend.log")
        else:
            start_bat = root / "start.bat"
            if not start_bat.exists():
                log_fn("Renderly backend is not running and cannot be started")
                return False
            log_fn("starting Renderly via start.bat (NOT managed - its "
                   "windows stay open)...")
            subprocess.Popen(["cmd", "/c", str(start_bat)], cwd=str(root),
                             creationflags=getattr(subprocess,
                                                   "CREATE_NEW_CONSOLE", 0))
            for _ in range(READY_TIMEOUT):
                if _up(_backend_url(cfg)):
                    break
                time.sleep(1)
            return False  # unmanaged: never stopped by release()
        for _ in range(READY_TIMEOUT):
            if _up(_backend_url(cfg)):
                log_fn("Renderly backend is up")
                return True
            time.sleep(1)
        log_fn("Renderly backend did not come up in time")
        return False

    def _start_driver(self, cfg, log_fn) -> bool:
        d = _driver_dir(cfg)
        if d is None or not (d / "server.js").exists():
            raise RuntimeError(
                "Flow Driver service is not running and studio.flow_driver_dir "
                "is not configured")
        if not (d / "node_modules" / "playwright").exists():
            raise RuntimeError(
                f"Playwright not installed - run: cd {d} && npm install")
        log_fn("starting the Flow Driver service (managed)...")
        self._spawn("flow-driver", ["node", "server.js"], d,
                    logfile="driver-service.log")
        for _ in range(40):
            if _up(_driver_url(cfg)):
                log_fn("Flow Driver is up")
                return True
            time.sleep(0.5)
        raise RuntimeError("Flow Driver service did not come up on "
                           + (cfg.flow_driver_url or "http://127.0.0.1:8030"))

    # ---------------------------------------------------------- stop ----

    def release(self, cfg, managed: bool, log_fn=None) -> list[str]:
        """Stop the services WE started, when the user opted in."""
        log_fn = log_fn or (lambda m: None)
        if not managed:
            return []
        stopped = []
        for name, proc in list(self._started.items()):
            if proc.poll() is not None:
                self._started.pop(name, None)
                continue
            if name == "renderly":
                # never kill a backend the user is likely using
                log_fn("leaving the Renderly backend running (shared service)")
                self._started.pop(name, None)
                continue
            log_fn(f"stopping the {name} service (started by WhisperRadar)")
            self._kill(proc)
            stopped.append(name)
            self._started.pop(name, None)
        return stopped


MANAGER = ServiceManager()


def services_for(engine: str, mode: str) -> list[str]:
    """Which external services a stage needs for this engine/mode."""
    if engine == "flowimagesgen":
        return []          # a CLI: it starts and stops its own browser
    if mode == "flow":
        return ["renderly", "flow-driver"]
    return ["renderly"]    # Renderly API needs the backend for Gemini
