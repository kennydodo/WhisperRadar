"""How our published videos did: the YouTube link, views at 24 hours / 7 days
/ 28 days, and the comparison with the channel's own norm.

Views come from the YouTube API when a key is set, otherwise from yt-dlp (one
video at a time). Each horizon is recorded once, with how many hours after
publishing it was really read (a late read is flagged, not hidden).
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import statistics
from pathlib import Path

HORIZONS = (("24h", 24), ("7d", 168), ("28d", 672))
LATE_FACTOR = 1.5          # read later than 1.5x the horizon = "late"
MIN_PEERS = 3              # own videos needed to call a "norm"
_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_URL = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})")


def parse_video_id(text) -> str | None:
    text = str(text or "").strip()
    if _ID.match(text):
        return text
    m = _URL.search(text)
    return m.group(1) if m else None


def _published(prod) -> _dt.datetime | None:
    """productions.published_at is SQLite localtime text; as an aware UTC
    datetime."""
    raw = prod["published_at"]
    if not raw:
        return None
    try:
        naive = _dt.datetime.fromisoformat(str(raw).replace("Z", ""))
    except ValueError:
        return None
    if naive.tzinfo is None:
        return naive.astimezone(_dt.timezone.utc)   # local -> UTC
    return naive.astimezone(_dt.timezone.utc)


def set_link(cfg, conn, pid: int, text: str) -> str | None:
    """Store the YouTube video id for a production; None when the text has
    no id. Also snapshots the packaging that shipped (for learning)."""
    from . import db, packaging, plan as packplan, studio, thumbnails
    vid = parse_video_id(text)
    if not vid:
        return None
    db.update_production(conn, pid, youtube_video_id=vid)
    pdir = studio.prod_dir(cfg, pid)
    try:
        kit = packaging.load_kit(pdir)
        th = thumbnails.load_thumbs(pdir)
        chosen = next((c for c in th["concepts"]
                       if c["id"] == th.get("chosen")), None)
        plan = packplan.load_plan(pdir)
        prod = db.get_production(conn, pid)
        snap = {"youtube_video_id": vid, "title": prod["title"],
                "keyword": kit.get("keyword") or plan.get("keyword") or "",
                "thumb_layout": chosen["layout"] if chosen else "",
                "thumb_text": chosen["text"] if chosen else "",
                "source_video_id": prod["source_video_id"] or ""}
        (pdir).mkdir(parents=True, exist_ok=True)
        (pdir / "published.json").write_text(
            json.dumps(snap, indent=1, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001 - the link matters more than the snapshot
        pass
    return vid


def load_snapshot(cfg, pid: int) -> dict:
    from . import studio
    try:
        return json.loads((studio.prod_dir(cfg, pid) / "published.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def recorded(conn, pid: int) -> dict:
    return {r["horizon"]: r for r in conn.execute(
        "SELECT * FROM prod_results WHERE production_id = ?", (pid,))}


def due(conn, now: _dt.datetime | None = None) -> list[tuple[int, str, str]]:
    """[(production id, horizon, youtube id)] ready to be read."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    out = []
    for p in conn.execute(
            "SELECT * FROM productions WHERE status = 'published'"
            " AND youtube_video_id IS NOT NULL AND youtube_video_id != ''"):
        when = _published(p)
        if when is None:
            continue
        have = recorded(conn, p["id"])
        age_h = (now - when).total_seconds() / 3600.0
        # only the newest horizon that has passed is read: reading 24h and
        # 7d at the same late moment would record the same number twice
        last_done = max((hours for h, hours in HORIZONS if h in have),
                        default=0)
        ready = [h for h, hours in HORIZONS
                 if age_h >= hours and hours > last_done and h not in have]
        if ready:
            out.append((p["id"], ready[-1], p["youtube_video_id"]))
    return out


def fetch_views(cfg, ids: list[str], fetcher=None) -> dict[str, int]:
    """{video_id: views}. `fetcher(ids)` is replaceable for tests."""
    if fetcher:
        return fetcher(ids)
    from . import youtube_api
    if youtube_api.key_source(cfg):
        try:
            got = youtube_api.video_details(youtube_api.Client(cfg), ids)
            return {i: d["view_count"] for i, d in got.items()
                    if d["view_count"] is not None}
        except youtube_api.ApiError:
            pass                        # fall back to yt-dlp below
    out = {}
    try:
        import yt_dlp
    except ImportError:
        return out
    for i in ids:
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True,
                                   "no_warnings": True}) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={i}",
                                        download=False)
            if info and info.get("view_count") is not None:
                out[i] = int(info["view_count"])
        except Exception:  # noqa: BLE001 - one video failing is not fatal
            continue
    return out


def update(cfg, conn, now: _dt.datetime | None = None, fetcher=None) -> int:
    """Record every result that is due. Returns how many were stored."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    todo = due(conn, now)
    if not todo:
        return 0
    views = fetch_views(cfg, sorted({t[2] for t in todo}), fetcher)
    stored = 0
    for pid, horizon, vid in todo:
        if vid not in views:
            continue
        prod = conn.execute("SELECT * FROM productions WHERE id = ?",
                            (pid,)).fetchone()
        hours = (now - _published(prod)).total_seconds() / 3600.0
        conn.execute(
            "INSERT OR IGNORE INTO prod_results (production_id, horizon,"
            " taken_at, hours_after, views) VALUES (?, ?, ?, ?, ?)",
            (pid, horizon, now.isoformat(timespec="seconds"),
             round(hours, 1), int(views[vid])))
        stored += 1
    conn.commit()
    return stored


def channel_norm(conn, own_channel_id, horizon: str, exclude_pid: int
                 ) -> float | None:
    """Median views at `horizon` of the channel's OTHER published videos
    (needs MIN_PEERS), else None."""
    rows = conn.execute(
        "SELECT r.views FROM prod_results r JOIN productions p"
        " ON p.id = r.production_id WHERE r.horizon = ?"
        " AND p.own_channel_id IS ? AND p.id != ?",
        (horizon, own_channel_id, exclude_pid)).fetchall()
    vals = [r[0] for r in rows]
    return float(statistics.median(vals)) if len(vals) >= MIN_PEERS else None


def report(conn, pid: int) -> list[dict]:
    """One row per recorded horizon: views, lateness, the norm and the
    multiplier against it."""
    prod = conn.execute("SELECT * FROM productions WHERE id = ?",
                        (pid,)).fetchone()
    if not prod:
        return []
    have = recorded(conn, pid)
    out = []
    for h, hours in HORIZONS:
        r = have.get(h)
        if not r:
            continue
        norm = channel_norm(conn, prod["own_channel_id"], h, pid)
        out.append({"horizon": h, "views": r["views"],
                    "hours_after": r["hours_after"],
                    "late": bool(r["hours_after"]
                                 and r["hours_after"] > hours * LATE_FACTOR),
                    "norm": norm,
                    "multiplier": (r["views"] / norm) if norm else None})
    return out
