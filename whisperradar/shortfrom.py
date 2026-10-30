"""A new Short (9:16 production) made from a time window of an existing video.

Reuses the old production's narration: the audio and subtitles of the window
are cut out and the script text of those cues is written as script.md. The
new production starts at the shots stage, so it is planned and illustrated
again, as vertical pictures. The old production is never changed."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import db, frames, shorts, studio

MIN_SECONDS = 8.0
MAX_SECONDS = 180.0     # YouTube Shorts allow up to 3 minutes


class ShortError(ValueError):
    pass


def parse_time(raw) -> float:
    """'83', '1:23', '0:01:23' or '1:23.5' -> seconds."""
    text = str(raw or "").strip().replace(",", ".")
    if not text:
        raise ShortError("Enter a start and an end time")
    parts = text.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise ShortError(f"'{raw}' is not a time (use 1:23 or seconds)")
    if len(nums) > 3 or any(n < 0 for n in nums):
        raise ShortError(f"'{raw}' is not a time (use 1:23 or seconds)")
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def _cut_audio_cmd(ffmpeg, src: Path, start: float, end: float, dst: Path) -> list[str]:
    return [ffmpeg, "-y", "-loglevel", "error", "-ss", f"{start:.3f}",
            "-i", str(src), "-t", f"{end - start:.3f}", str(dst)]


def window_text(cues: list[dict], start: float, end: float) -> str:
    """The narration of the cues inside the window, one paragraph."""
    words = [c["text"] for c in cues if c["end"] > start and c["start"] < end]
    return " ".join(" ".join(words).split())


def create(cfg, src_pid: int, start, end, title: str | None = None,
           run=subprocess.run) -> int:
    """Make the new production and return its id."""
    s, e = parse_time(start), parse_time(end)
    if e - s < MIN_SECONDS:
        raise ShortError(f"A Short needs at least {MIN_SECONDS:g} seconds")
    if e - s > MAX_SECONDS:
        raise ShortError(f"A Short can be at most {MAX_SECONDS:g} seconds")
    sdir = studio.prod_dir(cfg, src_pid)
    audio, srt = studio.find_audio(sdir), studio.find_srt(sdir)
    if not audio or not srt:
        raise ShortError("The video needs its audio and subtitles first")
    cues = shorts.parse_srt(srt.read_text(encoding="utf-8", errors="replace"))
    if not cues:
        raise ShortError("The subtitles file has no cues")
    if s >= cues[-1]["end"]:
        raise ShortError("The window starts after the end of the narration")
    e = min(e, cues[-1]["end"])
    text = window_text(cues, s, e)
    if not text:
        raise ShortError("No narration inside that window")
    clip_srt = shorts.srt_for_clip(cues, s, e)

    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        src = db.get_production(conn, src_pid)
        if src is None:
            raise ShortError("Unknown production")
        oc = (db.get_own_channel(conn, src["own_channel_id"])
              if src["own_channel_id"] else None)
        new_title = (title or "").strip() or (
            f"{src['title']} - Short {int(s // 60)}:{int(s % 60):02d}")[:300]
        try:
            work_dir = studio.auto_work_dir(cfg, conn, new_title,
                                            oc["name"] if oc else None)
        except RuntimeError as exc:
            raise ShortError(str(exc))
        pid = db.create_production(conn, new_title, src["genre"], None, work_dir)
        fields = {"frame": frames.VERTICAL, "stage": "shots"}
        if oc is not None:
            fields["own_channel_id"] = oc["id"]
        for k in ("voice", "llm_provider", "render_mode"):
            if src[k]:
                fields[k] = src[k]
        db.update_production(conn, pid, **fields)
        prod = db.get_production(conn, pid)
        studio.seed_production(cfg, conn, prod)
    finally:
        conn.close()

    pdir = studio.prod_dir(cfg, pid)
    pdir.mkdir(parents=True, exist_ok=True)
    dst_audio = pdir / f"audio{audio.suffix}"
    r = run(_cut_audio_cmd("ffmpeg", audio, s, e, dst_audio),
            capture_output=True, text=True)
    if getattr(r, "returncode", 0) != 0:
        raise ShortError("Could not cut the audio: "
                         + (getattr(r, "stderr", "") or "")[-300:])
    (pdir / "subtitles.srt").write_text(clip_srt, encoding="utf-8")
    (pdir / "script.md").write_text(text + "\n", encoding="utf-8")
    (pdir / "short_source.json").write_text(
        '{"from_production": %d, "start": %.3f, "end": %.3f}\n' % (src_pid, s, e),
        encoding="utf-8")
    return pid
