"""Outlier research: which videos beat their own channel's norm.

A video's multiplier is its views divided by the channel's typical views (a
MEDIAN, so one viral hit cannot drag the norm up). The baseline is the median
of the channel's previous `window` uploads when the video and enough earlier
uploads have a publish date; otherwise the median of all the channel's OTHER
videos (a channel's yt-dlp history listing has no dates - the YouTube API
fills them in later).

Pure functions over rows, so they are tested without a database or a network.
"""
from __future__ import annotations

import datetime as _dt
import statistics
from typing import Iterable

WINDOW = 30                # previous uploads that define a channel's norm
MIN_BASELINE = 5           # fewer comparison videos than this = no verdict
SHORT_SECONDS = 61         # <= this is a Short (when the duration is known)

SORTS = ("multiplier", "views", "vpd", "recent")


def parse_date(value) -> _dt.datetime | None:
    """published_at as an aware UTC datetime, or None when missing/odd."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        when = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return when.astimezone(_dt.timezone.utc)


def _median(values: list[int]) -> float:
    return float(statistics.median(values))


def baseline_for(video: dict, siblings: list[dict], window: int = WINDOW,
                 min_baseline: int = MIN_BASELINE) -> tuple[float | None, str]:
    """(typical views, how it was worked out) for one video among the other
    videos of its channel. (None, "") when there is too little to compare."""
    others = [s for s in siblings
              if s["video_id"] != video["video_id"] and s["views"] is not None]
    when = video.get("when")
    if when is not None:
        earlier = sorted((s for s in others
                          if s.get("when") is not None and s["when"] < when),
                         key=lambda s: s["when"], reverse=True)[:window]
        if len(earlier) >= min_baseline:
            return _median([s["views"] for s in earlier]), "previous uploads"
    if len(others) >= min_baseline:
        return _median([s["views"] for s in others]), "all other videos"
    return None, ""


def build(rows: Iterable, now: _dt.datetime | None = None, window: int = WINDOW,
          min_baseline: int = MIN_BASELINE) -> list[dict]:
    """Every video that has a view count and enough channel context, with its
    baseline, multiplier and views per day. `rows` need: video_id, channel_id,
    channel_name, genre, title, url, published_at, view_count, duration,
    status."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    items = []
    for r in rows:
        get = (r.get if isinstance(r, dict) else (lambda k, _r=r: _r[k]))
        views = get("view_count")
        items.append({
            "video_id": get("video_id"), "channel_id": get("channel_id"),
            "channel_name": get("channel_name"), "genre": get("genre"),
            "title": get("title"), "url": get("url"),
            "published_at": get("published_at"),
            "when": parse_date(get("published_at")),
            "views": int(views) if views is not None else None,
            "duration": get("duration"), "status": get("status"),
        })
    by_channel: dict[str, list[dict]] = {}
    for it in items:
        by_channel.setdefault(it["channel_id"], []).append(it)
    out = []
    for it in items:
        if it["views"] is None:
            continue
        base, how = baseline_for(it, by_channel[it["channel_id"]], window,
                                 min_baseline)
        if not base:
            continue
        age = ((now - it["when"]).total_seconds() / 86400.0
               if it["when"] else None)
        dur = it["duration"]
        out.append({
            **{k: it[k] for k in ("video_id", "channel_id", "channel_name",
                                  "genre", "title", "url", "published_at",
                                  "views", "duration", "status")},
            "baseline": base, "baseline_how": how,
            "multiplier": it["views"] / base,
            "age_days": age,
            "vpd": (it["views"] / max(age, 1.0)) if age is not None else None,
            "is_short": bool(dur is not None and dur <= SHORT_SECONDS),
            "thumb": f"https://i.ytimg.com/vi/{it['video_id']}/mqdefault.jpg",
        })
    return out


def filter_sort(items: list[dict], *, min_multiplier: float = 3.0,
                max_age_days: float | None = None, channel_id: str = "",
                genre: str = "", hide_shorts: bool = True, q: str = "",
                min_views: int = 0, sort: str = "multiplier",
                limit: int = 200) -> tuple[list[dict], int]:
    """(the rows to show, how many matched before the limit). A window on the
    age drops videos with no publish date - they cannot be placed."""
    q = (q or "").strip().lower()
    keep = []
    for it in items:
        if it["multiplier"] < min_multiplier or it["views"] < min_views:
            continue
        if channel_id and it["channel_id"] != channel_id:
            continue
        if genre and (it["genre"] or "") != genre:
            continue
        if hide_shorts and it["is_short"]:
            continue
        if max_age_days is not None and (
                it["age_days"] is None or it["age_days"] > max_age_days):
            continue
        if q and q not in (it["title"] or "").lower():
            continue
        keep.append(it)
    if sort == "views":
        keep.sort(key=lambda i: i["views"], reverse=True)
    elif sort == "vpd":
        keep.sort(key=lambda i: (i["vpd"] is None, -(i["vpd"] or 0)))
    elif sort == "recent":
        keep.sort(key=lambda i: (i["age_days"] is None, i["age_days"] or 0))
    else:
        keep.sort(key=lambda i: i["multiplier"], reverse=True)
    return keep[:limit], len(keep)


def fmt_views(n) -> str:
    if n is None:
        return "-"
    n = float(n)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            val = n / div
            return f"{val:.1f}".rstrip("0").rstrip(".") + suffix
    return str(int(n))


def fmt_age(days) -> str:
    if days is None:
        return "?"
    if days < 1:
        return "today"
    if days < 60:
        return f"{int(days)}d"
    if days < 700:
        return f"{int(days // 30)}mo"
    return f"{days / 365:.1f}y"
