"""OpenSpeaker (ai33.pro) API client - narration voices + TTS tasks.

Used by the Studio audio stage (tts_command hook) and the dashboard's
voice picker. API key: config `studio.ai33_api_key` or the WR_AI33_API_KEY /
AI33_API_KEY environment variable. Docs are embedded in the OpenSpeaker app;
short version: POST /v3/text-to-speech (FormData) -> task_id, poll
GET /v1/task/<task_id> until done, download output_uri / metadata.audio_url.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

API_BASE = "https://api.ai33.pro"
PROVIDERS = ("edge", "minimax", "kokoro", "elevenlabs", "vbee", "fishaudio",
             "clone")
REQUEST_TIMEOUT = 60
VOICES_CACHE_TTL = 600

_voices_cache: dict = {"at": 0.0, "data": []}


def api_key(cfg=None) -> str | None:
    key = getattr(cfg, "studio_ai33_api_key", None) if cfg else None
    return (key or os.environ.get("WR_AI33_API_KEY")
            or os.environ.get("AI33_API_KEY") or None)


def base_url(cfg=None) -> str:
    url = getattr(cfg, "studio_ai33_base_url", None) if cfg else None
    return (url or os.environ.get("AI33_BASE_URL") or API_BASE).rstrip("/")


def _log(msg: str) -> None:
    print(f"[ai33] {msg}", file=sys.stderr, flush=True)


def _request(url: str, api_key: str, *, data: bytes | None = None,
             headers: dict | None = None, method: str = "GET") -> bytes:
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("xi-api-key", api_key)
    for key, val in (headers or {}).items():
        req.add_header(key, val)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except OSError:
            pass
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach {url}: {exc.reason}") from exc


def _json(resp: bytes) -> dict:
    try:
        data = json.loads(resp.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"unexpected API response: {resp[:200]!r}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected API response: {resp[:200]!r}")
    return data


def _multipart(fields: dict) -> tuple[bytes, str]:
    boundary = "WRai33TTS7f2c1b"
    parts = []
    for name, val in fields.items():
        if val is None:
            continue
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{val}\r\n".encode("utf-8"))
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    return body, f"multipart/form-data; boundary={boundary}"


def _fetch_voices(cfg, provider: str, limit: int) -> list[dict]:
    key = api_key(cfg)
    if not key:
        raise RuntimeError("no OpenSpeaker API key - set WR_AI33_API_KEY "
                           "(or studio.ai33_api_key in config.yaml)")
    resp = _json(_request(
        f"{base_url(cfg)}/v3/voices?provider={provider}&page_size={limit}",
        key))
    raw = resp.get("data") if isinstance(resp.get("data"), list) else []
    voices = []
    for v in raw:
        if not isinstance(v, dict) or not v.get("voice_id"):
            continue
        voices.append({
            "voice_id": str(v["voice_id"]),
            "name": str(v.get("name") or v["voice_id"]),
            "provider": provider,
            "language": str(v.get("language") or "") or None,
            "gender": str(v.get("gender") or "") or None,
            "accent": str(v.get("accent") or "") or None,
            "preview_url": v.get("preview_url") or None,
        })
    return voices


def voices(cfg, provider: str | None = None, limit: int = 100,
           refresh: bool = False) -> list[dict]:
    """Voice catalog (cached ~10 min). provider=None fetches every known
    provider; each voice_id already carries its provider prefix."""
    if provider:
        return _fetch_voices(cfg, provider, limit)
    now = time.monotonic()
    if not refresh and _voices_cache["data"] and \
            now - _voices_cache["at"] < VOICES_CACHE_TTL:
        return _voices_cache["data"]
    all_voices: list[dict] = []
    for prov in PROVIDERS:
        try:
            all_voices.extend(_fetch_voices(cfg, prov, limit))
        except RuntimeError as exc:
            _log(f"skipping provider '{prov}': {exc}")
    _voices_cache["at"] = now
    _voices_cache["data"] = all_voices
    return all_voices


def generate(cfg, text: str, voice_id: str | None, *, speed: float = 1.0,
             file_name: str | None = None, timeout_s: int = 3300,
             log=None) -> str:
    """Submit a v3 TTS task, poll until done, return the audio URL."""
    key = api_key(cfg)
    if not key:
        raise RuntimeError("no OpenSpeaker API key - set WR_AI33_API_KEY "
                           "(or studio.ai33_api_key in config.yaml)")
    log = log or _log
    body, ctype = _multipart({
        "text": text,
        "voice_id": voice_id or "edge_en-US-BrianMultilingualNeural",
        "speed": speed,
        "file_name": file_name,
    })
    resp = _json(_request(f"{base_url(cfg)}/v3/text-to-speech", key,
                          data=body, headers={"Content-Type": ctype},
                          method="POST"))
    if not resp.get("success") or not resp.get("task_id"):
        raise RuntimeError(f"TTS task was not accepted: {resp}")
    task_id = resp["task_id"]
    log(f"task {task_id} submitted (voice {voice_id or 'default'})")

    deadline = time.monotonic() + timeout_s
    while True:
        time.sleep(5)
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"TTS task {task_id} not done after {timeout_s}s - "
                "check it in the OpenSpeaker app")
        task = _json(_request(f"{base_url(cfg)}/v1/task/{task_id}", key))
        status = task.get("status")
        log(f"status={status} progress={task.get('progress')}%")
        if status == "done":
            meta = task.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except ValueError:
                    meta = {}
            url = task.get("output_uri") or meta.get("audio_url")
            if not url:
                raise RuntimeError(f"task done but no audio_url: {task}")
            return url
        if status not in ("doing", "pending", None):
            raise RuntimeError(f"TTS task failed: "
                               f"{task.get('error_message') or 'unknown'} "
                               f"(task {task_id})")


def download(cfg, url: str, out: str) -> None:
    key = api_key(cfg)
    data = _request(url, key or "")
    with open(out, "wb") as fh:
        fh.write(data)
    _log(f"wrote {out} ({len(data)} bytes)")
