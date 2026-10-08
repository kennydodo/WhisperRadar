"""Visual style guide + character/environment bible for a WATCHED channel.

Manual only ("Get style & bible" on Watched channels). It samples a handful of
real video frames (not thumbnails - those do not always show what the video
looks like) from the channel's most viewed videos, sends them with a few titles
and transcript excerpts to an image-capable web-chat LLM and keeps the answer
beside the channel: data/channel_look/<channel_id>/{style.md,bible.md,frames/}.

The result stays with the watched channel. Nothing is copied into your own
channels - copy it by hand from the page when you want to try it.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from . import db

MAX_FRAMES = 10
MIN_FRAMES = 5
DEFAULT_FRAMES = 8
MAX_VIDEOS = 5
EXCERPT_CHARS = 700
STYLE_MARK = "=== STYLE GUIDE ==="
BIBLE_MARK = "=== BIBLE ==="


def look_dir(cfg, channel_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", channel_id)
    return Path(cfg.db_path).parent / "channel_look" / safe


def load(cfg, channel_id: str) -> dict | None:
    """What was saved for this channel, or None."""
    d = look_dir(cfg, channel_id)
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for name in ("style", "bible"):
        try:
            meta[name] = (d / f"{name}.md").read_text(encoding="utf-8")
        except OSError:
            meta[name] = ""
    meta["files"] = sorted(p.name for p in (d / "frames").glob("*.jpg"))
    return meta


def pick_videos(conn, channel_id: str, limit: int = MAX_VIDEOS) -> list[dict]:
    """Most viewed first (they show the look that works), newest as a tie-break
    and as the fallback when no view counts are stored."""
    rows = conn.execute(
        "SELECT * FROM videos WHERE channel_id = ? "
        "ORDER BY COALESCE(view_count, 0) DESC, published_at DESC LIMIT ?",
        (channel_id, limit)).fetchall()
    return [dict(r) for r in rows]


def frame_times(duration, per_video: int) -> list[float]:
    """Spread the picks over the middle of the video (skip intro/outro)."""
    d = float(duration or 0)
    if d <= 0:
        return [30.0 * (i + 1) for i in range(per_video)]
    return [d * (0.12 + 0.76 * (i + 0.5) / per_video)
            for i in range(per_video)]


def split_counts(n_frames: int, n_videos: int) -> list[int]:
    n_videos = max(1, min(n_videos, n_frames))
    base, extra = divmod(n_frames, n_videos)
    return [base + (1 if i < extra else 0) for i in range(n_videos)]


def download_low_res(url: str, out_dir: Path) -> tuple[Path, float | None]:
    """A small copy of the video (<=480p, no audio needed) for frame grabbing."""
    import yt_dlp
    opts = {"format": "bestvideo[height<=480]/best[height<=480]/worst",
            "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": 30, "retries": 3}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
    return path, info.get("duration")


def grab_frame(video: Path, at: float, out: Path) -> bool:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{at:.2f}",
           "-i", str(video), "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2",
           "-q:v", "3", str(out)]
    try:
        subprocess.run(cmd, check=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.exists() and out.stat().st_size > 0


def collect_frames(videos: list[dict], n_frames: int, dest: Path,
                   log: Callable[[str], None],
                   download=download_low_res, grab=grab_frame) -> list[dict]:
    """Frames named f01.jpg ...; returns [{file, video, title}]."""
    dest.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    counts = split_counts(n_frames, len(videos))
    for v, per in zip(videos, counts):
        if len(out) >= n_frames:
            break
        title = v.get("title") or v.get("video_id")
        log(f"taking {per} frame(s) from: {title}")
        tmp = Path(tempfile.mkdtemp(prefix="wr_look_"))
        try:
            try:
                path, dur = download(v["url"], tmp)
            except Exception as e:  # one unavailable video is not the end
                log(f"  skipped ({str(e)[:120]})")
                continue
            for at in frame_times(dur or v.get("duration"), per):
                name = f"f{len(out) + 1:02d}.jpg"
                if grab(path, at, dest / name):
                    out.append({"file": name, "video": v["video_id"],
                                "title": title})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return out


def excerpt(video: dict) -> str:
    p = video.get("transcript_path")
    if not p:
        return ""
    try:
        text = Path(p).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return " ".join(text.split())[:EXCERPT_CHARS]


def build_prompt(channel_name: str, videos: list[dict],
                 frames: list[dict]) -> str:
    lines = [f"Channel: {channel_name}", "", "Video titles:"]
    lines += [f"- {v.get('title')}" for v in videos]
    ex = [(v.get("title"), excerpt(v)) for v in videos]
    ex = [(t, e) for t, e in ex if e]
    if ex:
        lines += ["", "Transcript excerpts (what the narration is like):"]
        lines += [f'- "{t}": {e}' for t, e in ex[:3]]
    return (
        "I study a YouTube channel to understand the look I am aiming for. "
        f"The attached {len(frames)} images are real frames taken from its "
        "videos (not thumbnails). Look at them together.\n\n"
        + "\n".join(lines) + "\n\n"
        "Write two parts, with these exact marker lines:\n\n"
        f"{STYLE_MARK}\n"
        "A visual style guide that an image generator could follow: medium "
        "and rendering (3D, 2D, photo, ...), colour palette (with colour "
        "names or hex), lighting, camera and framing, composition, texture, "
        "mood, typical on-screen text or graphics, and 2-3 things that must "
        "never appear. End with one paragraph usable as a prompt prefix.\n\n"
        f"{BIBLE_MARK}\n"
        "A bible of what recurs: the main characters (look, clothing, "
        "proportions, expressions), the recurring environments (what they "
        "contain, time of day, colours) and recurring props. Describe only "
        "what you can see in the images; say 'not seen' rather than guess.\n\n"
        "Do not name the channel inside the style guide and do not copy "
        "exact characters - describe the look so a different channel could "
        "work in a similar direction.")


def parse_reply(reply: str) -> tuple[str, str]:
    text = str(reply or "")
    s = text.find(STYLE_MARK)
    b = text.find(BIBLE_MARK)
    if s >= 0 and b > s:
        return (text[s + len(STYLE_MARK):b].strip(),
                text[b + len(BIBLE_MARK):].strip())
    if b >= 0 and s > b:
        return (text[s + len(STYLE_MARK):].strip(),
                text[b + len(BIBLE_MARK):s].strip())
    return text.strip(), ""


def run(cfg, channel_id: str, site: str, transport, log, n_frames=DEFAULT_FRAMES,
        download=download_low_res, grab=grab_frame) -> dict:
    n_frames = max(MIN_FRAMES, min(MAX_FRAMES, int(n_frames or DEFAULT_FRAMES)))
    conn = db.connect(cfg.db_path)
    try:
        ch = db.get_channel(conn, channel_id)
        if ch is None:
            raise RuntimeError("Unknown watched channel")
        name, cid = ch["name"], ch["channel_id"]
        videos = pick_videos(conn, cid)
    finally:
        conn.close()
    if not videos:
        raise RuntimeError("This channel has no videos stored yet - run "
                           "'backfill' on it first")
    d = look_dir(cfg, cid)
    frames_dir = d / "frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir, ignore_errors=True)
    frames = collect_frames(videos, n_frames, frames_dir, log, download, grab)
    if len(frames) < 3:
        raise RuntimeError(f"Only {len(frames)} frame(s) could be taken - "
                           "check that yt-dlp and ffmpeg work")
    log(f"asking the web chat with {len(frames)} frames")
    reply = transport.ask(site, build_prompt(name, videos, frames),
                          files=[str(frames_dir / f["file"]) for f in frames])
    style, bible = parse_reply(reply)
    if not style:
        raise RuntimeError("The chat answered with nothing usable")
    d.mkdir(parents=True, exist_ok=True)
    (d / "style.md").write_text(style + "\n", encoding="utf-8")
    (d / "bible.md").write_text((bible or "") + "\n", encoding="utf-8")
    meta = {"channel": name, "site": site, "frames": len(frames),
            "videos": [v["title"] for v in videos],
            "at": __import__("time").strftime("%Y-%m-%d %H:%M")}
    (d / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    log("saved style guide and bible with the watched channel")
    return meta
