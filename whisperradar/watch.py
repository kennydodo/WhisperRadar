"""Channel feed reading and resolution.

Uploads are detected via the official YouTube RSS feed, which lists the
latest 15 videos per channel and needs no API key.
"""

import datetime
import re
import urllib.request
import xml.etree.ElementTree as ET

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}

CHANNEL_ID_RE = re.compile(r"^UC[\w-]{22}$")


def fetch_channel_videos(channel_id: str, timeout: int = 30) -> list[dict]:
    req = urllib.request.Request(FEED_URL.format(channel_id=channel_id), headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        root = ET.fromstring(resp.read())

    videos = []
    for entry in root.findall("a:entry", _NS):
        video_id = entry.findtext("yt:videoId", namespaces=_NS)
        if not video_id:
            continue
        videos.append(
            {
                "video_id": video_id,
                "title": (entry.findtext("a:title", default="", namespaces=_NS) or "").strip(),
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "published_at": entry.findtext("a:published", namespaces=_NS),
            }
        )
    return videos


def _published_at(entry: dict) -> str | None:
    """Normalize yt-dlp's upload_date (YYYYMMDD) / timestamp to the ISO-ish
    string the RSS path stores."""
    date = str(entry.get("upload_date") or "")
    if len(date) == 8 and date.isdigit():
        return (f"{date[0:4]}-{date[4:6]}-{date[6:8]}T00:00:00+00:00")
    ts = entry.get("timestamp")
    if ts:
        try:
            return datetime.datetime.fromtimestamp(
                int(ts), datetime.timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def fetch_channel_videos_full(
    channel_id: str, limit: int | None = None,
    cookies_from_browser: str | None = None,
) -> list[dict]:
    """Enumerate a channel's FULL uploads history with yt-dlp.

    The official RSS feed (`fetch_channel_videos`) only lists the latest 15
    videos, so it can never see a channel's older uploads. This walks the
    channel's `/videos` tab instead (flat extraction, no per-video requests)
    and returns the same {video_id, title, url, published_at} shape, newest
    first. `limit` caps the number of entries (None = the whole history).
    """
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "socket_timeout": 30,
    }
    if limit:
        opts["playlistend"] = int(limit)
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)

    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    videos = []
    for entry in (info.get("entries") or []):
        if not isinstance(entry, dict):
            continue
        video_id = entry.get("id")
        if not video_id:
            continue
        videos.append(
            {
                "video_id": video_id,
                "title": (entry.get("title") or "").strip(),
                "url": entry.get("url")
                or f"https://www.youtube.com/watch?v={video_id}",
                "published_at": _published_at(entry),
                "view_count": entry.get("view_count"),
            }
        )
    return videos


def resolve_channel(url_or_handle: str) -> tuple[str, str]:
    """Resolve a channel URL / @handle to (channel_id, name)."""
    try:
        import yt_dlp

        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "socket_timeout": 30,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url_or_handle, download=False)
        channel_id = info.get("channel_id") or info.get("uploader_id") or ""
        name = info.get("channel") or info.get("uploader") or ""
        if CHANNEL_ID_RE.match(channel_id):
            return channel_id, name
    except Exception:
        pass

    # Fallback: scrape the channel page for its externalId
    page_url = url_or_handle
    if page_url.startswith("@"):
        page_url = f"https://www.youtube.com/{page_url}"
    elif "/channel/" not in page_url and not page_url.startswith("http"):
        page_url = f"https://www.youtube.com/@{page_url}"
    req = urllib.request.Request(page_url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", "ignore")
    match = re.search(r'"(?:channelId|externalId)":"(UC[\w-]{22})"', html)
    if not match:
        raise ValueError(f"Could not resolve channel id from: {url_or_handle}")
    title = re.search(r'<meta property="og:title" content="([^"]+)"', html)
    return match.group(1), (title.group(1) if title else "")
