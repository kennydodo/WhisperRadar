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
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("whisperradar")

READY_TIMEOUT = 120


def _up(url: str, timeout: int = 4) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 500
    except (urllib.error.URLError, OSError):
        return False


def _backend_url(cfg) -> str:
    return (cfg.renderly_url or "http://127.0.0.1:8022").rstrip("/") + "/api/channels"


def _driver_url(cfg) -> str:
    return (cfg.flow_driver_url or "http://127.0.0.1:8030").rstrip("/") + "/status"


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

    # ---------------------------------------------------------- probes --

    def status(self, cfg) -> dict:
        return {
            "renderly": _up(_backend_url(cfg)),
            "flow-driver": _up(_driver_url(cfg)),
            "managed": [n for n in self._started if self._alive(n)],
        }

    def _alive(self, name: str) -> bool:
        proc = self._started.get(name)
        return bool(proc and proc.poll() is None)

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
            try:
                if hasattr(subprocess, "CREATE_NO_WINDOW"):
                    subprocess.run(["taskkill", "/T", "/F", "/PID",
                                    str(proc.pid)], capture_output=True,
                                   timeout=30)
                else:
                    proc.terminate()
                stopped.append(name)
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("could not stop %s: %s", name, exc)
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
