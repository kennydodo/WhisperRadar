"""Visual style guide + character/environment bible + WRITING style for a
WATCHED channel.

Manual only ("Get style & bible" on Watched channels). It samples a handful of
real video frames (not thumbnails - those do not always show what the video
looks like) from the channel's most viewed videos, sends them with a few titles
and transcript excerpts to an image-capable web-chat LLM and keeps the answer
beside the channel: data/channel_look/<channel_id>/{style.md,bible.md,frames/}.

The writing style is asked in a second message, built from the transcripts of
several of the channel's videos (not just one): the same style guide stage 1
used to write per video (voice, pacing, sentences, hook, structure as time
beats, CTA, vocabulary, things to avoid) plus a Story Layout section on how the
channel lays out its stories. It is saved as writing_style.md beside the rest.

The result stays with the watched channel. Nothing is copied into your own
channels - copy it by hand from the page when you want to try it.
"""
from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from . import db

MAX_ATTACH = 18                # images per message (Claude refuses > 20)
SEND_COUNT = 15                # default: this many of the collected frames are sent
THUMBS = 3                     # thumbnails of the first videos, as extras
MAX_POOL = 50                  # frames kept per channel (collected once, reused)
MAX_FRAMES = MAX_POOL
MIN_FRAMES = 5
DEFAULT_FRAMES = 45            # video frames to collect (+ up to 3 thumbnails)
MAX_VIDEOS = 12
POOL_FILE = "pool.json"        # inside frames/: which video each frame came from
EXCERPT_CHARS = 700
WRITING_FILE = "writing_style.md"
TRANSCRIPT_VIDEOS = 6          # transcripts the writing style is built from
TRANSCRIPT_CHARS = 9000        # per video: opening and ending are kept
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
    try:
        meta["writing_style"] = (d / WRITING_FILE).read_text(encoding="utf-8")
    except OSError:
        meta["writing_style"] = ""
    meta["files"] = sorted(p.name for p in (d / "frames").glob("*.jpg"))
    meta["sent"] = [n for n in meta.get("sent") or [] if n in meta["files"]]
    return meta


def _words(title) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", str(title or "").lower())
            if len(w) > 2}


def pick_videos(conn, channel_id: str, limit: int = MAX_VIDEOS) -> list[dict]:
    """Most viewed first (they show the look that works), but not `limit`
    near-identical titles: a video whose title shares most words with one
    already picked is skipped while others are left. Newest as a tie-break
    and as the fallback when no view counts are stored."""
    rows = conn.execute(
        "SELECT * FROM videos WHERE channel_id = ? "
        "ORDER BY COALESCE(view_count, 0) DESC, published_at DESC LIMIT ?",
        (channel_id, max(limit * 4, 30))).fetchall()
    pool = [dict(r) for r in rows]
    chosen: list[dict] = []
    for v in pool:
        w = _words(v.get("title"))
        if any(w and len(w & _words(c.get("title"))) / len(
                w | _words(c.get("title"))) > 0.5 for c in chosen):
            continue
        chosen.append(v)
        if len(chosen) >= limit:
            return chosen
    for v in pool:                       # not enough different ones: fill up
        if v not in chosen:
            chosen.append(v)
            if len(chosen) >= limit:
                break
    return chosen


def frame_times(duration, per_video: int, shift: float = 0.0) -> list[float]:
    """Spread the picks over the video: with 3+ frames the first is early in
    (the intro often shows the narrator/mascot), the rest are spread over the
    rest of it. `shift` (0..1) moves them so different videos are not sampled
    at the same points."""
    d = float(duration or 0)
    if d <= 0:
        return [30.0 * (i + 1) + 7.0 * shift for i in range(per_video)]
    out = []
    rest = per_video
    if per_video >= 3:
        out.append(min(6.0, d * 0.04) + 3.0 * shift)
        rest -= 1
    lo, span = 0.10, 0.84
    for i in range(rest):
        out.append(d * (lo + span * (i + 0.5 + (shift - 0.5) * 0.6) / rest))
    return [min(max(t, 0.5), max(d - 1.0, 0.5)) for t in out]


