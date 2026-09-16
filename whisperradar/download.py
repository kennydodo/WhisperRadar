"""Audio downloading via yt-dlp. Requires ffmpeg on PATH."""

from pathlib import Path

import yt_dlp


def download_audio(
    video_url: str,
    audio_dir: Path,
    cookies_from_browser: str | None = None,
    timeout: int = 600,
) -> tuple[Path, float | None]:
    """Download bestaudio as mp3 into audio_dir. Returns (path, duration_seconds)."""
    audio_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": "bestaudio[abr<=160]/bestaudio",
        "outtmpl": str(audio_dir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }
        ],
    }
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=True)

    video_id = info["id"]
    mp3 = audio_dir / f"{video_id}.mp3"
    if mp3.exists():
        return mp3, info.get("duration")

    # ffmpeg postprocess may have failed; use whatever landed instead
    for candidate in sorted(audio_dir.glob(f"{video_id}.*")):
        return candidate, info.get("duration")
    raise RuntimeError(f"Download produced no audio file for {video_url}")
