"""Per-stage service management for the images stage.

The only external service the images stage needs is the Renderly API (it now
lives in engines\\renderly-api: PL/PR wide shots, imports and upscaling).
FlowBatch is a CLI that starts and stops its own browser, so it is not a
service. Once a batch finishes the API process is dead weight, so Auto Run
should stop what it started.

Rules, deliberately conservative:
- A service that already answers is treated as YOURS: it is never tracked and
  never stopped, so a manually started API window is safe.
- Only processes this module spawned are stopped, and only when
  `services_managed` is on in Settings.
"""

import importlib.util
import logging
import subprocess
import sys
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
    the manager start a second API process, which died with EADDRINUSE and
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


def _renderly_dir(cfg) -> Path | None:
    """The Renderly web API folder (engines\\renderly-api in this repo)."""
    d = getattr(cfg, "renderly_api_dir", None)
    if not d:
        return None
    root = Path(d).expanduser()
    return root if (root / "main.py").exists() else None


class ServiceManager:
    def __init__(self):
        self._started: dict[str, subprocess.Popen] = {}
        self._probe_cache: dict = {"at": 0.0, "data": {}, "loading": False}

    # ---------------------------------------------------------- probes --

    def status(self, cfg) -> dict:
        """Live probes - only use where a small wait is acceptable."""
        return {
            "renderly": _up(_backend_url(cfg)),
            "managed": [n for n in self._started if self._alive(n)],
        }

    def _refresh_probe_cache(self, cfg) -> None:
        try:
            data = {"renderly": _up(_backend_url(cfg))}
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
        url = _backend_url(cfg)
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
        return started

    def _start_renderly(self, cfg, log_fn) -> bool:
        root = _renderly_dir(cfg)
        if root is None:
            log_fn("Renderly folder not found - images will keep Flow's "
                   "native size (no import/upscale)")
            return False
        port = (cfg.renderly_url or "http://127.0.0.1:8022").rsplit(":", 1)[-1]
        # its own .venv when one exists, otherwise the Python running
        # WhisperRadar (the API's packages are in requirements.txt)
        own = root / ".venv" / "Scripts" / "python.exe"
        python = str(own) if own.exists() else sys.executable
        if not own.exists() and importlib.util.find_spec("fastapi") is None:
            log_fn("The Renderly API needs its packages - run setup.cmd "
                   "(pip install -r requirements.txt) once")
            return False
        log_fn("starting the Renderly backend (managed)...")
        self._spawn("renderly",
                    [python, "-m", "uvicorn", "main:app", "--port", port],
                    root, logfile="whisperradar-backend.log")
        for _ in range(READY_TIMEOUT):
            if _up(_backend_url(cfg)):
                log_fn("Renderly backend is up")
                return True
            time.sleep(1)
        log_fn("Renderly backend did not come up in time")
        return False

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
    if engine == "flowbatch":
        return []          # a CLI: it starts and stops its own browser
    if mode == "flow":
        return []          # all shots on FlowBatch: no API involved
    return ["renderly"]    # PL/PR go through the API (Gemini)
