"""Direct Renderly backend test for the ratios the shotlist pipeline cannot pick.

The pipeline selects 16:9 / 21:9 / 1:1 through motion codes. The remaining
Renderly ratios - 9:16, 4:3, 3:4 - are only reachable by calling the backend
API directly, which is what this does. It POSTs one generation per ratio to
/api/channels/<id>/generate, downloads the result and checks its pixel shape.

THIS SPENDS PAID GEMINI GENERATIONS (one per ratio). It is created but not run
until you say so.

    python scripts/aspect_tests/renderly_direct_aspects.py
    python scripts/aspect_tests/renderly_direct_aspects.py --ratios 9:16,4:3,3:4
    python scripts/aspect_tests/renderly_direct_aspects.py --all   # + 16:9,21:9,1:1

The Renderly backend must already be running (config.yaml studio.renderly_url).
"""
import argparse
import json
import struct
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from whisperradar.config import load_config  # noqa: E402

from aspect_matrix import (  # noqa: E402
    ALL_RENDERLY_ASPECTS, RATIO_TOLERANCE, ratios_match,
)

DEFAULT_RATIOS = ["9:16", "4:3", "3:4"]
CHANNEL_NAME = "ASPECT TEST Direct"
PROMPT = ("TEST aspect-ratio probe: a single centred red circle on a plain "
          "grey background, no text.")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _request(url, method="GET", body=None, timeout=180):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return resp.status, (json.loads(raw) if raw else None)


def _pick_channel(base, channel_arg):
    status, channels = _request(base + "/api/channels")
    channels = channels or []
    if channel_arg:
        for ch in channels:
            if str(ch["id"]) == str(channel_arg) or ch["name"] == channel_arg:
                return ch
        raise SystemExit(f"no channel matching {channel_arg!r}")
    for ch in channels:
        if ch["name"] == CHANNEL_NAME:
            print(f"using existing channel {ch['id']} ({CHANNEL_NAME})")
            return ch
    try:
        status, ch = _request(base + "/api/channels", "POST",
                              {"name": CHANNEL_NAME, "description": "aspect tests"})
        print(f"created channel {ch['id']} ({CHANNEL_NAME})")
        return ch
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"could not create the test channel: {exc}")


def _png_size(raw: bytes):
    if raw[:8] != _PNG_MAGIC:
        return None
    length = struct.unpack(">I", raw[4:8])[0]
    if raw[8:12] != b"IHDR" or length < 8:
        return None
    return struct.unpack(">II", raw[12:20])


def _download(url):
    with urllib.request.urlopen(url, timeout=180) as resp:
        return resp.read()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ratios", default=",".join(DEFAULT_RATIOS),
                    help="comma-separated ratios (default: 9:16,4:3,3:4)")
    ap.add_argument("--all", action="store_true",
                    help="run every Renderly ratio (adds 16:9,21:9,1:1)")
    ap.add_argument("--channel", default=None,
                    help="Renderly channel id or name (default: reuse/create "
                         f"'{CHANNEL_NAME}')")
    args = ap.parse_args()

    ratios = list(ALL_RENDERLY_ASPECTS) if args.all else \
        [r.strip() for r in args.ratios.split(",") if r.strip()]
    bad = [r for r in ratios if r not in ALL_RENDERLY_ASPECTS]
    if bad:
        ap.error(f"unsupported ratio(s): {', '.join(bad)}")

    cfg = load_config(ROOT / "config.yaml")
    base = cfg.renderly_url.rstrip("/")
    try:
        channel = _pick_channel(base, args.channel)
    except urllib.error.URLError as exc:
        raise SystemExit(f"Renderly backend not reachable at {base}: {exc}")

    print(f"\n{'ratio':<7} {'status':<7} {'actual':<12} result")
    failures = []
    for ratio in ratios:
        try:
            _, gen = _request(
                f"{base}/api/channels/{channel['id']}/generate", "POST",
                {"prompt": PROMPT, "name": f"aspect_{ratio.replace(':', 'x')}",
                 "aspect_ratio": ratio, "image_size": "1K"})
        except urllib.error.HTTPError as exc:
            print(f"{ratio:<7} {'http':<7} {'-':<12} FAIL ({exc.code})")
            failures.append((ratio, f"HTTP {exc.code}"))
            continue

        if gen.get("status") != "done" or not gen.get("image_url"):
            print(f"{ratio:<7} {gen.get('status'):<7} {'-':<12} "
                  f"FAIL ({gen.get('error') or 'no image'})")
            failures.append((ratio, gen.get("status")))
            continue

        url = gen["image_url"]
        if url.startswith("/"):
            url = base + url
        size = _png_size(_download(url))
        if size is None:
            print(f"{ratio:<7} {'done':<7} {'not-png':<12} FAIL")
            failures.append((ratio, "not a PNG"))
            continue
        width, height = size
        ok = ratios_match(ratio, width, height, RATIO_TOLERANCE)
        if not ok:
            failures.append((ratio, f"{width}x{height}"))
        print(f"{ratio:<7} {'done':<7} {width}x{height:<7} "
              f"{'ok' if ok else 'FAIL'}")

    print()
    if failures:
        print(f"{len(failures)} MISMATCH(es) (tolerance {RATIO_TOLERANCE:.0%}):")
        for ratio, detail in failures:
            print(f"  {ratio}: {detail}")
        return 1
    print("every ratio produced an image of the expected shape")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
