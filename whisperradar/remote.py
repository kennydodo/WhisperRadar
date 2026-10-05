"""Phone / remote access: a password gate for every request that is not local.

The dashboard has no accounts, so it is only safe to reach from another device
if something stands in front of it. This module is that something:

* a request from this PC itself (loopback, and not relayed by a proxy) works
  exactly as before - no password;
* any other request needs the password in the WR_PASSWORD environment variable
  (a login page, then a signed session cookie). With no password set, remote
  requests are refused outright, so binding the server to a network address by
  mistake still exposes nothing;
* a request that arrives through a local proxy (`tailscale serve`, a reverse
  proxy) looks like loopback but carries forwarding headers - it is treated as
  remote, so a proxy cannot be used to skip the password.

`python wr.py serve --remote` additionally listens on this PC's Tailscale
address (see tailscale_addresses) and never starts the debugger.
"""
from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import shutil
import subprocess
import threading
import time
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote

from flask import redirect, request, session

PASSWORD_ENV = "WR_PASSWORD"
# headers a local proxy adds; their presence means "not really local"
PROXY_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP",
                 "Forwarded", "Tailscale-User-Login")
SESSION_KEY = "wr_auth"
SESSION_DAYS = 30
# failed logins per client address before it is slowed down
MAX_FAILURES = 5
FAILURE_WINDOW = 600      # seconds the failures are remembered
LOCKOUT = 60              # seconds a client is refused after too many

_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")

_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()


def password() -> str:
    return os.environ.get(PASSWORD_ENV, "")


def is_loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def is_remote_request() -> bool:
    """True unless the request came straight from this PC."""
    try:
        local = ipaddress.ip_address(request.remote_addr or "").is_loopback
    except ValueError:
        local = False
    if not local:
        return True
    return any(request.headers.get(h) for h in PROXY_HEADERS)


def _token(secret: str, pw: str) -> str:
    """What a logged-in session stores: changes when the password changes."""
    return hmac.new(secret.encode(), pw.encode(), sha256).hexdigest()


