"""WhisperRadar command-line interface."""

import argparse
import logging
import re
import shutil
import sys
from pathlib import Path

from . import __version__, db, pipeline
from .config import load_config
from .watch import CHANNEL_ID_RE, resolve_channel

log = logging.getLogger("whisperradar")


def _utf8_console() -> None:
    """Make stdout/stderr survive non-ASCII output.

    Stage logs carry text from other tools (the Flow Driver prints warnings
    with U+26A0, FlowBatch prints box drawing), and a Windows console is
    cp1252 by default, so a plain print() raises UnicodeEncodeError and kills
    the stage. Replacing unencodable characters is always better than dying.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _slugify(text: str, maxlen: int = 60) -> str:
    slug = re.sub(r"[^\w\s-]", "", text).strip()
    slug = re.sub(r"[\s_]+", "_", slug)
    return slug[:maxlen].strip("_") or "video"


def format_duration(seconds) -> str:
    """Human-readable duration, e.g. '1h 02m', '12m 30s', '45s'."""
    if not seconds:
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _open(cfg):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    db.sync_channels(conn, cfg.channels)
    return conn


def cmd_init(cfg, args):
    for d in (cfg.audio_dir, cfg.transcripts_dir, cfg.exports_dir):
        d.mkdir(parents=True, exist_ok=True)
    conn = _open(cfg)
    conn.close()
    print(f"Initialized WhisperRadar at {cfg.base_dir}")
    print(f"  db:         {cfg.db_path}")
    print(f"  audio:      {cfg.audio_dir}")
    print(f"  transcripts:{cfg.transcripts_dir}")


def cmd_add(cfg, args):
    raw = args.channel.strip()
    conn = _open(cfg)
    try:
        if CHANNEL_ID_RE.match(raw):
            channel_id, name = raw, args.name or f"channel-{raw[:8]}"
        else:
            log.info("resolving %s ...", raw)
            channel_id, name = resolve_channel(raw)
            if args.name:
                name = args.name
        if not name:
            name = f"channel-{channel_id[:8]}"
        db.add_channel(conn, name=name, channel_id=channel_id,
                       kind=args.kind, genre=args.genre)
        print(f"Added channel '{name}' ({channel_id}, {args.kind}, genre: {args.genre})")
    finally:
        conn.close()


def cmd_remove(cfg, args):
    conn = _open(cfg)
    try:
        if db.remove_channel(conn, args.channel):
            print(f"Removed '{args.channel}'")
        else:
            print(f"No channel matching '{args.channel}'")
    finally:
        conn.close()


def cmd_channels(cfg, args):
    conn = _open(cfg)
    try:
        rows = db.list_channels(conn)
        if not rows:
            print("No channels yet. Add one: python wr.py add <channel-url>")
            return
        for row in rows:
            state = "active" if row["active"] else "paused"
            print(f"{row['name']:<30} {row['channel_id']}  {row['kind']:<8} "
                  f"{row['genre']:<12} {state}")
    finally:
        conn.close()


def cmd_edit(cfg, args):
    conn = _open(cfg)
    try:
        row = db.get_channel(conn, args.channel)
        if not row:
            print(f"No channel matching '{args.channel}'")
            return
        updates = {}
        if args.name:
            updates["name"] = args.name
        if args.genre:
            updates["genre"] = args.genre.strip() or "general"
        if args.kind:
            updates["kind"] = args.kind
        if args.pause:
            updates["active"] = 0
        if args.activate:
            updates["active"] = 1
        if not updates:
            print("Nothing to change. Use --genre, --name, --kind, "
                  "--pause or --activate.")
            return
        db.update_channel(conn, row["channel_id"], **updates)
        moved = 0
        if updates.get("genre") and updates["genre"] != row["genre"]:
            moved = pipeline.move_channel_files(cfg, conn, row, updates["genre"])
        label = updates.get("name") or row["name"]
        changes = ", ".join(f"{k}={v}" for k, v in updates.items())
        print(f"Updated '{label}': {changes}")
        if moved:
            print(f"moved {moved} file(s) into data\\...\\{updates['genre']}\\")
    finally:
        conn.close()


def cmd_videos(cfg, args):
    conn = _open(cfg)
    try:
        rows = db.get_videos(conn, status=args.status, genre=args.genre,
                             limit=args.limit)
        if not rows:
            print(f"No videos{' with status ' + args.status if args.status else ''}"
                  f"{' in genre ' + args.genre if args.genre else ''}.")
            return
        for row in rows:
            dur = format_duration(row["duration"])
            dur_part = f"  [{dur}]" if dur else ""
            print(f"{row['status']:<12} {row['published_at'] or '?'}  "
                  f"[{row['channel_name']}|{row['channel_genre']}] {row['title']}"
                  f"{dur_part}  ({row['video_id']})")
    finally:
        conn.close()


def cmd_watch(cfg, args):
    conn = _open(cfg)
    try:
        pipeline.refresh_feeds(cfg, conn)
    finally:
        conn.close()


def cmd_download(cfg, args):
    conn = _open(cfg)
    try:
        ids = None
        if args.video != "all":
            if not db.get_video(conn, args.video):
                print(f"No such video: {args.video}")
                return
            db.set_video(conn, args.video, status="new", auto=1, error=None)
            ids = [args.video]
        pipeline.process_downloads(cfg, conn, video_ids=ids)
    finally:
        conn.close()


def cmd_transcribe(cfg, args):
    conn = _open(cfg)
    try:
        ids = None
        if args.video != "all":
            row = db.get_video(conn, args.video)
            if not row:
                print(f"No such video: {args.video}")
                return
            if not row["audio_path"] or not Path(row["audio_path"]).exists():
                print(f"Audio not downloaded yet - run first: "
                      f"python wr.py download {args.video}")
                return
            db.set_video(conn, args.video, status="downloaded", error=None)
            ids = [args.video]
        pipeline.process_transcripts(cfg, conn, video_ids=ids)
    finally:
        conn.close()


def cmd_delete(cfg, args):
    conn = _open(cfg)
    try:
        row = db.get_video(conn, args.video)
        if not row:
            print(f"No such video: {args.video}")
            return
        db.delete_video(conn, args.video)
    finally:
        conn.close()
    print(f"Deleted '{row['title']}' from the database.")
    if args.keep_files:
        print("Files kept on disk (--keep-files).")
        return
    for key in ("audio_path", "transcript_path"):
        path = row[key]
        if path and Path(path).exists():
            Path(path).unlink()
            print(f"deleted file: {path}")


def cmd_reset(cfg, args):
    conn = _open(cfg)
    try:
        row = db.get_video(conn, args.video)
        if not row:
            print(f"No such video: {args.video}")
            return
        for key in ("audio_path", "transcript_path"):
            path = row[key]
            if path and Path(path).exists():
                Path(path).unlink()
                print(f"deleted file: {path}")
        db.set_video(conn, args.video, status="new", auto=1, audio_path=None,
                     transcript_path=None, language=None, error=None)
        print(f"Reset '{row['title']}' - files removed, queued for re-download.")
    finally:
        conn.close()


def cmd_backlog(cfg, args):
    conn = _open(cfg)
    try:
        if not args.video:
            rows = db.get_videos(conn, backlog=True)
            if not rows:
                print("Backlog is empty.")
                return
            print(f"{len(rows)} backlog video(s) (not auto-downloaded):")
            for row in rows:
                print(f"  {row['published_at'] or '?'}  [{row['channel_name']}|"
                      f"{row['channel_genre']}] {row['title']}  ({row['video_id']})")
            print("Queue all:    python wr.py backlog all")
            print("Queue one:    python wr.py backlog <video_id>")
            return
        if args.video == "all":
            cur = conn.execute(
                "UPDATE videos SET auto = 1 WHERE auto = 0 AND status = 'new'"
            )
            conn.commit()
            print(f"Queued {cur.rowcount} backlog video(s) for download.")
            return
        if not db.get_video(conn, args.video):
            print(f"No such video: {args.video}")
            return
        db.set_video(conn, args.video, auto=1, status="new", error=None)
        print(f"Queued {args.video}. Download with: python wr.py download {args.video}")
    finally:
        conn.close()


def cmd_clean(cfg, args):
    conn = _open(cfg)
    try:
        known = {r[0] for r in conn.execute("SELECT video_id FROM videos")}
    finally:
        conn.close()
    in_use = orphans = 0
    for folder in (cfg.audio_dir, cfg.transcripts_dir):
        if not folder.exists():
            continue
        for f in sorted(folder.rglob("*")):
            if not f.is_file():
                continue
            if f.stem in known:
                in_use += 1
                continue
            orphans += 1
            if args.yes:
                f.unlink()
                print(f"deleted: {f}")
            else:
                print(f"orphan: {f}")
    print(f"{in_use} file(s) in use.")
    if args.yes:
        print(f"Deleted {orphans} orphan file(s).")
    else:
        print(f"{orphans} orphan file(s) found (not in the database). "
              f"Re-run with --yes to delete them.")


def cmd_run(cfg, args):
    result = pipeline.run_all(cfg, retry=args.retry)
    log.info(
        "run finished: %d new, %d downloaded, %d transcribed, %d failed",
        result["new_videos"],
        result["downloaded"],
        result["transcribed"],
        result["failed"],
    )


def cmd_export(cfg, args):
    conn = _open(cfg)
    try:
        if args.video == "all":
            rows = db.get_videos(conn, status=args.status, genre=args.genre,
                                 channel=args.channel)
        else:
            row = db.get_video(conn, args.video)
            rows = [row] if row else []
        if not rows:
            print(f"No matching videos to export (video={args.video}, "
                  f"status={args.status}, genre={args.genre})")
            return
        out_dir = Path(args.out or cfg.exports_dir)
        # exporting everything without a genre filter: organize into genre folders
        by_genre = args.video == "all" and not args.genre
        for row in rows:
            src = row["transcript_path"]
            if not src or not Path(src).exists():
                print(f"skip (no transcript): {row['title']}")
                continue
            name = _slugify(row["title"])
            if args.video == "all":
                name = f"{name}__{row['video_id']}"
            dest_dir = out_dir / (row["channel_genre"] or "general") if by_genre else out_dir
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{name}.txt"
            shutil.copyfile(src, dest)
            print(f"exported: {dest}")
    finally:
        conn.close()


def cmd_produce(cfg, args):
    from . import producer

    if args.plan:
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        try:
            plan = producer.build_plan(cfg, conn)
        finally:
            conn.close()
        for entry in plan:
            name = entry.get("own_channel", "-")
            print(f"[{entry['action']:5}] {name}: {entry['detail']}")
        return 0
    result = producer.run(cfg, log=print)
    for item in result["created"]:
        print(f"created #{item['pid']}: {item['title']} "
              f"({item['own_channel']})")
    for entry in result.get("resumed", []):
        print(f"resumed #{entry['production_id']}: {entry['title']} "
              f"-> {entry.get('result', '?')}")
    for entry in result["skipped"]:
        print(f"skipped {entry.get('own_channel', '-')}: {entry['detail']}")
    print(f"produce: {len(result['created'])} created, "
          f"{len(result.get('resumed', []))} resumed, "
          f"{len(result['skipped'])} skipped, result={result['result']}")
    return 0 if result["result"] == "ok" else 1


def cmd_serve(cfg, args):
    from .webapp import create_app

    app = create_app(cfg)
    print(f"WhisperRadar dashboard: http://{args.host}:{args.port}")
    try:
        from waitress import serve

        serve(app, host=args.host, port=args.port, threads=8)
    except ImportError:
        logging.getLogger("whisperradar").warning(
            "waitress not installed - using the Flask development server"
        )
        app.run(host=args.host, port=args.port, debug=False)


def cmd_test(cfg, args):
    """Run the test suite: this repo's unit tests, plus the Renderly driver
    suite when --renderly DIR is given (that repo's test.bat)."""
    import subprocess
    import unittest

    root = Path(__file__).resolve().parents[1]
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestLoader().discover(str(root / "tests"),
                                       top_level_dir=str(root)))
    ok = result.wasSuccessful()
    if getattr(args, "renderly", None):
        bat = Path(args.renderly) / "test.bat"
        if bat.exists():
            print(f"\nRunning the Renderly suite: {bat}")
            ok = subprocess.call(["cmd", "/c", str(bat)]) == 0 and ok
        else:
            print(f"\nNo test.bat found in {args.renderly}")
            ok = False
    raise SystemExit(0 if ok else 1)


