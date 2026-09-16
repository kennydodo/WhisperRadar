"""Configuration loading. Paths in config.yaml are relative to its directory."""

from pathlib import Path

import yaml

DEFAULT_CONFIG_NAME = "config.yaml"


class Config:
    def __init__(self, base_dir: Path, raw: dict):
        self.base_dir = base_dir
        self.db_path = base_dir / raw.get("db_path", "data/whisperradar.db")
        self.audio_dir = base_dir / raw.get("audio_dir", "data/audio")
        self.transcripts_dir = base_dir / raw.get("transcripts_dir", "data/transcripts")
        self.exports_dir = base_dir / "exports"
        self.cookies_from_browser = raw.get("cookies_from_browser") or None
        self.max_video_seconds = raw.get("max_video_seconds") or None

        whisper = raw.get("whisper") or {}
        self.whisper_model = whisper.get("model", "small")
        self.whisper_language = whisper.get("language") or None

        channels = []
        for ch in raw.get("channels") or []:
            channels.append(
                {
                    "name": ch.get("name") or f"channel-{str(ch.get('id'))[:8]}",
                    "id": str(ch.get("id") or "").strip(),
                    "kind": ch.get("kind") or "primary",
                    "genre": ch.get("genre") or "general",
                    "active": bool(ch.get("active", True)),
                }
            )
        self.channels = channels


def load_config(config_path: str | None = None) -> Config:
    path = (
        Path(config_path)
        if config_path
        else Path(__file__).resolve().parent.parent / DEFAULT_CONFIG_NAME
    )
    if not path.exists():
        raise SystemExit(f"Config not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config(path.parent.resolve(), raw)
