"""Shorts clipping: pick the strongest 20-58 second moments of a finished
video from its subtitles and cut each to a vertical 9:16 clip with the
captions burned in (ffmpeg).

Picking is local and rule-based (no LLM): windows end on a sentence end,
start on a hook (a question, a number, a "never/secret/mistake" word), and
are scored for speaking density. Clips never overlap."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

SHORTS_DIR = "shorts"
MIN_S, MAX_S = 20.0, 58.0
HOOK_WORDS = ("never", "secret", "mistake", "stop", "why", "how", "truth",
              "nobody", "everyone", "wrong", "rule", "don't", "dont", "must",
              "before", "only", "first", "biggest")
_TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


def _secs(ts: str) -> float:
    m = _TS.search(ts)
    if not m:
        return 0.0
    h, mi, s, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000


def parse_srt(text: str) -> list[dict]:
    """[{start, end, text}] from SRT text."""
    cues = []
    for block in re.split(r"\n\s*\n", (text or "").strip().replace("\r", "")):
        lines = [l for l in block.split("\n") if l.strip()]
        for k, line in enumerate(lines):
            if "-->" in line:
                a, b = line.split("-->", 1)
                body = " ".join(lines[k + 1:]).strip()
                if body:
                    cues.append({"start": _secs(a), "end": _secs(b),
                                 "text": body})
                break
    return cues


def _fmt(t: float) -> str:
    t = max(0.0, t)
    ms = int(round(t * 1000))
    return (f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:"
            f"{ms // 1000 % 60:02d},{ms % 1000:03d}")


def srt_for_clip(cues: list[dict], start: float, end: float) -> str:
    """The cues inside [start, end], shifted so the clip begins at 0."""
    blocks, n = [], 0
    for c in cues:
        if c["end"] <= start or c["start"] >= end:
            continue
        n += 1
        a, b = max(c["start"], start) - start, min(c["end"], end) - start
        blocks.append(f"{n}\n{_fmt(a)} --> {_fmt(b)}\n{c['text']}\n")
    return "\n".join(blocks)


def _hook_score(text: str) -> float:
    t = text.lower()
    pts = 0.0
    if "?" in t:
        pts += 2
    if re.search(r"\d", t):
        pts += 1.5
    pts += sum(1.0 for w in HOOK_WORDS if re.search(rf"\b{re.escape(w)}\b", t))
    return min(pts, 5.0)


def plan_clips(cues: list[dict], n: int = 3, min_s: float = MIN_S,
               max_s: float = MAX_S) -> list[dict]:
    """Up to `n` non-overlapping clips: [{start, end, score, hook}]."""
    cands = []
    for i, first in enumerate(cues):
        start = first["start"]
        words = 0
        for j in range(i, len(cues)):
            end = cues[j]["end"]
            dur = end - start
            if dur > max_s:
                break
            words += len(cues[j]["text"].split())
            sentence_end = cues[j]["text"].rstrip().endswith((".", "?", "!"))
            if dur >= min_s and sentence_end:
                density = words / dur            # ~2.5 words/s is lively
                score = (_hook_score(first["text"]) * 2
                         + min(density, 3.5) * 2
                         + (1 if dur <= 45 else 0))
                cands.append({"start": start, "end": end,
                              "score": round(score, 2),
                              "hook": first["text"][:90]})
    cands.sort(key=lambda c: -c["score"])
    chosen = []
    for c in cands:
        if all(c["end"] <= k["start"] or c["start"] >= k["end"]
               for k in chosen):
            chosen.append(c)
        if len(chosen) >= n:
            break
    chosen.sort(key=lambda c: c["start"])
    return chosen


def ffmpeg_cmd(src, start: float, end: float, out, srt_name: str,
               width: int = 1080, height: int = 1920) -> list[str]:
    """Vertical clip: the picture fitted over a blurred copy of itself, the
    captions burned in at the lower third. `srt_name` is relative to the
    working directory the command runs in (avoids Windows path escaping)."""
    style = ("FontName=Arial,FontSize=14,Bold=1,PrimaryColour=&H00FFFFFF,"
             "OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=0,"
             "Alignment=2,MarginV=260")
    fc = (f"[0:v]split=2[bg][fg];"
          f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
          f"crop={width}:{height},boxblur=24:2[b];"
          f"[fg]scale={width}:-2[f];"
          f"[b][f]overlay=(W-w)/2:(H-h)/2,"
          f"subtitles={srt_name}:force_style='{style}'[v]")
    return ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.2f}",
            "-i", str(src), "-t", f"{end - start:.2f}",
            "-filter_complex", fc, "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
            str(out)]


def shorts_dir(pdir) -> Path:
    return Path(pdir) / SHORTS_DIR


def make_clips(pdir, n: int = 3, run=subprocess.run, log=print) -> list[dict]:
    """Plan and cut the clips for a production folder. Returns
    [{file, start, end, hook, score}] for the clips that were written."""
    from . import studio
    pdir = Path(pdir).resolve()
    srt, video = studio.find_srt(pdir), studio.find_final(pdir)
    if not srt or not video:
        raise ValueError("A shorts run needs the finished video and its "
                         "subtitles in this production.")
    cues = parse_srt(srt.read_text(encoding="utf-8", errors="replace"))
    plan = plan_clips(cues, n=n)
    if not plan:
        raise ValueError("No 20-58 second stretch that ends on a full "
                         "sentence was found in the subtitles.")
    d = shorts_dir(pdir)
    d.mkdir(parents=True, exist_ok=True)
    done = []
    for k, c in enumerate(plan, 1):
        name = f"short{k}"
        (d / f"{name}.srt").write_text(
            srt_for_clip(cues, c["start"], c["end"]), encoding="utf-8")
        out = d / f"{name}.mp4"
        cmd = ffmpeg_cmd(video, c["start"], c["end"], out, f"{name}.srt")
        try:
            run(cmd, check=True, timeout=900, cwd=str(d))
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"shorts: {name} failed: {exc}")
            continue
        if out.exists() and out.stat().st_size > 0:
            done.append({"file": f"{SHORTS_DIR}/{out.name}", **c})
    return done
