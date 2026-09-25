"""OpenSpeaker (ai33.pro) API client - narration voices + TTS tasks.

Used by the Studio audio stage (tts_command hook) and the dashboard's
voice picker. API key: Settings > LLM in the dashboard, else config
`studio.ai33_api_key`, else the WR_AI33_API_KEY / AI33_API_KEY environment
variable. Docs are embedded in the OpenSpeaker app;
short version: POST /v3/text-to-speech (FormData) -> task_id, poll
GET /v1/task/<task_id> until done, download output_uri / metadata.audio_url.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.ai33.pro"
PROVIDERS = ("edge", "minimax", "kokoro", "elevenlabs", "vbee", "fishaudio",
             "clone")
REQUEST_TIMEOUT = 60
VOICES_CACHE_TTL = 600
VOICES_MAX_PAGES = 40  # safety cap: 40 pages x page_size per provider

# cdn.ai33.pro rejects requests without a browser-like User-Agent (Cloudflare
# error 1010), so every call sends one.
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# studio.ai33_voice_source / the studio.ai33_voices sentinel that makes the
# picker show the voices starred in the OpenSpeaker app instead of a
# hand-written shortlist.
FAVORITES = "favorites"

_voices_cache: dict = {"at": 0.0, "data": []}
_curated_cache: dict = {"key": None, "at": 0.0, "data": []}
_favorites_cache: dict = {"at": 0.0, "data": []}


def curated_ids(cfg) -> list[str]:
    ids = getattr(cfg, "studio_ai33_voices", None) if cfg else None
    return [str(v).strip() for v in (ids or []) if str(v).strip()]


def wants_favorites(cfg) -> bool:
    """True when the picker should list the OpenSpeaker favorites: either
    studio.ai33_voice_source: favorites, or ["favorites"] as the whole
    studio.ai33_voices shortlist."""
    src = (getattr(cfg, "studio_ai33_voice_source", None) or "").strip().lower()
    if src:
        return src == FAVORITES
    ids = curated_ids(cfg)
    return len(ids) == 1 and ids[0].lower() == FAVORITES


def _provider_of(voice_id: str) -> str:
    """The provider prefix of a voice_id ('elevenlabs_xxx' -> 'elevenlabs')."""
    provider, _, bare = str(voice_id).partition("_")
    return provider if bare else ""


def _normalize(v: dict, provider: str) -> dict:
    return {
        "voice_id": str(v["voice_id"]),
        "name": str(v.get("name") or v["voice_id"]),
        "provider": provider,
        "language": str(v.get("language") or "") or None,
        "gender": str(v.get("gender") or "") or None,
        "accent": str(v.get("accent") or "") or None,
        "preview_url": v.get("preview_url") or None,
    }


def resolve_voice(cfg, voice_id: str) -> dict | None:
    """Resolve one voice_id to its metadata via the API's id-aware search.
    Returns None when the voice cannot be found."""
    key = api_key(cfg)
    if not key:
        raise RuntimeError("no OpenSpeaker API key - add one in Settings > "
                           "LLM, or set WR_AI33_API_KEY")
    provider, _, bare = voice_id.partition("_")
    if provider not in PROVIDERS or not bare:
        return None
    resp = _json(_request(
        f"{base_url(cfg)}/v3/voices?provider={provider}"
        f"&search={urllib.parse.quote(bare)}&page_size=10", key))
    for v in resp.get("data") or []:
        if isinstance(v, dict) and v.get("voice_id") == voice_id:
            return _normalize(v, provider)
    return None


def _resolve_curated(cfg, ids: list[str]) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor

    def one(vid: str) -> dict:
        try:
            found = resolve_voice(cfg, vid)
        except RuntimeError:
            found = None
        if found:
            return found
        provider = vid.partition("_")[0]
        return {"voice_id": vid, "name": vid, "provider": provider,
                "language": None, "gender": None, "accent": None,
                "preview_url": None}

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(ids)))) as pool:
        return list(pool.map(one, ids))


def favorites(cfg, refresh: bool = False) -> list[dict]:
    """The voices starred in the OpenSpeaker app (GET /v3/favorites).

    The response is {success, favorites: [{voice_id, provider, voice_data}]};
    `provider` there is a generic "v3", so the real provider comes from the
    voice_id prefix. Metadata is normalized like the catalog and cached for
    VOICES_CACHE_TTL seconds. Undocumented endpoint - verified 2026-09-22.
    """
    key = api_key(cfg)
    if not key:
        raise RuntimeError("no OpenSpeaker API key - add one in Settings > "
                           "LLM, or set WR_AI33_API_KEY")
    now = time.monotonic()
    if not refresh and _favorites_cache["data"] \
            and now - _favorites_cache["at"] < VOICES_CACHE_TTL:
        return _favorites_cache["data"]
    resp = _json(_request(f"{base_url(cfg)}/v3/favorites", key))
    out: list[dict] = []
    seen: set[str] = set()
    for fav in resp.get("favorites") or []:
        if not isinstance(fav, dict):
            continue
        data = fav.get("voice_data")
        if not isinstance(data, dict):
            data = {"voice_id": fav.get("voice_id"), "name": fav.get("name")}
        vid = str(data.get("voice_id") or fav.get("voice_id") or "").strip()
        if not vid or vid in seen:
            continue
        provider = _provider_of(vid) or str(fav.get("provider") or "").strip()
        seen.add(vid)
        out.append(_normalize({**data, "voice_id": vid}, provider))
    _favorites_cache["at"] = now
    _favorites_cache["data"] = out
    return out


def _saved_setting(cfg, key: str) -> str | None:
    """A value from the dashboard's settings table, when cfg points at a DB.

    The Settings > LLM page is the configured home for the AI33 key, so it
    wins over config.yaml; a missing DB or table is simply "not set"."""
    db_path = getattr(cfg, "db_path", None) if cfg else None
    if not db_path:
        return None
    try:
        from . import db

        conn = db.connect(db_path)
        try:
            return (db.get_setting(conn, key) or "").strip() or None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - unreadable DB means "not configured"
        return None


def api_key(cfg=None) -> str | None:
    # Priority: Settings > LLM in the dashboard, then config.yaml, then env.
    key = _saved_setting(cfg, "ai33_api_key") \
        or (getattr(cfg, "studio_ai33_api_key", None) if cfg else None)
    return (key or os.environ.get("WR_AI33_API_KEY")
            or os.environ.get("AI33_API_KEY") or None)


def base_url(cfg=None) -> str:
    url = _saved_setting(cfg, "ai33_base_url") \
        or (getattr(cfg, "studio_ai33_base_url", None) if cfg else None)
    return (url or os.environ.get("AI33_BASE_URL") or API_BASE).rstrip("/")


def _log(msg: str) -> None:
    print(f"[ai33] {msg}", file=sys.stderr, flush=True)


def _request(url: str, api_key: str, *, data: bytes | None = None,
             headers: dict | None = None, method: str = "GET") -> bytes:
    req = urllib.request.Request(url, data=data, method=method)
    # cdn.ai33.pro sits behind Cloudflare, which bans urllib's default
    # "Python-urllib/3.x" signature with error 1010 - the API host does not
    # care, but the mp3 download does. Send a browser-like agent everywhere.
    for key, val in DEFAULT_HEADERS.items():
        req.add_header(key, val)
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


def _fetch_voices(cfg, provider: str, page_size: int,
                  search: str | None = None) -> list[dict]:
    """All voices of one provider - follows pagination.has_more up to a
    sane cap so catalogs larger than one page are fully loaded."""
    key = api_key(cfg)
    if not key:
        raise RuntimeError("no OpenSpeaker API key - add one in Settings > "
                           "LLM, or set WR_AI33_API_KEY")
    voices = []
    page = 1
    while True:
        resp = _json(_request(
            f"{base_url(cfg)}/v3/voices?provider={provider}"
            f"&page_size={page_size}&page={page}"
            + (f"&search={urllib.parse.quote(search)}" if search else ""), key))
        raw = resp.get("data") if isinstance(resp.get("data"), list) else []
        for v in raw:
            if not isinstance(v, dict) or not v.get("voice_id"):
                continue
            voices.append(_normalize(v, provider))
        pagination = resp.get("pagination") or {}
        if not raw or not pagination.get("has_more") \
                or page >= VOICES_MAX_PAGES:
            break
        page += 1
    return voices


def _fetch_all_providers(cfg, page_size: int,
                         search: str | None = None) -> list[dict]:
    """Fetch every provider in parallel - a cold cache fills in a few
    seconds instead of tens of seconds of sequential calls."""
    from concurrent.futures import ThreadPoolExecutor

    all_voices: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(PROVIDERS)) as pool:
        futures = {prov: pool.submit(_fetch_voices, cfg, prov, page_size,
                                     search)
                   for prov in PROVIDERS}
        for prov, fut in futures.items():
            try:
                all_voices.extend(fut.result())
            except RuntimeError as exc:
                _log(f"skipping provider '{prov}': {exc}")
    return all_voices


def voices(cfg, provider: str | None = None, page_size: int = 100,
           refresh: bool = False, search: str | None = None,
           source: str | None = None) -> list[dict]:
    """Voice list for the picker.

    source='favorites' (or studio.ai33_voice_source: favorites, or the
    ["favorites"] sentinel in studio.ai33_voices) returns the voices starred
    in OpenSpeaker; an empty favorites list falls back to the shortlist /
    full catalog. Otherwise: when `studio.ai33_voices` is configured, only
    those voices are returned (in config order, metadata resolved via the
    API's id search). Otherwise the full catalog is fetched (cached ~10 min,
    providers in parallel). provider + search narrow the raw catalog; each
    voice_id carries its provider prefix."""
    if source is None and wants_favorites(cfg):
        source = FAVORITES
    if source and source.strip().lower() == FAVORITES \
            and provider is None and search is None:
        starred = favorites(cfg, refresh=refresh)
        if starred:
            return starred
        _log("no OpenSpeaker favorites - falling back to the shortlist/catalog")
    # the sentinel is a mode switch, never a real voice id
    ids = [i for i in curated_ids(cfg) if i.lower() != FAVORITES]
    if provider is None and search is None and ids:
        key = tuple(ids)
        now = time.monotonic()
        if refresh or not _curated_cache["data"] \
                or _curated_cache["key"] != key \
                or now - _curated_cache["at"] >= VOICES_CACHE_TTL:
            _curated_cache["key"] = key
            _curated_cache["data"] = _resolve_curated(cfg, ids)
            _curated_cache["at"] = now
        return _curated_cache["data"]
    if provider:
        return _fetch_voices(cfg, provider, page_size, search=search)
    now = time.monotonic()
    if not refresh and not search and _voices_cache["data"] and \
            now - _voices_cache["at"] < VOICES_CACHE_TTL:
        return _voices_cache["data"]
    all_voices = _fetch_all_providers(cfg, page_size, search=search)
    if not search:
        _voices_cache["at"] = now
        _voices_cache["data"] = all_voices
    return all_voices


def warm_cache(cfg) -> None:
    """Prefetch voice metadata in the background (no-op without an API
    key; errors are ignored). Curated shortlists are resolved with a few
    cheap id searches; otherwise the full catalog is fetched."""
    import threading

    if not api_key(cfg):
        return

    def run():
        try:
            voices(cfg)
        except Exception:  # noqa: BLE001 - warming must never crash anything
            pass

    threading.Thread(target=run, daemon=True, name="ai33-voice-warm").start()


def generate(cfg, text: str, voice_id: str | None, *, speed: float = 1.0,
             file_name: str | None = None, timeout_s: int = 3300,
             log=None) -> str:
    """Submit a v3 TTS task, poll until done, return the audio URL."""
    key = api_key(cfg)
    if not key:
        raise RuntimeError("no OpenSpeaker API key - add one in Settings > "
                           "LLM, or set WR_AI33_API_KEY")
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
