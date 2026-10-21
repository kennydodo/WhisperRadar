"""Narration speed: speed the generated voice up or slow it down with ffmpeg
(atempo - the pitch stays), right after the TTS step and before the subtitles,
so the cues and the video follow the changed audio. The untouched original is
kept as audio_original<ext> (find_audio only looks at audio<ext>)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

MIN_SPEED, MAX_SPEED = 0.5, 2.0       # what one atempo pass accepts everywhere
_EPS = 0.005


def clamp(speed: float) -> float:
    return max(MIN_SPEED, min(MAX_SPEED, float(speed)))


def is_change(speed) -> bool:
    try:
        return abs(clamp(speed) - 1.0) >= _EPS
    except (TypeError, ValueError):
        return False


def command(src: Path, dst: Path, speed: float) -> list:
    ext = dst.suffix.lower()
    codec = {".mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
             ".m4a": ["-c:a", "aac", "-b:a", "192k"]}.get(ext, [])
    return ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn",
            "-filter:a", f"atempo={clamp(speed):.4f}", *codec, str(dst)]


def apply(audio: Path, speed: float, fresh: bool = False) -> Path:
    """Change `audio` to `speed` in place. The first time, the original is
    copied to audio_original<ext>; a later run starts from that original, so
    the speed never compounds. fresh=True: `audio` was just generated, so it
    becomes the new original. Returns the audio path."""
    audio = Path(audio)
    original = audio.with_name("audio_original" + audio.suffix)
    if fresh or not original.exists():
        shutil.copy2(audio, original)
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg was not found on PATH - it is needed for the narration speed")
    tmp = audio.with_name("audio_speed_tmp" + audio.suffix)
    res = subprocess.run(command(original, tmp, speed), capture_output=True, text=True)
    if res.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg could not change the narration speed: "
                           + (res.stderr or "").strip()[-300:])
    tmp.replace(audio)
    return audio
