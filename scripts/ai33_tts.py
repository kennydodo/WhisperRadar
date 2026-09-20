"""OpenSpeaker (ai33.pro) TTS hook for WhisperRadar's Studio audio stage.

Usage (this is what studio.tts_command calls):
    python D:\\Repos\\WhisperRadar\\scripts\\ai33_tts.py {script} {out}

It reads the script text, submits a v3 text-to-speech task to the
OpenSpeaker API, polls until the task finishes, then downloads the mp3
to {out} (the production's audio.mp3).

API key: set the WR_AI33_API_KEY environment variable (or pass --api-key).
Get one from the OpenSpeaker app (ai33.pro) - API section.

Pick a voice with:
    python scripts\\ai33_tts.py --voices --provider minimax
Voice IDs carry a provider prefix, e.g. edge_en-US-BrianNeural,
minimax_male-qn-qingse, elevenlabs_21m00Tcm4TlvDq8ikWAM.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.ai33.pro"
DEFAULT_VOICE = "edge_en-US-BrianMultilingualNeural"
POLL_SECONDS = 5
REQUEST_TIMEOUT = 60


def _request(url: str, *, api_key: str, data: bytes | None = None,
             headers: dict | None = None, method: str = "GET"):
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


def _json(resp: bytes) -> dict:
    try:
        data = json.loads(resp.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"unexpected API response: {resp[:200]!r}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected API response: {resp[:200]!r}")
    return data


def generate(text: str, *, base_url: str, api_key: str, voice_id: str,
             speed: float, file_name: str | None, timeout_s: int) -> str:
    """Submit the TTS task, poll it, return the finished audio URL."""
    body, ctype = _multipart({
        "text": text,
        "voice_id": voice_id,
        "speed": speed,
        "file_name": file_name,
    })
    raw = _request(f"{base_url}/v3/text-to-speech", api_key=api_key,
                   data=body, headers={"Content-Type": ctype},
                   method="POST")
    resp = _json(raw)
    if not resp.get("success") or not resp.get("task_id"):
        raise RuntimeError(f"TTS task was not accepted: {raw[:300]!r}")
    task_id = resp["task_id"]

    deadline = time.monotonic() + timeout_s
    log = lambda msg: print(msg, file=sys.stderr, flush=True)
    log(f"[ai33] task {task_id} submitted (voice {voice_id})")
    while True:
        time.sleep(POLL_SECONDS)
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"TTS task {task_id} not done after {timeout_s}s - "
                "check it in the OpenSpeaker app")
        task = _json(_request(f"{base_url}/v1/task/{task_id}",
                              api_key=api_key))
        status = task.get("status")
        progress = task.get("progress")
        log(f"[ai33] status={status} progress={progress}%")
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
            error = task.get("error_message") or "unknown error"
            raise RuntimeError(f"TTS task failed: {error} (task {task_id})")


def download(url: str, out: str, *, api_key: str) -> None:
    data = _request(url, api_key=api_key)
    with open(out, "wb") as fh:
        fh.write(data)
    print(f"[ai33] wrote {out} ({len(data)} bytes)", file=sys.stderr, flush=True)


def list_voices(*, base_url: str, api_key: str, provider: str | None,
                search: str | None, limit: int) -> None:
    query = [f"page_size={limit}"]
    if provider:
        query.append(f"provider={provider}")
    if search:
        query.append(f"search={search}")
    resp = _json(_request(f"{base_url}/v3/voices?{'&'.join(query)}",
                          api_key=api_key))
    voices = resp.get("data") if isinstance(resp.get("data"), list) \
        else resp.get("voices") or []
    for v in voices:
        vid = v.get("voice_id") or v.get("id") or ""
        name = v.get("name") or ""
        lang = v.get("language") or ""
        print(f"{vid}\t{name}\t{lang}")
    print(f"[ai33] {len(voices)} voice(s) shown", file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate Studio narration audio via OpenSpeaker "
                    "(ai33.pro) - WhisperRadar TTS hook")
    parser.add_argument("script", nargs="?", help="path to script.md "
                        "(the {script} placeholder)")
    parser.add_argument("out", nargs="?", help="output audio path "
                        "(the {out} placeholder, e.g. ...\\audio.mp3)")
    parser.add_argument("--api-key", default=None,
                        help="OpenSpeaker API key (default: WR_AI33_API_KEY "
                        "or AI33_API_KEY env var)")
    parser.add_argument("--base-url", default=os.environ.get(
        "AI33_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--voice", default=os.environ.get(
        "AI33_VOICE", DEFAULT_VOICE),
        help="voice_id with provider prefix (default: %(default)s)")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="0.5-1.5 (default: 1)")
    parser.add_argument("--file-name", default=None,
                        help="friendly name for the task in OpenSpeaker")
    parser.add_argument("--timeout", type=int, default=3300,
                        help="max seconds to wait for the task "
                        "(default: %(default)s)")
    parser.add_argument("--voices", action="store_true",
                        help="list voices instead of generating")
    parser.add_argument("--provider", default=None,
                        help="filter --voices by provider "
                        "(elevenlabs/minimax/clone/edge/kokoro/vbee/fishaudio)")
    parser.add_argument("--search", default=None, help="filter --voices")
    parser.add_argument("--limit", type=int, default=30,
                        help="max voices to list (default: 30)")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("WR_AI33_API_KEY") \
        or os.environ.get("AI33_API_KEY")
    if not api_key:
        parser.error("no API key: set WR_AI33_API_KEY or pass --api-key")

    if args.voices:
        list_voices(base_url=args.base_url, api_key=api_key,
                    provider=args.provider, search=args.search,
                    limit=args.limit)
        return 0

    if not args.script or not args.out:
        parser.error("script and out paths are required "
                     "(called by WhisperRadar with {script} {out})")
    with open(args.script, encoding="utf-8") as fh:
        text = fh.read().strip()
    if not text:
        raise SystemExit(f"script file is empty: {args.script}")

    url = generate(text, base_url=args.base_url, api_key=api_key,
                   voice_id=args.voice, speed=args.speed,
                   file_name=args.file_name, timeout_s=args.timeout)
    download(url, args.out, api_key=api_key)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        print(f"[ai33] ERROR: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
