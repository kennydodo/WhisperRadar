"""Configuration loading. Paths in config.yaml are relative to its directory."""

import re  # noqa: F401  (kept for callers that import it from this module)
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
        # On a channel's first sync, import its full uploads history (the RSS
        # feed only lists the latest 15). New rows land in the backlog.
        self.history_backfill = bool(raw.get("history_backfill", True))
        _backfill_limit = raw.get("history_backfill_limit")
        self.history_backfill_limit = int(_backfill_limit) if _backfill_limit else None

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
        # LLM providers and the default LLM live in the database (Settings >
        # LLM); the settings table is the single source of truth. The legacy
        # single-LLM keys are kept as empty attributes for compatibility and
        # are never read for LLM selection.
        self.studio_llm = "openai"
        self.studio_llm_base_url = None
        self.studio_llm_api_key = None
        self.studio_llm_model = None
        self.studio_llm_providers = []
        self.studio_llm_default = None

        self.studio_tts_command = studio.get("tts_command") or None
        # OpenSpeaker (ai33.pro) narration: optional config key/voice; the
        # key can also come from the WR_AI33_API_KEY env variable.
        self.studio_ai33_api_key = studio.get("ai33_api_key") or None
        self.studio_ai33_voice = studio.get("ai33_voice") or None
        self.studio_ai33_base_url = studio.get("ai33_base_url") or None
        # Shortlist shown in the audio-stage dropdown; empty = full catalog
        self.studio_ai33_voices = [str(v).strip()
                                   for v in (studio.get("ai33_voices") or [])
                                   if str(v).strip()]
        # Voice picker source: "favorites" lists the voices starred in the
        # OpenSpeaker app; anything else/absent uses the shortlist above
        # (or the full catalog when the shortlist is empty).
        self.studio_ai33_voice_source = (
            (studio.get("ai33_voice_source") or "").strip().lower() or None)
        self.studio_imagegen_command = studio.get("imagegen_command") or None
        self.studio_merge_command = studio.get("merge_command") or None
        self.studio_script_words = studio.get("script_words") or None
        self.renderly_url = (studio.get("renderly_url") or
                             "http://127.0.0.1:8022").rstrip("/")
        self.renderly_channel = studio.get("renderly_channel") or None
        # 2 = the 2K preset (2560x1440), the smallest tier that meets
        # ImgToVideo's 2304x1296 canvas spec. A configured 0 (off) must
        # survive the lookup, so no truthiness shortcut here.
        self.renderly_upscale = 2 if studio.get("renderly_upscale") is None \
            else studio.get("renderly_upscale")
        self.imgtovideo_repo = studio.get("imgtovideo_repo") or None
        # FlowBatch (the standalone Playwright Flow CLI) - consumed in
        # place from its own checkout, like ImgToVideo and Renderly.
        self.flowbatch_repo = studio.get("flowbatch_repo") or None
        self.flowbatch_project_url = \
            (studio.get("flowbatch_project_url") or "").strip() or None
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
