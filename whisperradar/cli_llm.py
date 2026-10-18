"""Subscription CLI settings and read-only connection checks (no generation)."""
import json
import os
import subprocess
from pathlib import Path

from . import db

KEY = "cli_llm_settings"
NAMES = {"codex": "GPT Codex", "claude": "Claude Code"}


def load(cfg):
    result = {"default": "codex", "codex": {"enabled": True, "path": "", "model": ""},
              "claude": {"enabled": True, "path": "", "model": ""}}
    if cfg is None:
        return result
    conn = db.connect(cfg.db_path)
    try:
        try:
            stored = json.loads(db.get_setting(conn, KEY) or "{}")
        except (ValueError, TypeError):
            stored = {}
        if stored.get("default") in NAMES:
            result["default"] = stored["default"]
        for name in NAMES:
            if isinstance(stored.get(name), dict):
                result[name].update({k: stored[name][k] for k in result[name] if k in stored[name]})
        return result
    finally:
        conn.close()


def save(cfg, form):
    data = load(cfg)
    if form.get("default") not in NAMES:
        raise ValueError("Choose Codex or Claude Code as the default.")
    data["default"] = form["default"]
    for name in NAMES:
        path = form.get(name + "_path", "").strip().strip('"')
        model = form.get(name + "_model", "").strip()
        if path and (not Path(path).is_absolute() or not Path(path).is_file()):
            raise ValueError(f"{NAMES[name]} executable path does not exist on this PC.")
        if len(model) > 150 or any(c in model for c in "\r\n\x00"):
            raise ValueError("Enter a valid model name or leave it blank for the CLI default.")
        data[name] = {"enabled": form.get(name + "_enabled") == "on", "path": path, "model": model}
    if not data[data["default"]]["enabled"]:
        raise ValueError("The default CLI provider must be enabled.")
    conn = db.connect(cfg.db_path)
    try:
        db.set_setting(conn, KEY, json.dumps(data))
        db.set_setting(conn, "cli_llm_checks", "{}")
    finally:
        conn.close()


def resolve(cfg, name):
    from . import script_fixes
    if name not in NAMES:
        return None
    entry = load(cfg)[name]
    if not entry["enabled"]:
        return None
    if entry["path"]:
        return entry["path"] if Path(entry["path"]).is_file() else None
    return script_fixes.executable(name)


def environment():
    # CLI accounts only; don't inherit separately billed API credentials/providers.
    env = os.environ.copy()
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
        env.pop(key, None)
    return env


def check(cfg, name):
    exe = resolve(cfg, name)
    if not exe:
        return {"installed": False, "signed_in": False, "message": "CLI not found or disabled. Install it or set its executable path."}
    options = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace",
               "timeout": 20, "env": environment()}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        args = [exe, "login", "status"] if name == "codex" else [exe, "auth", "status"]
        response = subprocess.run(args, **options)
        if name == "codex":
            signed_in = response.returncode == 0 and "chatgpt" in (response.stdout + response.stderr).lower()
        else:
            details = json.loads(response.stdout or "{}")
            method = str(details.get("authMethod", "")).lower()
            signed_in = response.returncode == 0 and details.get("loggedIn") is True and method in ("claude.ai", "oauth")
        return {"installed": True, "signed_in": signed_in,
                "message": "Signed in with a subscription account. Ready." if signed_in else
                "Installed. Subscription sign-in is required; use the sign-in command shown on this card, then check again."}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"installed": True, "signed_in": False, "message": "Could not confirm sign-in. Check the CLI in your terminal, then retry."}


def view(cfg):
    data = load(cfg)
    conn = db.connect(cfg.db_path)
    try:
        try:
            checks = json.loads(db.get_setting(conn, "cli_llm_checks") or "{}")
        except (ValueError, TypeError):
            checks = {}
    finally:
        conn.close()
    entries = []
    for name, title in NAMES.items():
        detected = resolve(cfg, name)
        command = ('& "' + detected + '"' if os.name == "nt" and detected else name)
        command += " login" if name == "codex" else " auth login"
        entries.append({"key": name, "name": title, **data[name], "detected": detected,
                        "login_command": command, "check": checks.get(name)})
    return {"config": data, "entries": entries}
