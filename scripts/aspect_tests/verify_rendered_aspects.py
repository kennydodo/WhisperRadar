"""Check a rendered aspect-ratio TEST production against the expected matrix.

For every shotlist image it reports:

  requested   the aspect the path actually asked for
              renderly/api  -> out\\image-batch.json "aspect"
              flowbatch     -> flowbatch.json "aspectRatio" (or the default)
              renderly/flow -> flowbatch.json (render mode flow = all FlowBatch)
  actual      the pixel shape of images\\<file>, read from the PNG header

and fails when a rendered image's shape is more than RATIO_TOLERANCE away from
what that motion code is supposed to produce.

    python scripts/aspect_tests/verify_rendered_aspects.py <pid>
    python scripts/aspect_tests/verify_rendered_aspects.py <pid> --require-all
"""
import argparse
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from whisperradar.config import load_config  # noqa: E402
from whisperradar import db, settings as settings_mod, studio  # noqa: E402

from aspect_matrix import (  # noqa: E402
    DEFAULT_ASPECT, RATIO_TOLERANCE, RENDERLY_API_MOTIONS,
    flowbatch_aspect, motion_of, ratios_match, renderly_api_aspect,
    renderly_engine_aspect,
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_size(path: Path):
    """(width, height) from a PNG IHDR, or None when it is not a PNG."""
    try:
        with path.open("rb") as fh:
            if fh.read(8) != _PNG_MAGIC:
                return None
            length = struct.unpack(">I", fh.read(4))[0]
            if fh.read(4) != b"IHDR" or length < 8:
                return None
            width, height = struct.unpack(">II", fh.read(8))
            return width, height
    except OSError:
        return None


def _path_kind(eff) -> str:
    engine = (eff.get("engine") or "renderly").lower()
    if engine == "flowbatch":
        return "flowbatch"
    mode = (eff.get("render_mode") or "api").lower()
    # render mode "flow" = every shot on FlowBatch (studio.effective_engine)
    return "flowbatch" if mode == "flow" else "renderly"


def _expected(kind: str, motion: str) -> str:
    if kind == "flowbatch":
        return flowbatch_aspect(motion)
    # engine=renderly/mode=api: only PL/PR go to the paid API; the rest of the
    # shotlist is rendered by the FlowBatch half of the same batch.
    return renderly_engine_aspect(motion)


def _requested_from_batch(pdir: Path, kind: str) -> dict:
    """file -> requested aspect, as recorded by the path itself."""
    if kind == "renderly":
        p = pdir / "out" / "image-batch.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            # export-batch writes an aspect for every missing file, but only the
            # PL/PR slice actually reaches the API; FlowBatch's own ratio
            # for the rest is read from flowbatch.json when present.
            return {i["file"]: i.get("aspect") for i in data.get("images", [])
                    if motion_of(i["file"]) in RENDERLY_API_MOTIONS}
        return {}
    if kind == "flowbatch":
        p = pdir / "flowbatch.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            default = (data.get("defaults") or {}).get("aspectRatio", DEFAULT_ASPECT)
            return {i["file"]: i.get("aspectRatio", default)
                    for i in data.get("images", [])}
        return {}
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pid", type=int)
    ap.add_argument("--require-all", action="store_true",
                    help="also fail when a shotlist image has not been rendered")
    args = ap.parse_args()

    cfg = load_config(ROOT / "config.yaml")
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        prod = db.get_production(conn, args.pid)
        if prod is None:
            print(f"no production {args.pid}")
            return 1
        eff = settings_mod.for_production(conn, prod)
    finally:
        conn.close()

    kind = _path_kind(eff)
    pdir = studio.prod_dir(cfg, args.pid)
    shotlist = json.loads((pdir / "shotlist.json").read_text(encoding="utf-8"))
    requested = _requested_from_batch(pdir, kind)

    print(f"pid {args.pid} - {prod['title']}")
    print(f"path: {kind}" + (f" (engine {eff.get('engine')}, "
                             f"mode {eff.get('render_mode')})" if kind != "flowbatch" else ""))
    print(f"{'file':<16} {'motion':<7} {'expected':<9} {'requested':<10} "
          f"{'actual':<12} result")

    missing = []
    failures = []
    for item in shotlist.get("images", []):
        file_name = item["file"]
        motion = motion_of(file_name)
        expect = _expected(kind, motion)
        req = requested.get(file_name)
        image = pdir / "images" / file_name
        size = png_size(image) if image.exists() else None
        if size is None:
            missing.append(file_name)
            print(f"{file_name:<16} {motion:<7} {expect:<9} {str(req):<10} "
                  f"{'-':<12} MISSING")
            continue
        width, height = size
        ok = ratios_match(expect, width, height)
        if not ok:
            failures.append((file_name, expect, width, height))
        print(f"{file_name:<16} {motion:<7} {expect:<9} {str(req):<10} "
              f"{width}x{height:<6} {'ok' if ok else 'FAIL'}")

    print()
    if missing:
        print(f"{len(missing)} image(s) not rendered yet: {', '.join(missing)}")
    if failures:
        print(f"{len(failures)} MISMATCH(es) (tolerance {RATIO_TOLERANCE:.0%}):")
        for file_name, expect, width, height in failures:
            print(f"  {file_name}: expected {expect}, got {width}x{height}")
    if not failures and not missing:
        print("all rendered images match the expected aspect matrix")
    if failures:
        return 1
    if missing and args.require_all:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
