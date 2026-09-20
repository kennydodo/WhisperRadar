"""OpenSpeaker (ai33.pro) TTS hook for WhisperRadar's Studio audio stage.

Usage (this is what studio.tts_command calls):
    python D:\\Repos\\WhisperRadar\\scripts\\ai33_tts.py {script} {out} --voice {voice}

It reads the script text, submits a v3 text-to-speech task to the
OpenSpeaker API, polls until the task finishes, then downloads the mp3
to {out} (the production's audio.mp3). {voice} is the production's
selected narration voice (empty = default).

API key: set the WR_AI33_API_KEY environment variable (or pass --api-key).
Get one from the OpenSpeaker app (ai33.pro) - API section.

Pick a voice with:
    python scripts\\ai33_tts.py --voices --provider minimax
Voice IDs carry a provider prefix, e.g. edge_en-US-BrianNeural,
minimax_male-qn-qingse, elevenlabs_21m00Tcm4TlvDq8ikWAM.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whisperradar import ai33  # noqa: E402

DEFAULT_VOICE = "edge_en-US-BrianMultilingualNeural"


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
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--voice", default="",
                        help="voice_id with provider prefix; empty = "
                        "AI33_VOICE env var or %(default)s default")
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
                        "(elevenlabs/minimax/clone/edge/kokoro/vbee/"
                        "fishaudio; default: all)")
    parser.add_argument("--search", default=None, help="filter --voices")
    parser.add_argument("--limit", type=int, default=30,
                        help="max voices per provider to list (default: 30)")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("WR_AI33_API_KEY") \
        or os.environ.get("AI33_API_KEY")
    if not api_key:
        parser.error("no API key: set WR_AI33_API_KEY or pass --api-key")

    class _Cfg:  # minimal config stand-in for the ai33 module
        studio_ai33_api_key = api_key
        studio_ai33_base_url = args.base_url

    cfg = _Cfg()

    if args.voices:
        providers = [args.provider] if args.provider else list(ai33.PROVIDERS)
        for prov in providers:
            print(f"--- {prov} ---", file=sys.stderr, flush=True)
            try:
                items = ai33.voices(cfg, provider=prov, limit=args.limit)
            except RuntimeError as exc:
                print(f"[ai33] ERROR: {exc}", file=sys.stderr, flush=True)
                continue
            for v in items:
                print(f"{v['voice_id']}\t{v['name']}\t"
                      f"{v.get('language') or ''}\t{v.get('gender') or ''}")
        return 0

    if not args.script or not args.out:
        parser.error("script and out paths are required "
                     "(called by WhisperRadar with {script} {out})")
    with open(args.script, encoding="utf-8") as fh:
        text = fh.read().strip()
    if not text:
        raise SystemExit(f"script file is empty: {args.script}")

    voice = (args.voice or "").strip() \
        or os.environ.get("AI33_VOICE") or DEFAULT_VOICE

    url = ai33.generate(cfg, text, voice, speed=args.speed,
                        file_name=args.file_name, timeout_s=args.timeout)
    ai33.download(cfg, url, args.out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        print(f"[ai33] ERROR: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