def split_counts(n_frames: int, n_videos: int) -> list[int]:
    n_videos = max(1, min(n_videos, n_frames))
    base, extra = divmod(n_frames, n_videos)
    return [base + (1 if i < extra else 0) for i in range(n_videos)]


def choose_files(pool: list[str], selected=None, count: int = SEND_COUNT,
                 rng=None) -> list[str]:
    """Which collected frames go to the chat. `selected` (the user's ticks)
    wins; names that are not in the pool are ignored. With none selected a
    random `count` is drawn. Never more than MAX_ATTACH."""
    names = sorted(pool)
    if selected:
        wanted = set(selected)
        chosen = [n for n in names if n in wanted]
        if not chosen:
            raise ValueError("None of the selected frames exist any more")
        if len(chosen) > MAX_ATTACH:
            raise ValueError(f"Select at most {MAX_ATTACH} frames "
                             f"(you selected {len(chosen)})")
        return chosen
    if not names:
        raise ValueError("There are no collected frames to send")
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = SEND_COUNT
    count = max(1, min(MAX_ATTACH, count))
    return sorted((rng or random).sample(names, min(count, len(names))))


def _read_pool(frames_dir: Path) -> dict:
    try:
        data = json.loads((frames_dir / POOL_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _write_pool(frames_dir: Path, videos: list[dict], frames: list[dict]):
    (frames_dir / POOL_FILE).write_text(json.dumps({
        "videos": [{"video_id": v.get("video_id"), "title": v.get("title")}
                   for v in videos],
        "frames": frames}, indent=1), encoding="utf-8")


def videos_by_id(conn, ids: list[str]) -> list[dict]:
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT * FROM videos WHERE video_id IN ({q})",
                        list(ids)).fetchall()
    order = {v: i for i, v in enumerate(ids)}
    return sorted((dict(r) for r in rows), key=lambda r: order[r["video_id"]])


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
    for idx, (v, per) in enumerate(zip(videos, counts)):
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
            for at in frame_times(dur or v.get("duration"), per,
                                  shift=((idx * 0.37) % 1.0)):
                name = f"f{len(out) + 1:02d}.jpg"
                if grab(path, at, dest / name):
                    out.append({"file": name, "video": v["video_id"],
                                "title": title})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return out


def fetch_thumbnails(videos: list[dict], dest: Path, log,
                     n: int = THUMBS) -> list[dict]:
    """The thumbnails of the first videos (they often show the recurring
    character large). Best effort: a failed download is just skipped."""
    import urllib.request
    out: list[dict] = []
    for v in videos[:n]:
        vid = v.get("video_id")
        if not vid:
            continue
        name = f"t{len(out) + 1:02d}.jpg"
        try:
            req = urllib.request.Request(
                f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
            if len(data) < 1000:
                continue
            (dest / name).write_bytes(data)
            out.append({"file": name, "video": vid,
                        "title": v.get("title") or vid})
        except Exception as e:  # noqa: BLE001
            log(f"  no thumbnail for {vid} ({str(e)[:80]})")
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
                 frames: list[dict], hint: str = "") -> str:
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
        "videos at different points (the files starting with t are "
        "thumbnails). Look at them together.\n\n"
        + "\n".join(lines) + "\n\n"
        + (f"What I know about the channel: {hint.strip()}\n\n"
           if hint and hint.strip() else "")
        + "Write two parts, with these exact marker lines:\n\n"
        f"{STYLE_MARK}\n"
        "A visual style guide that an image generator could follow: medium "
        "and rendering (3D, 2D, photo, ...), colour palette (with colour "
        "names or hex), lighting, camera and framing, composition, texture, "
        "mood, typical on-screen text or graphics, and 2-3 things that must "
        "never appear. Write each colour on ONE line with its hex code "
        "inline (e.g. 'cream #F1E6C8'). End with one paragraph usable as a "
        "prompt prefix.\n\n"
        f"{BIBLE_MARK}\n"
        "A bible of what recurs: the main characters (look, clothing, "
        "proportions, expressions), the recurring environments (what they "
        "contain, time of day, colours) and recurring props. Describe only "
        "what you can see in the images; say 'not seen' rather than guess. "
        "Cover EVERY kind of recurring subject you see - a protagonist, "
        "narrator or mascot, other humans, animals, objects - and say "
        "clearly if the channel seems to have no recurring character. "
        "Do not list one-off scene details of a single video.\n\n"
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


def _ask(transport, site, name, videos, frames_dir: Path, send: list[str],
         hint: str, log) -> tuple[str, str]:
    log(f"asking the web chat with {len(send)} frame(s)")
    reply = transport.ask(site, build_prompt(name, videos, send, hint),
                          files=[str(frames_dir / n) for n in send])
    style, bible = parse_reply(reply)
    if not style:
        raise RuntimeError("The chat answered with nothing usable")
    return style, bible


def pick_transcribed(conn, channel_id: str,
                     limit: int = TRANSCRIPT_VIDEOS) -> list[dict]:
    """Videos of this channel that have a transcript on disk, most viewed
    first, skipping near-identical titles while others are left."""
    rows = conn.execute(
        "SELECT * FROM videos WHERE channel_id = ? AND "
        "COALESCE(transcript_path, '') != '' "
        "ORDER BY COALESCE(view_count, 0) DESC, published_at DESC LIMIT ?",
        (channel_id, max(limit * 6, 30))).fetchall()
    chosen: list[dict] = []
    rest: list[dict] = []
    for r in rows:
        v = dict(r)
        if not Path(str(v.get("transcript_path"))).is_file():
            continue
        w = _words(v.get("title"))
        if any(w and len(w & _words(c.get("title"))) / len(
                w | _words(c.get("title"))) > 0.5 for c in chosen):
            rest.append(v)
        else:
            chosen.append(v)
        if len(chosen) >= limit:
            return chosen
    return (chosen + rest)[:limit]


def _transcript_text(video: dict, max_chars: int = TRANSCRIPT_CHARS) -> str:
    try:
        text = Path(video["transcript_path"]).read_text(
            encoding="utf-8", errors="replace")
    except (OSError, KeyError, TypeError):
        return ""
    text = " ".join(text.split())
    if len(text) > max_chars:       # keep the opening AND the ending
        head = int(max_chars * 0.6)
        text = text[:head] + " [...] " + text[-(max_chars - head):]
    return text


STORY_HEADER = "## Story Layout (how this channel lays out its videos)"
STORY_RULES = """- STORY LAYOUT. The transcripts are different videos from ONE channel. In
  the Story Layout section describe how the channel lays out its stories: the
  recurring arc from opening to ending, the kinds of segments it reuses and
  their usual order, how it moves between segments (transitions, callbacks,
  open loops), how a topic is introduced and how it is resolved, and how the
  layout changes with the type of video if it does. Describe ROLES and patterns
  only, never the topic, examples, numbers, titles or story of any one video;
  give timing only as shares of the runtime. Keep only what repeats across at
  least two videos and say so where the videos differ."""
MULTI_NOTE = ("The TRANSCRIPT block holds {n} separate videos from the same "
              "channel, each headed '### Video N'. Describe the writing style "
              "they SHARE and ignore what is true of only one video. Never "
              "mention a video's title or topic.")


def writing_prompt(name: str, genre: str, videos: list[dict], hint: str = "",
                   variant: str | None = None) -> str:
    """The stage-1 writing-style prompt (same sections and rules), asked about
    several of the channel's videos at once, plus a Story Layout section."""
    from . import studio
    parts, total = [], 0
    for i, v in enumerate(videos, 1):
        t = _transcript_text(v)
        if t:
            parts.append(f"### Video {i}\n{t}")
            total += len(t.split())
    if not parts:
        raise RuntimeError("No transcripts are stored for this channel's "
                           "videos yet - transcribe a few first")
    direction = MULTI_NOTE.format(n=len(parts))
    if hint and hint.strip():
        direction += "\nWhat I know about the channel: " + hint.strip()
    prompt = studio.style_prompt(
        f"{name} (several videos)", genre or "general", "\n\n".join(parts),
        word_count=max(1, total // len(parts)), extra_direction=direction,
        variant=variant)
    lines = prompt.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("## Structure"):
            lines.insert(i + 1, STORY_HEADER)
            break
    else:
        raise RuntimeError("The writing-style prompt changed shape")
    prompt = "\n".join(lines)
    marker = "\n\nOutput ONLY the style guide markdown."
    if marker in prompt:
        return prompt.replace(marker, "\n" + STORY_RULES + marker, 1)
    return prompt.rstrip() + "\n" + STORY_RULES + "\n"


def _ask_writing(cfg, transport, site, name, genre, videos, hint, log) -> str:
    from . import studio
    prompt = writing_prompt(name, genre, videos, hint,
                            studio.style_variant(cfg=cfg))
    log(f"asking the web chat for the writing style from {len(videos)} "
        "video transcript(s)")
    reply = str(transport.ask(site, prompt) or "").strip()
    if not reply:
        raise RuntimeError("The chat answered the writing style with nothing "
                           "usable")
    return reply


def _save_writing(d: Path, name, site, text, titles) -> dict:
    """Write writing_style.md and note what it was built from in meta.json
    (creating a minimal meta when only the writing style exists yet)."""
    d.mkdir(parents=True, exist_ok=True)
    (d / WRITING_FILE).write_text(text.strip() + "\n", encoding="utf-8")
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {"channel": name, "site": site, "frames": 0, "sent": [],
                "videos": [], "at": __import__("time").strftime("%Y-%m-%d %H:%M")}
    meta["writing_videos"] = list(titles)
    meta["writing_at"] = __import__("time").strftime("%Y-%m-%d %H:%M")
    meta["writing_site"] = site
    (d / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return meta


def regenerate_writing(cfg, channel_id: str, site: str, transport, log,
                       hint: str = "") -> dict:
    """Ask for the writing style again from the stored transcripts - no
    download, no frames. The old one stays if the chat fails."""
    conn = db.connect(cfg.db_path)
    try:
        ch = db.get_channel(conn, channel_id)
        if ch is None:
            raise RuntimeError("Unknown watched channel")
        name, cid, genre = ch["name"], ch["channel_id"], ch["genre"]
        videos = pick_transcribed(conn, cid)
    finally:
        conn.close()
    text = _ask_writing(cfg, transport, site, name, genre, videos, hint, log)
    meta = _save_writing(look_dir(cfg, cid), name, site, text,
                         [v["title"] for v in videos])
    log("saved the writing style")
    return meta


def _save(d: Path, name, site, style, bible, pool_n, send, titles, extra=None):
    d.mkdir(parents=True, exist_ok=True)
    (d / "style.md").write_text(style + "\n", encoding="utf-8")
    (d / "bible.md").write_text((bible or "") + "\n", encoding="utf-8")
    meta = {"channel": name, "site": site, "frames": pool_n,
            "sent": list(send), "videos": list(titles),
            "at": __import__("time").strftime("%Y-%m-%d %H:%M")}
    try:        # a refresh of the look keeps the writing style it belongs with
        old = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        for k in ("writing_videos", "writing_at", "writing_site"):
            if k in old:
                meta[k] = old[k]
    except (OSError, ValueError):
        pass
    meta.update(extra or {})
    (d / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return meta


def run(cfg, channel_id: str, site: str, transport, log, n_frames=DEFAULT_FRAMES,
        download=download_low_res, grab=grab_frame, hint: str = "",
        thumbs=fetch_thumbnails, send_count: int = SEND_COUNT,
        rng=None) -> dict:
    """Collect a pool of frames (up to MAX_POOL), send a random `send_count`
    of them to the chat, keep the pool + the answer."""
    n_frames = max(MIN_FRAMES, min(MAX_FRAMES, int(n_frames or DEFAULT_FRAMES)))
    conn = db.connect(cfg.db_path)
    try:
        ch = db.get_channel(conn, channel_id)
        if ch is None:
            raise RuntimeError("Unknown watched channel")
        name, cid, genre = ch["name"], ch["channel_id"], ch["genre"]
        n_videos = max(3, min(MAX_VIDEOS, -(-n_frames // 4)))
        videos = pick_videos(conn, cid, n_videos)
        talk = pick_transcribed(conn, cid)
    finally:
        conn.close()
    if not videos:
        raise RuntimeError("This channel has no videos stored yet - run "
                           "'backfill' on it first")
    d = look_dir(cfg, cid)
    # a refresh collects into a scratch folder: the saved frames, style guide
    # and bible are replaced together only when the new run succeeded
    frames_dir = d / "frames.new"
    if frames_dir.exists():
        shutil.rmtree(frames_dir, ignore_errors=True)
    frames = collect_frames(videos, n_frames, frames_dir, log, download, grab)
    if thumbs is not None:
        try:
            frames += thumbs(videos, frames_dir, log)[:max(
                0, MAX_POOL - len(frames))]
        except Exception as e:  # noqa: BLE001
            log(f"thumbnails skipped ({str(e)[:80]})")
    frames = frames[:MAX_POOL]
    if len(frames) < 3:
        raise RuntimeError(f"Only {len(frames)} frame(s) could be taken - "
                           "check that yt-dlp and ffmpeg work")
    log(f"collected {len(frames)} frames")
    _write_pool(frames_dir, videos, frames)
    send = choose_files([f["file"] for f in frames], None, send_count, rng)
    style, bible = _ask(transport, site, name, videos, frames_dir, send,
                        hint, log)
    final = d / "frames"
    d.mkdir(parents=True, exist_ok=True)
    if final.exists():
        shutil.rmtree(final, ignore_errors=True)
    frames_dir.rename(final)
    meta = _save(d, name, site, style, bible, len(frames), send,
                 [v["title"] for v in videos])
    log("saved style guide and bible with the watched channel")
    # the writing style is a second question, from the transcripts of several
    # videos; if it fails the look that was just saved is kept
    try:
        text = _ask_writing(cfg, transport, site, name, genre, talk, hint, log)
        meta = _save_writing(d, name, site, text, [v["title"] for v in talk])
        log("saved the writing style")
    except Exception as e:  # noqa: BLE001
        log(f"writing style skipped: {str(e)[:160]}")
    return meta


def regenerate(cfg, channel_id: str, site: str, transport, log,
               selected=None, send_count: int = SEND_COUNT, hint: str = "",
               rng=None) -> dict:
    """Ask again with frames that are already collected - nothing is
    downloaded. `selected` = the frames the user ticked; none ticked = a new
    random `send_count`. The old answer stays if the chat fails."""
    conn = db.connect(cfg.db_path)
    try:
        ch = db.get_channel(conn, channel_id)
        if ch is None:
            raise RuntimeError("Unknown watched channel")
        name, cid = ch["name"], ch["channel_id"]
        d = look_dir(cfg, cid)
        frames_dir = d / "frames"
        pool = sorted(p.name for p in frames_dir.glob("*.jpg"))
        if not pool:
            raise RuntimeError("No collected frames yet - use "
                               "'Get style & bible' first")
        pool_info = _read_pool(frames_dir)
        videos = videos_by_id(conn, [v.get("video_id") for v in
                                     pool_info.get("videos") or []
                                     if v.get("video_id")])
    finally:
        conn.close()
    send = choose_files(pool, selected, send_count, rng)
    style, bible = _ask(transport, site, name, videos, frames_dir, send,
                        hint, log)
    old = load(cfg, cid) or {}
    meta = _save(d, name, site, style, bible, len(pool), send,
                 old.get("videos") or [v.get("title") for v in videos],
                 {"regenerated": True})
    log("saved the new style guide and bible")
    return meta
