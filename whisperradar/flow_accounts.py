"""Opt-in Flow identities. No live browser is opened by importing this module."""
from __future__ import annotations

import contextlib
import functools
import hashlib
import inspect
import json
import os
import sqlite3
from pathlib import Path
import threading
import time

from . import db, prodfiles, settings

LOCAL = threading.local()
OWNER_FILE = "flow_account.json"


def options(cfg):
    if not Path(cfg.db_path).exists():
        return {}
    conn = db.connect(cfg.db_path)
    try:
        try:
            return settings.load(conn)
        except sqlite3.OperationalError as exc:
            if "no such table: settings" in str(exc):
                return {}  # Before initial setup, multi-account remains off.
            raise
    finally:
        conn.close()


def enabled(cfg):
    return bool(options(cfg).get("flow_multi_account", False))


def valid(account):
    if account not in ("1", "2"):
        raise ValueError("Choose Flow Account 1 or Flow Account 2")
    return account


def root(cfg):
    return Path(cfg.db_path).resolve().parent / "flow_accounts"


@contextlib.contextmanager
def lease(cfg, account, cancel=None, log=None, wait=True):
    """OS-owned file lock: also excludes other server processes and login."""
    folder = root(cfg) / valid(account)
    with file_lease(folder / "worker.lock", cancel, log, wait, f"Flow Account {account}"):
        yield


@contextlib.contextmanager
def file_lease(path, cancel=None, log=None, wait=True, label="Flow production"):
    folder = path.parent
    folder.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        announced = False
        while True:
            if cancel and cancel():
                from .studio import BatchCancelled
                raise BatchCancelled("Cancelled while waiting for Flow account")
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if not wait:
                    raise RuntimeError(f"{label} is busy")
                if log and not announced:
                    log(f"Waiting for {label}; another job or login is using it")
                    announced = True
                time.sleep(.2)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)


def read_owner(pdir):
    path = Path(pdir) / OWNER_FILE
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    valid(data.get("account"))
    return data


def production_lease(cfg, pdir, **kwargs):
    key = hashlib.sha256(str(Path(pdir).resolve()).lower().encode()).hexdigest()
    return file_lease(root(cfg) / "productions" / (key + ".lock"), **kwargs)


def write_owner(pdir, account, bound):
    Path(pdir).mkdir(parents=True, exist_ok=True)
    path = Path(pdir) / OWNER_FILE
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"account": account, "bound": bound}), encoding="utf-8")
    temp.replace(path)


def legacy_project(pdir):
    for name in ("flow_prepare.json", "refs_project.json", "flowbatch.json"):
        path = prodfiles.path(pdir, name)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("projectUrl"):
                return True
    return False


def selection(cfg, pdir, pid=None):
    owner = read_owner(pdir)
    if owner:
        return owner["account"]
    if legacy_project(pdir):
        return "1"
    conn = db.connect(cfg.db_path)
    try:
        if pid is None and Path(pdir).parent.resolve() == Path(cfg.studio_dir).resolve() and Path(pdir).name.isdigit():
            pid = int(Path(pdir).name)
        prod = db.get_production(conn, pid) if pid else conn.execute(
            "SELECT * FROM productions WHERE work_dir = ?", (str(pdir),)).fetchone()
        # An existing project predating this feature belongs to the legacy profile.
        if prod is not None and prod["flow_project_url"]:
            return "1"
        if prod is not None and prod["own_channel_id"]:
            channel = db.get_own_channel(conn, prod["own_channel_id"])
            if channel is not None and channel["flow_account"]:
                return valid(channel["flow_account"])
    finally:
        conn.close()
    return valid(str(options(cfg).get("flow_default_account", "1")))


def profile(cfg, account):
    if valid(account) == "1":
        from . import chrome_profile, studio
        return chrome_profile.flowbatch_profile(studio.flowbatch_dir(cfg))
    return root(cfg) / account / "profile"


def require_supported_engine(cfg):
    cli = Path(cfg.flowbatch_repo or "") / "src" / "cli.js"
    if not cli.exists() or not all(flag in cli.read_text(encoding="utf-8")
                                   for flag in ("'runtime-dir'", "'upscale-tier'")):
        raise RuntimeError("Multiple Flow accounts require the updated bundled FlowBatch engine. Update the configured external checkout or use engines/flowbatch.")


def command_flags():
    active = getattr(LOCAL, "active", None)
    if not active or getattr(LOCAL, "legacy", False):
        return []
    return account_flags(*active)


def account_flags(cfg, account):
    flags = ["--profile", str(profile(cfg, account))]
    # Account 1 retains legacy state so turning this feature off is reversible.
    if account != "1":
        flags += ["--runtime-dir", str(root(cfg) / account / "runtime")]
    return flags


def isolated(fn):
    """Wrap the whole browser operation, including nested prepare/recovery."""
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def run(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        cfg = bound.arguments["cfg"]
        pdir = bound.arguments.get("pid_dir", bound.arguments.get("pdir"))
        active = getattr(LOCAL, "active", None)
        if active:
            return fn(*args, **kwargs)
        with production_lease(cfg, pdir, cancel=bound.arguments.get("cancel")):
            owner = read_owner(pdir)
            if not enabled(cfg):
                if owner and owner["account"] != "1":
                    raise RuntimeError("This project belongs to Flow Account 2. Enable multiple Flow accounts to resume it.")
                with lease(cfg, "1", bound.arguments.get("cancel"), bound.arguments.get("log")):
                    LOCAL.active = (cfg, "1")
                    LOCAL.legacy = True
                    try:
                        return fn(*args, **kwargs)
                    finally:
                        LOCAL.active = None
                        LOCAL.legacy = False
            account = selection(cfg, pdir, bound.arguments.get("pid"))
            require_supported_engine(cfg)
            with lease(cfg, account, bound.arguments.get("cancel"), bound.arguments.get("log")):
                # Recheck under the lease; selection/ownership must not race a launch.
                current = selection(cfg, pdir, bound.arguments.get("pid"))
                if current != account:
                    raise RuntimeError("Flow account changed while queued; start the job again")
                write_owner(pdir, account, True)
                LOCAL.active = (cfg, account)
                LOCAL.tier = "off"
                try:
                    return fn(*args, **kwargs)
                finally:
                    LOCAL.active = None
                    LOCAL.tier = "off"
    return run


def choose(cfg, pdir, pid, account):
    """Select before first use. Once bound, project ownership is immutable."""
    valid(account)
    if not enabled(cfg):
        raise ValueError("Enable multiple Flow accounts in image settings first")
    with production_lease(cfg, pdir, wait=False):
        _choose(cfg, pdir, pid, account)


def _choose(cfg, pdir, pid, account):
    old = read_owner(pdir)
    if old and old.get("bound") and old["account"] != account:
        raise ValueError("This production is already bound to another Flow account")
    conn = db.connect(cfg.db_path)
    try:
        prod = db.get_production(conn, pid)
        if (prod["flow_project_url"] or legacy_project(pdir)) and account != (old or {"account": "1"})["account"]:
            raise ValueError("An existing Flow project cannot be moved to another account")
    finally:
        conn.close()
    with lease(cfg, account, wait=False):
        write_owner(pdir, account, bool(old and old.get("bound")))
