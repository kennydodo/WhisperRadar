"""Configuration loading. Paths in config.yaml are relative to its directory."""

import re
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

        studio = raw.get("studio") or {}
        self.studio_llm = studio.get("llm", "openai")
        self.studio_llm_base_url = studio.get("llm_base_url") or None
        self.studio_llm_api_key = studio.get("llm_api_key") or None
        self.studio_llm_model = studio.get("llm_model") or None

        # named providers: each has its own base_url / key / model
        self.studio_llm_providers = []
        for i, prov in enumerate(studio.get("llm_providers") or []):
            name = (prov.get("name") or f"provider-{i + 1}").strip()
            self.studio_llm_providers.append({
                "name": name,
                "base_url": (prov.get("base_url") or "").strip() or None,
                "api_key": (prov.get("api_key") or "").strip() or None,
                "model": (prov.get("model") or "").strip() or None,
                "env_key": "WR_" + re.sub(r"[^A-Z0-9]", "_", name.upper()) + "_API_KEY",
            })
        names = [p["name"] for p in self.studio_llm_providers]
        default = studio.get("llm_default") or (names[0] if names else None)
        self.studio_llm_default = default if default in names else (
            names[0] if names else None)

        self.studio_tts_command = studio.get("tts_command") or None
        # OpenSpeaker (ai33.pro) narration: optional config key/voice; the
        # key can also come from the WR_AI33_API_KEY env variable.
        self.studio_ai33_api_key = studio.get("ai33_api_key") or None
        self.studio_ai33_voice = studio.get("ai33_voice") or None
        self.studio_ai33_base_url = studio.get("ai33_base_url") or None
        self.studio_imagegen_command = studio.get("imagegen_command") or None
        self.studio_merge_command = studio.get("merge_command") or None
        self.studio_script_words = studio.get("script_words") or None
        self.renderly_url = (studio.get("renderly_url") or
                             "http://127.0.0.1:8022").rstrip("/")
        self.renderly_channel = studio.get("renderly_channel") or None
        self.renderly_upscale = studio.get("renderly_upscale") or 4
        self.imgtovideo_repo = studio.get("imgtovideo_repo") or None
        self.studio_manifest_brief = studio.get("manifest_brief") or None
        self.flow_driver_dir = studio.get("flow_driver_dir") or None
        self.flow_driver_url = studio.get("flow_driver_url") \
            or "http://127.0.0.1:8030"
        self.studio_dir = base_dir / "data" / "studio"


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
