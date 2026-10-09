"""YouTube Data API v3 layer - an ADDITION to the yt-dlp scraping, never a
replacement: the API fills what scraping cannot (publish dates, exact
durations) and finds new channels; yt-dlp keeps working with no key.

The key is LOCAL ONLY: the WR_YOUTUBE_API_KEY environment variable, or the
file data/youtube_api_key.txt (data/ is gitignored). It is never logged,
echoed, put in an error message or committed. A quota guard stops before the
free 10,000 units/day are spent (search.list alone costs 100 each).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://www.googleapis.com/youtube/v3/"
ENV_KEY = "WR_YOUTUBE_API_KEY"
KEY_FILE = "youtube_api_key.txt"
QUOTA_FILE = "youtube_quota.json"
DAILY_LIMIT = 10000
SAFETY_MARGIN = 500            # stop this far below the real limit
COSTS = {"videos": 1, "channels": 1, "playlistItems": 1, "search": 100}


class ApiError(RuntimeError):
    """A readable failure. Never contains the key."""


class NoKey(ApiError):
    pass


class QuotaGuard(ApiError):
    pass


# ---- the key --------------------------------------------------------------

def key_path(cfg) -> Path:
    return Path(cfg.db_path).parent / KEY_FILE


def get_key(cfg) -> str:
    key = (os.environ.get(ENV_KEY) or "").strip()
    if not key:
        try:
            key = key_path(cfg).read_text(encoding="utf-8").strip()
        except OSError:
            key = ""
    return key


def key_source(cfg) -> str:
    """"env", "file" or "" - where the key is, never the key itself."""
    if (os.environ.get(ENV_KEY) or "").strip():
        return "env"
    try:
        if key_path(cfg).read_text(encoding="utf-8").strip():
            return "file"
    except OSError:
        pass
    return ""


def save_key(cfg, key: str) -> None:
    key = (key or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{20,80}", key):
        raise ApiError("That does not look like a YouTube API key.")
    p = key_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(key, encoding="utf-8")


def clear_key(cfg) -> None:
    try:
        key_path(cfg).write_text("", encoding="utf-8")
    except OSError:
        pass


# ---- quota ------------------------------------------------------------------

def _today() -> str:
    """The quota day: YouTube resets at midnight Pacific time."""
    try:
        from zoneinfo import ZoneInfo
        now = _dt.datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:  # noqa: BLE001
        now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=-8)))
    return now.date().isoformat()


def quota_used(cfg) -> int:
    try:
        data = json.loads((Path(cfg.db_path).parent / QUOTA_FILE)
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return int(data.get("used", 0)) if data.get("day") == _today() else 0


def _spend(cfg, units: int) -> None:
    used = quota_used(cfg) + units
    p = Path(cfg.db_path).parent / QUOTA_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"day": _today(), "used": used}),
                 encoding="utf-8")


def quota_left(cfg) -> int:
    return max(0, DAILY_LIMIT - SAFETY_MARGIN - quota_used(cfg))


# ---- requests ------------------------------------------------------------------

def _default_http(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "WhisperRadar"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            msg = json.loads(exc.read().decode("utf-8"))["error"]["message"]
        except Exception:  # noqa: BLE001
            msg = f"HTTP {exc.code}"
        raise ApiError(f"YouTube API: {msg}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ApiError(f"YouTube API unreachable: {type(exc).__name__}") \
            from None


class Client:
    """One API session. `http(url) -> dict` is replaceable for tests."""

    def __init__(self, cfg, http=None):
        self.cfg = cfg
        self.http = http or _default_http
        self.key = get_key(cfg)
        if not self.key:
            raise NoKey("No YouTube API key set. Add it in Settings (it "
                        "stays on this computer).")

    def call(self, resource: str, **params) -> dict:
        cost = COSTS.get(resource, 1)
        if cost > quota_left(self.cfg):
            raise QuotaGuard(
                f"YouTube quota guard: {quota_used(self.cfg)} of "
                f"{DAILY_LIMIT} units used today - stopping before the "
                "limit. It resets at midnight Pacific time.")
        params = {k: v for k, v in params.items() if v not in (None, "")}
        url = (API + resource + "?" + urllib.parse.urlencode(params)
               + "&key=" + self.key)
        try:
            data = self.http(url)
        except ApiError as exc:
            raise ApiError(str(exc).replace(self.key, "***")) from None
        _spend(self.cfg, cost)
        return data


# ---- parsing ------------------------------------------------------------------------

_DUR = re.compile(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def parse_duration(text) -> int | None:
    """ISO-8601 duration (PT1H2M3S) to seconds."""
    m = _DUR.match(str(text or ""))
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _video(item: dict) -> dict:
    sn, st = item.get("snippet", {}), item.get("statistics", {})
    views = st.get("viewCount")
    return {"video_id": item.get("id"), "title": sn.get("title", ""),
            "channel_id": sn.get("channelId", ""),
            "published_at": sn.get("publishedAt"),
            "duration": parse_duration(
                item.get("contentDetails", {}).get("duration")),
            "view_count": int(views) if views is not None else None}


# ---- calls ------------------------------------------------------------------------------

def video_details(client: Client, ids: list[str]) -> dict[str, dict]:
    """{video_id: details} for up to any number of ids (50 per request,
    1 unit each)."""
    out = {}
    ids = [i for i in dict.fromkeys(ids) if i]
    for n in range(0, len(ids), 50):
        data = client.call("videos", part="snippet,contentDetails,statistics",
                           id=",".join(ids[n:n + 50]), maxResults=50)
        for item in data.get("items", []):
            v = _video(item)
            if v["video_id"]:
                out[v["video_id"]] = v
    return out


def channel_uploads(client: Client, channel_id: str, limit: int = 50
                    ) -> list[dict]:
    """The channel's newest uploads with dates, durations and views. A
    channel's uploads playlist is its id with UC replaced by UU (1 unit a
    page), then video_details fills the rest."""
    if not channel_id.startswith("UC"):
        raise ApiError("Not a channel id (UC...).")
    ids, token = [], None
    while len(ids) < limit:
        data = client.call("playlistItems", part="contentDetails",
                           playlistId="UU" + channel_id[2:],
                           maxResults=min(50, limit - len(ids)),
                           pageToken=token)
        ids += [i["contentDetails"]["videoId"] for i in data.get("items", [])
                if i.get("contentDetails", {}).get("videoId")]
        token = data.get("nextPageToken")
        if not token:
            break
    details = video_details(client, ids)
    return [details[i] for i in ids if i in details]


def fill_missing(cfg, conn, client: Client | None = None,
                 limit: int = 500) -> dict:
    """Give stored videos their publish date / duration / fresh views from
    the API. Newest-first by view count (the outliers matter most). Returns
    {looked_up, dated, units}."""
    from . import db
    client = client or Client(cfg)
    rows = conn.execute(
        "SELECT video_id FROM videos WHERE published_at IS NULL"
        " OR duration IS NULL ORDER BY view_count DESC LIMIT ?",
        (limit,)).fetchall()
    ids = [r[0] for r in rows]
    before = quota_used(cfg)
    details = video_details(client, ids)
    dated = 0
    pairs = []
    for vid, d in details.items():
        conn.execute(
            "UPDATE videos SET published_at = COALESCE(published_at, ?),"
            " duration = COALESCE(duration, ?),"
            " view_count = COALESCE(?, view_count) WHERE video_id = ?",
            (d["published_at"], d["duration"], d["view_count"], vid))
        dated += 1 if d["published_at"] else 0
        pairs.append((vid, d["view_count"]))
    conn.commit()
    db.record_snapshots(conn, pairs)
    return {"looked_up": len(ids), "dated": dated,
            "units": quota_used(cfg) - before}


def channel_stats(client: Client, ids: list[str]) -> dict[str, dict]:
    """{channel_id: {subscribers, views, video_count}} - 1 unit per request
    of up to 50 ids. Hidden subscriber counts stay None (honest, not 0)."""
    out = {}
    ids = [i for i in dict.fromkeys(ids) if i]
    for n in range(0, len(ids), 50):
        data = client.call("channels", part="statistics",
                           id=",".join(ids[n:n + 50]))
        for item in data.get("items", []):
            st = item.get("statistics") or {}

            def _int(key):
                v = st.get(key)
                return int(v) if v is not None else None

            subs = None if st.get("hiddenSubscriberCount") \
                else _int("subscriberCount")
            out[item.get("id")] = {"subscribers": subs,
                                   "views": _int("viewCount"),
                                   "video_count": _int("videoCount")}
    return out


def trending(client: Client, query: str, days: int = 7,
             max_results: int = 25, now=None) -> list[dict]:
    """The most-viewed videos for `query` published in the last `days` days,
    with views per hour since upload. Costs 100 (search) + 1 (details)."""
    import datetime as _dt
    now = now or _dt.datetime.now(_dt.timezone.utc)
    days = max(1, min(int(days), 60))
    after = (now - _dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = client.call("search", part="snippet", q=query, type="video",
                       order="viewCount", publishedAfter=after,
                       maxResults=min(50, max(1, int(max_results))),
                       relevanceLanguage="en")
    names = {}
    for item in data.get("items", []):
        vid = item.get("id", {}).get("videoId")
        if vid:
            names[vid] = item.get("snippet", {}).get("channelTitle", "")
    details = video_details(client, list(names))
    out = []
    for vid, d in details.items():
        when = None
        if d.get("published_at"):
            try:
                when = _dt.datetime.fromisoformat(
                    d["published_at"].replace("Z", "+00:00"))
            except ValueError:
                when = None
        hours = (max((now - when).total_seconds() / 3600.0, 1.0)
                 if when else None)
        views = d.get("view_count")
        out.append({"video_id": vid, "title": d["title"],
                    "channel_id": d["channel_id"],
                    "channel_name": names.get(vid, ""),
                    "published_at": d.get("published_at"),
                    "duration": d.get("duration"), "views": views,
                    "vph": (round(views / hours, 1)
                            if views is not None and hours else None),
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "thumb": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"})
    out.sort(key=lambda v: -(v["vph"] or 0))
    return out


def discover_channels(client: Client, query: str, watched: set[str],
                      max_results: int = 25, min_subs: int = 0,
                      max_subs: int | None = None) -> list[dict]:
    """New channels for a topic: the top videos for `query`, their channels
    minus those already watched, with subscriber counts. Costs 100 + 1."""
    data = client.call("search", part="snippet", q=query, type="video",
                       order="viewCount", maxResults=min(50, max_results * 2),
                       relevanceLanguage="en")
    found: dict[str, dict] = {}
    for item in data.get("items", []):
        sn = item.get("snippet", {})
        cid = sn.get("channelId")
        if not cid or cid in watched:
            continue
        c = found.setdefault(cid, {"channel_id": cid,
                                   "name": sn.get("channelTitle", ""),
                                   "sample": []})
        c["sample"].append({"video_id": item.get("id", {}).get("videoId"),
                            "title": sn.get("title", "")})
    out = []
    ids = list(found)
    for n in range(0, len(ids), 50):
        stats = client.call("channels", part="statistics,snippet",
                            id=",".join(ids[n:n + 50]), maxResults=50)
        for item in stats.get("items", []):
            st = item.get("statistics", {})
            c = found.get(item.get("id"))
            if not c:
                continue
            subs = (None if st.get("hiddenSubscriberCount")
                    else int(st.get("subscriberCount", 0) or 0))
            c.update({"subscribers": subs,
                      "videos": int(st.get("videoCount", 0) or 0),
                      "views": int(st.get("viewCount", 0) or 0),
                      "about": (item.get("snippet", {})
                                .get("description") or "")[:160]})
            out.append(c)
    if min_subs:
        out = [c for c in out if (c["subscribers"] or 0) >= min_subs]
    if max_subs is not None:
        out = [c for c in out if c["subscribers"] is None
               or c["subscribers"] <= max_subs]
    # small channels with big views are the interesting ones
    out.sort(key=lambda c: -(c["views"] / max(c["subscribers"] or 1, 1)))
    return out[:max_results]
