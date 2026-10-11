"""Port of Ffprobe.cs."""
from __future__ import annotations

from . import tool_locator
from .ffmpeg_runner import FfmpegRunner


def ffprobe_duration_seconds(media_path: str, ffprobe_path: str = "ffprobe", cancel_event=None) -> float:
    resolved = tool_locator.resolve("ffprobe", ffprobe_path)
    result = FfmpegRunner(resolved).run(
        ["-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", media_path],
        cancel_event=cancel_event,
    )
    if not result.success:
        raise RuntimeError(f'ffprobe failed for "{media_path}" (exit {result.exit_code}): {result.error_tail}')
    line = next((l.strip() for l in result.standard_output.split("\n") if l.strip()), None)
    try:
        if line is None:
            raise ValueError
        return float(line)
    except ValueError:
        raise RuntimeError(f'ffprobe returned no duration for "{media_path}".')
