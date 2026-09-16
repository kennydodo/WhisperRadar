"""Channel feed reading and resolution.

Uploads are detected via the official YouTube RSS feed, which lists the
latest 15 videos per channel and needs no API key.
"""

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
