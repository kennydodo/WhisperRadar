"""Turn Chrome's Efficiency mode (Memory Saver + Energy Saver) off in the
automation profiles the image engines drive.

Both engines open their own Chrome from a dedicated profile folder (the Flow
Driver's `profileDir` in extension-v2\\driver-config.json, default `profile`;
FlowBatch's `paths.profileDir` in config\\settings.json, default `profile`).
Chrome keeps these two switches in `Local State` at the root of that folder,
so editing that one file BEFORE the engine launches Chrome is enough: no
process has to be found, and the user's everyday Chrome (a different folder)
is never touched.

The edit is skipped while the profile is open (Chrome would overwrite the file
on exit anyway); it is applied again before the next launch.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# (path of keys inside Local State, value) - the multi-state prefs of recent
# Chrome versions (0 = off) plus the older boolean for Memory Saver
_OFF = (
    (("performance_tuning", "high_efficiency_mode", "state"), 0),
    (("performance_tuning", "high_efficiency_mode", "enabled"), False),
    (("performance_tuning", "battery_saver_mode", "state"), 0),
)


def _profile_in_use(profile: Path) -> bool:
    """True while a Chrome has this profile open."""
    if (profile / "SingletonLock").exists() or \
            os.path.lexists(profile / "SingletonLock"):
        return True
    lock = profile / "lockfile"      # Windows: held open by the browser
    if lock.exists():
        try:
            with open(lock, "ab"):
                pass
        except OSError:
            return True
    return False


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def turn_off_efficiency_mode(profile_dir) -> str:
    """Set Memory Saver and Energy Saver off in `profile_dir`'s Local State.

    Returns what happened: "changed", "already off", "in use" (Chrome has the
    profile open, nothing written), "no profile" (the folder does not exist
    yet) or "error: ...". Never raises."""
    try:
        profile = Path(profile_dir)
        if not profile.is_dir():
            return "no profile"
        if _profile_in_use(profile):
            return "in use"
        path = profile / "Local State"
        state = _read_json(path) if path.exists() else {}
        if state is None:
            return "error: Local State is not valid JSON"
        changed = False
        for keys, value in _OFF:
            node = state
            for key in keys[:-1]:
                nxt = node.get(key)
                if not isinstance(nxt, dict):
                    nxt = node[key] = {}
                node = nxt
            if node.get(keys[-1]) != value or keys[-1] not in node:
                node[keys[-1]] = value
                changed = True
        if not changed:
            return "already off"
        fd, tmp = tempfile.mkstemp(dir=str(profile), prefix="Local State.",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, separators=(",", ":"))
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return "changed"
    except Exception as exc:  # noqa: BLE001 - never block a render
        return f"error: {exc}"


def _resolve(base: Path, raw, default: str) -> Path:
    raw = str(raw or "").strip() or default
    p = Path(raw).expanduser()
    return p if p.is_absolute() else base / p


def flow_driver_profile(driver_dir) -> Path:
    """The Flow Driver's Chrome profile: driver-config.json `profileDir`
    (relative to the extension-v2 folder), else `profile`."""
    base = Path(driver_dir)
    cfg = _read_json(base / "driver-config.json") or {}
    return _resolve(base, cfg.get("profileDir"), "profile")


def flowbatch_profile(repo) -> Path:
    """FlowBatch's Chrome profile: `paths.profileDir` of config\\settings.json
    (settings.local.json wins), relative to the FlowBatch folder."""
    base = Path(repo)
    raw = None
    for name in ("settings.json", "settings.local.json"):
        paths = (_read_json(base / "config" / name) or {}).get("paths")
        if isinstance(paths, dict) and paths.get("profileDir"):
            raw = paths["profileDir"]
    return _resolve(base, raw, "profile")


def enabled(cfg) -> bool:
    """The `chrome_efficiency_off` setting (default on)."""
    from . import db, settings

    try:
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            return bool(settings.load(conn).get("chrome_efficiency_off", True))
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.debug("could not read chrome_efficiency_off: %s", exc)
        return True


def apply(cfg, engine: str, say=None) -> str | None:
    """Before an engine launches Chrome: turn Efficiency mode off in that
    engine's profile when the setting is on. `engine` is "flow_driver" or
    "flowbatch". Returns the outcome (None = setting off / engine not set
    up). Says something only when it changed the profile or could not."""
    from . import studio

    if os.environ.get("WHISPERRADAR_NO_CHROME_PREFS"):
        return None      # the test suite must never edit a real profile
    if not enabled(cfg):
        return None
    if engine == "flowbatch":
        repo = studio.flowbatch_dir(cfg)
        profile = flowbatch_profile(repo) if repo else None
    else:
        driver = studio.flow_driver_dir(cfg)
        profile = flow_driver_profile(driver) if driver else None
    if profile is None:
        return None
    result = turn_off_efficiency_mode(profile)
    say = say or (lambda m: log.info("%s", m))
    if result == "changed":
        say(f"Chrome Efficiency mode turned off in the automation profile "
            f"({profile})")
    elif result.startswith("error"):
        say(f"could not turn Chrome Efficiency mode off in {profile}: "
            f"{result[7:]} - continuing")
    return result
