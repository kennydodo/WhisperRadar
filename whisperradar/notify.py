"""Best-effort notifications for unattended Auto Run.

An unattended run that pauses at 3am is useless if nobody hears about it, so
the producer reports problems here. Two optional targets, both configured in
Settings:

- a Windows desktop balloon, via PowerShell's NotifyIcon (no dependencies)
- an HTTP webhook: Discord, Slack, ntfy, or a generic JSON POST

Nothing here may ever raise: a failed notification must not fail a production.
"""

import json
import logging
import os
import subprocess
import urllib.error
import urllib.request

log = logging.getLogger("whisperradar")

KINDS = ("discord", "slack", "ntfy", "json")


def config_for(conn) -> dict:
    """The notify_* settings, read fresh (they are user-editable at runtime)."""
    from . import db, settings

    vals = settings.load(conn)
    return {
        "desktop": bool(vals.get("notify_desktop")),
        "url": (vals.get("notify_webhook_url") or "").strip(),
        "kind": (vals.get("notify_webhook_kind") or "discord").strip().lower(),
        "on_success": bool(vals.get("notify_on_success")),
    }


def enabled(conn) -> bool:
    conf = config_for(conn)
    return bool(conf["desktop"] or conf["url"])


def build_body(kind: str, title: str, message: str) -> tuple[bytes, dict]:
    """(body, headers) for the webhook kind. ntfy takes the raw message and
    a Title header; the others take JSON."""
    text = f"{title}\n{message}" if title else message
    if kind == "ntfy":
        return message.encode("utf-8"), {
            "Content-Type": "text/plain; charset=utf-8",
            "Title": title.encode("ascii", "replace").decode("ascii"),
        }
    if kind == "slack":
        payload = {"text": f"*{title}*\n{message}"}
    elif kind == "discord":
        payload = {"content": f"**{title}**\n{message}"}
    else:
        payload = {"title": title, "message": message, "text": text}
    return (json.dumps(payload).encode("utf-8"),
            {"Content-Type": "application/json"})


def send_webhook(url: str, kind: str, title: str, message: str,
                 timeout: int = 15) -> bool:
    try:
        body, headers = build_body(kind if kind in KINDS else "json",
                                   title, message)
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("notification webhook failed: %s", exc)
        return False


def desktop_command(title: str, message: str) -> list[str]:
    """A PowerShell one-liner showing a balloon tip - no modules needed."""
    safe = lambda s: s.replace("'", "''")  # noqa: E731 - PowerShell escaping
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        "$n.Visible = $true;"
        f"$n.ShowBalloonTip(10000, '{safe(title)}', '{safe(message)}',"
        "[System.Windows.Forms.ToolTipIcon]::Info);"
        "Start-Sleep -Seconds 8;"
        "$n.Dispose()"
    )
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]


def send_desktop(title: str, message: str) -> bool:
    if os.name != "nt":
        return False
    try:
        subprocess.Popen(desktop_command(title, message),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess,
                                                "CREATE_NO_WINDOW", 0))
        return True
    except OSError as exc:
        log.warning("desktop notification failed: %s", exc)
        return False


def notify(conn, title: str, message: str) -> list[str]:
    """Send to every configured target. Returns the targets that were used."""
    conf = config_for(conn)
    used = []
    if conf["desktop"] and send_desktop(title, message):
        used.append("desktop")
    if conf["url"] and send_webhook(conf["url"], conf["kind"], title, message):
        used.append(conf["kind"])
    return used