def _secret(cfg) -> str:
    """A random signing key kept next to the database (created once)."""
    path = Path(cfg.db_path).parent / ".web_secret"
    try:
        value = path.read_text(encoding="utf-8").strip()
        if len(value) >= 32:
            return value
    except OSError:
        pass
    value = secrets.token_hex(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    except OSError:
        pass            # unwritable: sessions last until the next restart
    return value


def _client() -> str:
    return request.remote_addr or "?"


def _locked(now: float) -> int:
    """Seconds left before this client may try again (0 = may try)."""
    with _failures_lock:
        recent = [t for t in _failures.get(_client(), [])
                  if now - t < FAILURE_WINDOW]
        _failures[_client()] = recent
        if len(recent) >= MAX_FAILURES:
            return max(0, int(LOCKOUT - (now - recent[-1])))
    return 0


def _record_failure(now: float) -> None:
    with _failures_lock:
        _failures.setdefault(_client(), []).append(now)


def _safe_next(value: str | None) -> str:
    """Only ever redirect to a path on this site."""
    if value and value.startswith("/") and not value.startswith("//") \
            and "\\" not in value:
        return value
    return "/"


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WhisperRadar - sign in</title>
<style>
  body { margin:0; min-height:100vh; display:flex; align-items:center;
         justify-content:center; background:#0f1115; color:#e6e9ef;
         font:16px/1.5 "Segoe UI", system-ui, sans-serif; }
  form { background:#171a21; border:1px solid #262b36; border-radius:12px;
         padding:22px 20px; width:min(340px, 92vw); }
  h1 { font-size:18px; margin:0 0 14px; }
  input, button { width:100%; box-sizing:border-box; font-size:16px;
                  border-radius:8px; padding:11px 12px; }
  input { background:#0c0e12; color:#e6e9ef; border:1px solid #262b36; }
  button { margin-top:12px; background:#4f8cff; color:#fff; border:0;
           font-weight:600; }
  .err { color:#e05c5c; margin:0 0 10px; font-size:14px; }
</style></head><body>
<form method="post" action="/login">
  <h1>WhisperRadar</h1>
  {error}
  <input type="hidden" name="next" value="{next}">
  <input type="password" name="password" placeholder="Password" autofocus
         autocomplete="current-password" required>
  <button type="submit">Sign in</button>
</form></body></html>"""


def _login_page(next_url: str, error: str = "", status: int = 200):
    from html import escape

    body = LOGIN_PAGE.replace(
        "{error}", f'<p class="err">{escape(error)}</p>' if error else ""
    ).replace("{next}", escape(next_url, quote=True))
    return body, status, {"Content-Type": "text/html; charset=utf-8"}


def install(app, cfg) -> None:
    """Add the gate and the /login, /logout routes to `app`."""
    app.secret_key = _secret(cfg)
    app.config.update(SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_NAME="wr_session")

    def authed() -> bool:
        pw = password()
        return bool(pw) and hmac.compare_digest(
            str(session.get(SESSION_KEY, "")),
            _token(app.secret_key, pw))

    @app.before_request
    def _remote_gate():
        if not is_remote_request():
            return None
        if request.path.startswith("/static/"):
            return None                      # stylesheets / scripts only
        if not password():
            return ("Remote access is off. Set the WR_PASSWORD environment "
                    "variable on the PC running WhisperRadar and restart it.",
                    403)
        if authed() or request.path == "/login":
            return None
        if request.method == "GET":
            return redirect("/login?next=" + quote(request.full_path.rstrip("?")))
        return ("Sign in first", 401)

    @app.route("/login", methods=["GET", "POST"])
    def _login():
        next_url = _safe_next(request.values.get("next"))
        if not is_remote_request():
            return redirect(next_url)        # local: nothing to sign in to
        if request.method == "GET":
            return _login_page(next_url)
        wait = _locked(time.time())
        if wait:
            return _login_page(next_url, f"Too many attempts - wait {wait}s.", 429)
        pw = password()
        given = request.form.get("password", "")
        if pw and hmac.compare_digest(given.encode(), pw.encode()):
            session.clear()
            session[SESSION_KEY] = _token(app.secret_key, pw)
            session.permanent = True
            app.permanent_session_lifetime = SESSION_DAYS * 86400
            return redirect(next_url)
        _record_failure(time.time())
        return _login_page(next_url, "Wrong password.", 401)

    @app.post("/logout")
    def _logout():
        session.clear()
        return redirect("/login")


def tailscale_addresses() -> list[str]:
    """This PC's Tailscale IPv4 address(es): from the tailscale CLI, which is
    installed with the Windows/macOS/Linux app (not always on PATH)."""
    candidates = [shutil.which("tailscale"),
                  r"C:\Program Files\Tailscale\tailscale.exe",
                  r"C:\Program Files (x86)\Tailscale\tailscale.exe",
                  "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]
    for exe in candidates:
        if not exe:
            continue
        try:
            out = subprocess.run([exe, "ip", "-4"], capture_output=True,
                                 text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        found = []
        for line in out.stdout.split():
            try:
                ip = ipaddress.ip_address(line.strip())
            except ValueError:
                continue
            if ip.version == 4 and ip in _TAILSCALE_NET:
                found.append(str(ip))
        if found:
            return found
    return _adapter_addresses()


def _adapter_addresses() -> list[str]:
    """Fallback when the tailscale command is not found: a running Tailscale
    shows up as a network adapter holding an address in 100.64.0.0/10, which
    `ipconfig` (Windows) or `ip`/`ifconfig` (Linux, macOS) lists."""
    import re

    for cmd in (["ipconfig"], ["ip", "-4", "addr"], ["ifconfig"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        found = []
        for raw in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", out):
            try:
                ip = ipaddress.ip_address(raw)
            except ValueError:
                continue
            if ip in _TAILSCALE_NET and str(ip) not in found:
                found.append(str(ip))
        if found:
            return found
    return []