def build_parser() -> argparse.ArgumentParser:
    # lets `--config` be given before or after the subcommand
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(
        prog="wr",
        description="WhisperRadar: watch YouTube channels and transcribe new uploads.",
    )
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--version", action="version", version=f"whisperradar {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create database and folders", parents=[common])
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("add", help="add a channel (URL, @handle or UC... id)",
                       parents=[common])
    p.add_argument("channel")
    p.add_argument("--name", default=None, help="display name")
    p.add_argument("--similar", dest="kind", action="store_const",
                   const="similar", default="primary", help="mark as similar channel")
    p.add_argument("--genre", default="general",
                   help="category tag, e.g. tech, science, finance")
    p.set_defaults(func=cmd_add)
    p = sub.add_parser("remove", help="remove a channel by name or id",
                       parents=[common])
    p.add_argument("channel")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("edit", help="edit a channel's genre, name, kind or state",
                       parents=[common])
    p.add_argument("channel", help="channel name or id")
    p.add_argument("--genre", default=None, help="new genre tag")
    p.add_argument("--name", default=None, help="new display name")
    p.add_argument("--kind", choices=["primary", "similar"], default=None,
                   help="auto-download new uploads (primary) or "
                        "backlog-only (similar)")
    p.add_argument("--pause", action="store_true", help="pause the channel")
    p.add_argument("--activate", action="store_true", help="re-activate the channel")
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("channels", help="list channels", parents=[common])
    p.set_defaults(func=cmd_channels)

    p = sub.add_parser("videos", help="list known videos", parents=[common])
    p.add_argument("--status", default=None,
                   help="filter: new, downloaded, transcribed, error, skipped")
    p.add_argument("--genre", default=None, help="filter by channel genre")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_videos)

    p = sub.add_parser("watch", help="refresh channel feeds only", parents=[common])
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("download", help="download audio for pending videos", parents=[common])
    p.add_argument("video", nargs="?", default="all",
                   help="video id to download now, or 'all' (default)")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("transcribe", help="transcribe downloaded videos", parents=[common])
    p.add_argument("video", nargs="?", default="all",
                   help="video id to (re-)transcribe now, or 'all' (default)")
    p.set_defaults(func=cmd_transcribe)

    p = sub.add_parser("backlog", help="list or queue old (backlog) videos", parents=[common])
    p.add_argument("video", nargs="?", default=None,
                   help="video id or 'all' to queue for download; "
                        "omit to list the backlog")
    p.set_defaults(func=cmd_backlog)

    p = sub.add_parser("clean", help="report/delete audio+transcript files "
                                     "not tracked in the database")
    p.add_argument("--yes", action="store_true", help="actually delete orphans")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("run", help="full pipeline: watch + download + transcribe", parents=[common])
    p.add_argument("--retry", action="store_true", help="re-queue errored videos")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("export", help="copy transcripts out of data/", parents=[common])
    p.add_argument("video", help="video id or 'all'")
    p.add_argument("--status", default="transcribed",
                   help="status filter when exporting 'all'")
    p.add_argument("--genre", default=None,
                   help="only export channels of this genre; "
                        "'all' without --genre exports into per-genre folders")
    p.add_argument("--channel", default=None,
                   help="only export videos from this channel (id or name)")
    p.add_argument("--out", default=None, help="destination directory")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("delete", help="delete a video from the library", parents=[common])
    p.add_argument("video", help="video id")
    p.add_argument("--keep-files", action="store_true",
                   help="remove from database but keep audio/transcript files")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("reset", help="delete a video's files and re-queue it", parents=[common])
    p.add_argument("video", help="video id")
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("produce", help="auto-run: create + run a production "
                                       "per own channel (see Settings)",
                       parents=[common])
    p.add_argument("--plan", action="store_true",
                   help="dry run: show what would be created, spend nothing")
    p.set_defaults(func=cmd_produce)

    p = sub.add_parser("serve", help="start the local web dashboard", parents=[common])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8540)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("test", parents=[common],
                       help="run the test suite (unit tests; "
                            "--renderly DIR also runs the driver suite)")
    p.add_argument("--renderly", default=None, metavar="DIR",
                   help="also run DIR\\test.bat (the Renderly suite)")
    p.set_defaults(func=cmd_test)

    return parser


def main(argv=None) -> int:
    _utf8_console()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    args.func(cfg, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
