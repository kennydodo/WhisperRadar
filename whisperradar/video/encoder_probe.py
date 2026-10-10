"""Port of EncoderProbe.cs: which encoders an ffmpeg binary has / can really initialise."""
from __future__ import annotations

import threading
from typing import Dict, FrozenSet, Tuple

from .ffmpeg_runner import FfmpegRunner

_lock = threading.Lock()
_cache: Dict[str, FrozenSet[str]] = {}
_verify_cache: Dict[Tuple[str, str], bool] = {}


def clear_cache() -> None:
    with _lock:
        _cache.clear()
        _verify_cache.clear()


def get_encoders(ffmpeg_path: str) -> FrozenSet[str]:
    with _lock:
        if ffmpeg_path not in _cache:
            _cache[ffmpeg_path] = _load(ffmpeg_path)
        return _cache[ffmpeg_path]


def supports(ffmpeg_path: str, encoder: str) -> bool:
    """True if the encoder is compiled in AND can initialise right now (test-encodes one frame)."""
    if encoder.lower() not in {e.lower() for e in get_encoders(ffmpeg_path)}:
        return False
    key = (ffmpeg_path, encoder)
    with _lock:
        if key not in _verify_cache:
            _verify_cache[key] = _verify(ffmpeg_path, encoder)
        return _verify_cache[key]


def _verify(ffmpeg_path: str, encoder: str) -> bool:
    from .render_plan import video_encoder_args
    try:
        args = ["-hide_banner", "-f", "lavfi", "-i", "color=black:s=64x64:d=0.1", "-frames:v", "1"]
        args += video_encoder_args(encoder, "ultrafast", 28, 0)
        args += ["-f", "null", "-"]
        return FfmpegRunner(ffmpeg_path).run(args).success
    except RuntimeError:
        return False


def _load(ffmpeg_path: str) -> FrozenSet[str]:
    try:
        result = FfmpegRunner(ffmpeg_path).run(["-hide_banner", "-encoders"])
    except RuntimeError:
        return frozenset()
    if not result.success:
        return frozenset()
    encoders = set()
    for line in result.standard_output.split("\n"):
        parts = line.split()
        if len(parts) >= 2 and "V" in parts[0]:
            encoders.add(parts[1])
    return frozenset(encoders)
