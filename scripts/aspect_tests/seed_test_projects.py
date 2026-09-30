"""Create the two aspect-ratio TEST productions. RENDERS NOTHING.

This writes only DB rows and a compact synthetic shotlist.json - 10 images,
exactly one each of the wide trio PL/PR/PV plus ordinary motions. That is
enough to see every aspect path do its job without a long run:

  * "ASPECT TEST Renderly"   engine=renderly, mode=api   -> the paid API path
  * "ASPECT TEST FlowBatch"  engine=flowbatch            -> the FlowBatch path

No Renderly/Flow/FlowBatch process is started and no generation is requested.
Rendering is a separate, explicit step you run under supervision:

    python scripts/run_stage.py <pid> images

Then, to check the results:

    python scripts/aspect_tests/verify_rendered_aspects.py <pid>

Options:
    --motions ST,ZI,...   override the shotlist (default: the 10-image studio test)
    --print-only          do not write anything, just report what it would do
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from whisperradar.config import load_config  # noqa: E402
from whisperradar import db, studio  # noqa: E402

from aspect_matrix import MOTIONS  # noqa: E402

STYLE = ("TEST project for the 2026-09 aspect-ratio work - flat lighting, "
         "centred subject, no text or captions, 35mm still.")

# The compact studio test: 10 prompts, exactly one each of PL/PR/PV so the
# wide/pan paths are exercised. On engine=renderly the API handles PL/PR (21:9)
# while everything else - PV included, now 16:9 - goes through the Flow Driver;
# FlowBatch renders all 10 through Flow (PL/PR/PV as 16:9).
STUDIO_TEST_MOTIONS = ["ST", "ZI", "ZO", "PU", "PD", "ST", "ZI", "PL", "PR", "PV"]


def _shotlist(motions) -> dict:
    images = []
    shots = []
    for i, motion in enumerate(motions, 1):
        file_name = f"A{i:02d}_{motion}.png"
        images.append({
            "file": file_name,
            "prompt": (f"TEST frame {i}, motion {motion}: a neutral studio scene "
                       f"used only to exercise the {motion} aspect path."),
        })
        # ImgToVideo.Cli's parser requires a top-level "shots" array (cues +
        # asset); the motion rides along for readability, but the batch derives
        # it from the filename suffix.
        shots.append({
            "shot_id": f"SH{i:03d}",
            "cues": str(i),
            "asset": file_name,
            "motion": motion,
        })
    return {"style": STYLE, "shots": shots, "images": images}


def _ensure_channel(conn, name, **fields) -> int:
    """Reuse an existing own_channel of this name, else create it."""
    existing = db.get_own_channel(conn, name)
    if existing is not None:
        db.update_own_channel(conn, existing["id"], **fields)
        return existing["id"]
    return db.create_own_channel(conn, name, **fields)


def _prepare(cfg, conn, title, channel_name, channel_fields, motions, print_only):
    oc = _ensure_channel(conn, channel_name, **channel_fields)
    pid = db.create_production(conn, title, "general")
    db.update_production(conn, pid, own_channel_id=oc, autorun=0)
    pdir = studio.prod_dir(cfg, pid)
    for sub in ("images", "refs", "out"):
        (pdir / sub).mkdir(exist_ok=True)
    data = _shotlist(motions)
    if not print_only:
        (pdir / "style.md").write_text(STYLE + "\n", encoding="utf-8")
        (pdir / "shotlist.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
    print(f"pid {pid}: {title}")
    print(f"  engine: {channel_fields.get('default_engine')}"
          + (f", mode: {channel_fields['default_render_mode']}"
             if channel_fields.get("default_render_mode") else ""))
    print(f"  folder: {pdir}")
    print(f"  shots:  {len(data['images'])} "
          f"({', '.join(m for m in motions)})")
    return pid


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--motions", default=",".join(STUDIO_TEST_MOTIONS),
                    help="comma-separated motion codes (default: the 10-image "
                         "studio test - 1 each of PL/PR/PV plus ordinary shots)")
    ap.add_argument("--print-only", action="store_true",
                    help="report what would be created without writing")
    args = ap.parse_args()

    motions = [m.strip().upper() for m in args.motions.split(",") if m.strip()]
    unknown = [m for m in motions if m not in MOTIONS]
    if unknown:
        ap.error(f"unknown motion code(s): {', '.join(unknown)}")

    cfg = load_config(ROOT / "config.yaml")
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        _prepare(cfg, conn, "ASPECT TEST Renderly", "ASPECT TEST Renderly",
                 {"default_engine": "renderly", "default_render_mode": "api"},
                 motions, args.print_only)
        _prepare(cfg, conn, "ASPECT TEST FlowBatch", "ASPECT TEST FlowBatch",
                 {"default_engine": "flowbatch"},
                 motions, args.print_only)
    finally:
        conn.close()

    if args.print_only:
        print("\n(print-only: nothing written)")
    else:
        print("\nCreated. Render later with: python scripts/run_stage.py <pid> images")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
