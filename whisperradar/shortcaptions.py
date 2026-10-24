"""Big word-by-word captions for vertical (Shorts) productions.

The narration SRT only has times per cue, so each cue's time is shared out over
its words by their length (a long word stays on screen longer). Outputs:
  captions_words.srt  one word per cue (imports into Premiere / CapCut)
  captions.ass        the same words, big and centred for a 1080x1920 frame
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_BLOCK = re.compile(
    r"(\d+)\s*\n(\d\d:\d\d:\d\d[,.]\d{1,3})\s*-->\s*(\d\d:\d\d:\d\d[,.]\d{1,3})\s*\n"
    r"(.*?)(?:\n\s*\n|\Z)", re.S)
MIN_WORD_SECONDS = 0.12
# ASS style: big white bold text, thick black outline, lower-middle of the frame
ASS_STYLE = ("Style: Word,Arial,120,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
             "-1,0,0,0,100,100,0,0,1,9,2,2,60,60,520,1")


def _secs(ts: str) -> float:
    h, m, rest = ts.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def _srt_ts(t: float) -> str:
    t = max(0.0, t)
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _ass_ts(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def words_from_srt(srt_text: str) -> list[tuple[float, float, str]]:
    """[(start, end, word)] - every word of every cue, timed by its length."""
    out: list[tuple[float, float, str]] = []
    for m in _BLOCK.finditer((srt_text or "").replace("\r\n", "\n")):
        start, end = _secs(m.group(2)), _secs(m.group(3))
        words = m.group(4).split()
        if not words or end <= start:
            continue
        weights = [max(2, len(w)) for w in words]
        total = float(sum(weights))
        span = end - start
        t = start
        for w, wt in zip(words, weights):
            d = span * wt / total
            out.append((t, t + max(d, MIN_WORD_SECONDS if span > MIN_WORD_SECONDS * len(words) else 0), w))
            t += d
        # a word never runs into the next one
        for i in range(len(words)):
            idx = len(out) - len(words) + i
            s, e, w = out[idx]
            nxt = out[idx + 1][0] if i + 1 < len(words) else end
            out[idx] = (s, min(e, nxt), w)
    return out


def to_word_srt(words) -> str:
    return "".join(f"{i}\n{_srt_ts(s)} --> {_srt_ts(e)}\n{w}\n\n"
                   for i, (s, e, w) in enumerate(words, 1))


def to_ass(words, width: int = 1080, height: int = 1920, upper: bool = True) -> str:
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\n"
            "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
            "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
            "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,"
            "BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
            f"{ASS_STYLE}\n\n[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,"
            "MarginR,MarginV,Effect,Text\n")
    lines = []
    for s, e, w in words:
        text = (w.upper() if upper else w).replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{_ass_ts(s)},{_ass_ts(e)},Word,,0,0,0,,{text}")
    return head + "\n".join(lines) + "\n"


def write(srt_path, out_dir, width: int = 1080, height: int = 1920) -> dict:
    """Write captions_words.srt and captions.ass into out_dir. Returns the
    paths ({} when the SRT is missing or has no words)."""
    srt = Path(srt_path) if srt_path else None
    if not srt or not srt.is_file():
        return {}
    words = words_from_srt(srt.read_text(encoding="utf-8-sig"))
    if not words:
        return {}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    p_srt, p_ass = out / "captions_words.srt", out / "captions.ass"
    p_srt.write_text(to_word_srt(words), encoding="utf-8")
    p_ass.write_text(to_ass(words, width, height), encoding="utf-8")
    return {"srt": str(p_srt), "ass": str(p_ass)}


def burn_cmd(ffmpeg: str, video_name: str, ass_name: str, out_name: str) -> list[str]:
    """ffmpeg command to burn the ASS into a video. Names are relative to the
    directory the command runs in (no Windows path escaping needed)."""
    return [ffmpeg, "-y", "-loglevel", "error", "-i", video_name,
            "-vf", f"ass={ass_name}", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "23", "-c:a", "copy", out_name]


def burn(ffmpeg: str, folder, video_name: str, ass_name: str, out_name: str) -> bool:
    r = subprocess.run(burn_cmd(ffmpeg, video_name, ass_name, out_name),
                       cwd=str(folder), capture_output=True, text=True)
    return r.returncode == 0
