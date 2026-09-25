"""Run ONE stage of ONE production - for the step-by-step retest.

    python scripts/run_stage.py 10 style --plan   # what auto-run would do, no cost
    python scripts/run_stage.py 10 style
    python scripts/run_stage.py 10 script --provider deepseek
    python scripts/run_stage.py 10 images

Exit code 0 on "ok", 1 on paused/failed - so a shell can stop at the first
problem. The LLM/refs/images stages use exactly the same runners as auto-run,
so a single-stage run behaves like the pipeline would.
"""
import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar.config import load_config  # noqa: E402
from whisperradar import autorun, db  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pid", type=int)
    ap.add_argument("stage", choices=[*db.STAGES, "all"],
                    help="one stage, or 'all' to run the whole pipeline from here")
    ap.add_argument("--provider", default=None,
                    help="LLM provider for style/script/shots (default: the "
                         "production's channel/global provider)")
    ap.add_argument("--plan", action="store_true",
                    help="print what the stage would do and exit (no API calls)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    cfg = load_config(ROOT / "config.yaml")
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    log = lambda m: print(m, flush=True)  # noqa: E731

    if args.stage == "all":
        t0 = time.monotonic()
        result = autorun.run_pipeline(cfg, args.pid, log=log)
        print(f"\npid {args.pid} pipeline -> {result} "
              f"({time.monotonic() - t0:.0f}s)")
        return 0 if result == "ok" else 1

    plan = autorun.stage_action(cfg, args.pid, args.stage)
    print(f"plan: {plan['stage']} -> {plan['action']}: {plan['detail']}")
    if args.plan or plan["action"] == "skip":
        return 0

    if args.stage == "images":
        params = autorun._stage_params(cfg, args.pid, "images", log)
    elif args.stage in ("style", "script", "shots"):
        params = {"provider": args.provider
                  or autorun._default_provider(cfg, args.pid)}
    else:
        params = {}

    t0 = time.monotonic()
    result = autorun.run_stage(cfg, args.pid, args.stage, params=params)
    took = time.monotonic() - t0
    print(f"\npid {args.pid} {args.stage} -> {result} ({took:.0f}s)")

    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        step = db.latest_steps(conn, args.pid).get(args.stage)
        if step:
            print(f"step: {step['status']} - {step['detail']}")
        prod = db.get_production(conn, args.pid)
        if prod and prod["warning"]:
            print(f"warning: {prod['warning']}")
    finally:
        conn.close()
    return 0 if result == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
