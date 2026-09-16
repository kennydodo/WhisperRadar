"""Studio: human-in-the-loop YouTube production pipeline logic.

Every stage can be completed by an automated tool (if its hook is configured)
or by hand (paste text / upload files) - the human stays in charge.
"""

import json
import logging
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("whisperradar")

OLLAMA_URL = "http://localhost:11434"
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".flac", ".ogg")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def prod_dir(cfg, pid: int) -> Path:
    d = cfg.studio_dir / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_audio(pid_dir: Path) -> Path | None:
    for ext in AUDIO_EXTS:
        p = pid_dir / f"audio{ext}"
        if p.exists():
            return p
    return None


def find_srt(pid_dir: Path) -> Path | None:
    p = pid_dir / "subtitles.srt"
    return p if p.exists() else None


def find_script(pid_dir: Path) -> Path | None:
    p = pid_dir / "script.md"
    return p if p.exists() else None


def find_style(pid_dir: Path) -> Path | None:
    p = pid_dir / "style.md"
    return p if p.exists() else None


def find_source_transcript(pid_dir: Path) -> Path | None:
    p = pid_dir / "source_transcript.txt"
    return p if p.exists() else None


def find_prompts(pid_dir: Path) -> Path | None:
    p = pid_dir / "prompts.txt"
    return p if p.exists() else None


def find_images(pid_dir: Path) -> list[Path]:
    img_dir = pid_dir / "images"
    if not img_dir.exists():
        return []
    return sorted(p for p in img_dir.iterdir()
                  if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


def find_final(pid_dir: Path) -> Path | None:
    p = pid_dir / "final.mp4"
    return p if p.exists() else None


# ------------------------------------------------------------------ ollama --

def ollama_models(timeout: int = 3) -> list[str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=timeout) as r:
            data = json.loads(r.read())
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def ollama_generate(model: str, prompt: str, timeout: int = 1800) -> str:
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=json.dumps({"model": model, "prompt": prompt, "stream": False}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()).get("response", "").strip()


def openai_generate(cfg, prompt: str, timeout: int = 600) -> str:
    """Call the configured LLM (provider list or legacy single-LLM config)."""
    provider = llm_provider(cfg, None)
    return openai_chat(provider, prompt, timeout=timeout)


def _resolve_provider(cfg, name: str | None = None) -> dict:
    if cfg.studio_llm_providers:
        name = name or cfg.studio_llm_default
        for p in cfg.studio_llm_providers:
            if p["name"] == name:
                return p
        raise RuntimeError(f"Unknown LLM provider '{name}'")
    # legacy single-LLM config
    if cfg.studio_llm == "openai":
        return {"name": "llm", "base_url": cfg.studio_llm_base_url,
                "api_key": cfg.studio_llm_api_key,
                "model": cfg.studio_llm_model, "env_key": "WR_LLM_API_KEY"}
    raise RuntimeError("No LLM providers configured (studio.llm_providers)")


def _provider_key(p: dict) -> str | None:
    import os

    key = (p.get("api_key") or os.environ.get(p.get("env_key") or "")
           or os.environ.get("WR_LLM_API_KEY") or "").strip()
    return key or None


def provider_ready(cfg, name: str | None = None) -> bool:
    try:
        p = _resolve_provider(cfg, name)
    except RuntimeError:
        return False
    return bool(p["base_url"] and p["model"] and _provider_key(p))


def openai_chat(p: dict, prompt: str, timeout: int = 600) -> str:
    import http.client

    key = _provider_key(p)
    if not key:
        raise RuntimeError(
            f"No API key for '{p['name']}' (set api_key in config.yaml or "
            f"{p['env_key']} env variable)"
        )
    if not p["base_url"] or not p["model"]:
        raise RuntimeError(f"Provider '{p['name']}' needs base_url and model")
    url = p["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": p["model"],
        "stream": True,  # streaming keeps gateways from timing out long completions
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "Accept": "text/event-stream",
        },
        method="POST",
    )

    last_exc = None
    for attempt in range(2):
        try:
            parts = []
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ctype = r.headers.get("Content-Type", "")
                if "event-stream" not in ctype:
                    data = json.loads(r.read())
                    return data["choices"][0]["message"]["content"].strip()
                for raw in r:
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        delta = json.loads(chunk)["choices"][0].get("delta", {})
                    except (ValueError, KeyError, IndexError):
                        continue
                    parts.append(delta.get("content") or "")
            text = "".join(parts).strip()
            if text:
                return text
            raise RuntimeError("LLM returned an empty response")
        except RuntimeError:
            raise
        except (urllib.error.URLError, http.client.HTTPException,
                ConnectionError, TimeoutError, OSError) as exc:
            last_exc = exc
            log.warning("LLM connection error (attempt %d): %s", attempt + 1, exc)
    raise RuntimeError(f"LLM connection failed after retry: {last_exc}")
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected LLM response: {data}") from exc


def openai_chat_raw(p: dict, prompt: str, timeout: int = 600) -> tuple[int, str]:
    """Like openai_chat but returns (status, body) instead of raising on HTTP errors."""
    key = _provider_key(p) or ""
    url = (p.get("base_url") or "").rstrip("/") + "/chat/completions"
    payload = {"model": p.get("model"), "stream": False,
               "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")


def llm_provider(cfg, name: str | None) -> dict:
    return _resolve_provider(cfg, name)


def llm_generate(cfg, prompt: str, timeout: int = 1800,
                 provider: str | None = None) -> str:
    if not cfg.studio_llm_providers and cfg.studio_llm == "ollama":
        models = ollama_models()
        if not models:
            raise RuntimeError("Ollama is not reachable at localhost:11434")
        model = models[0]
        for m in models:
            if cfg.ollama_model and m.startswith(cfg.ollama_model):
                model = m
                break
        return ollama_generate(model, prompt, timeout=timeout)
    return openai_chat(_resolve_provider(cfg, provider), prompt, timeout=timeout)


def llm_label(cfg, provider: str | None = None) -> str:
    try:
        p = _resolve_provider(cfg, provider)
        return f"{p['name']} ({p['model']})"
    except RuntimeError:
        if not cfg.studio_llm_providers and cfg.studio_llm == "ollama":
            return "local LLM (Ollama)"
        return "LLM not configured"


# ------------------------------------------------------ "not a copycat" ----

def _ngrams(text: str, n: int = 5) -> set:
    words = re.findall(r"\w+", text.lower())
    return {tuple(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def overlap_ratio(script: str, source: str) -> float:
    """Share of the script's 5-grams that also appear in the source text."""
    a, b = _ngrams(script), _ngrams(source or "")
    if not a:
        return 0.0
    return len(a & b) / len(a)


def style_prompt(title: str, genre: str, source_text: str) -> str:
    text = (source_text or "").strip()
    if len(text) > 12000:
        text = text[:12000] + " ..."
    if not text:
        raise RuntimeError(
            "No source transcript available - write the style guide manually"
        )
    return f"""You are a writing coach for a {genre} YouTube channel.
Analyze the WRITING STYLE of the transcript below (from a video titled "{title}").

TRANSCRIPT TO ANALYZE:
{text}

Produce a STYLE GUIDE in markdown with exactly these sections:
## Voice & Tone
## Pacing & Rhythm
## Sentence Style
## Hook Pattern
## Structure (beats in order, with rough timing)
## CTA Style
## Vocabulary & Register
## Things to Avoid

Rules:
- Describe patterns abstractly (e.g. "short punchy sentences, averages 8-12 words").
- Do NOT quote, copy, or paraphrase any phrase or sentence from the transcript.
- Be concrete enough that another writer could imitate the style without ever seeing the transcript.

Output ONLY the style guide markdown."""


def script_prompt(title: str, genre: str, source_text: str,
                  style_guide: str = "", target_words: int = 1200) -> str:
    facts = (source_text or "").strip()
    if len(facts) > 12000:
        facts = facts[:12000] + " ..."
    if facts:
        facts_block = f"FACTS gathered from research (use these, nothing else):\n{facts}"
    else:
        facts_block = "No research transcript available - write from the title alone."
    style = (style_guide or "").strip()
    if style:
        style_block = f"""STYLE GUIDE (match this exactly - tone, pacing, rhythm,
hook pattern, structure, and CTA style all come from it):
{style[:6000]}"""
    else:
        style_block = "No style guide provided."
    return f"""You are an original YouTube scriptwriter for a {genre} channel.

{style_block}

{facts_block}

TASK: Write an original YouTube script titled "{title}".

Rules:
- Follow the STYLE GUIDE above precisely. The script must feel like it was
  written by the writer described there.
- Use ONLY the facts above. Never reuse sentences, phrasing, or the structure
  of any source material - only the style is shared.
- Hook the viewer in the first 15 seconds, following the style guide's hook pattern.
- About {target_words} words. Conversational, second person, no stage directions, no scene labels.
- End with a short call to action matching the style guide's CTA style.

Output ONLY the script text."""


def image_prompts_prompt(script_text: str, genre: str,
                         style_guide: str = "") -> str:
    style = (style_guide or "").strip()
    style_note = ""
    if style:
        style_note = f"\nVisual style should also reflect this writing style guide:\n{style[:3000]}"
    return f"""Break this {genre} YouTube script into scenes for image generation.

Script:
{script_text[:12000]}
{style_note}
For each scene output exactly ONE line:
IMAGE: <detailed image prompt, cinematic 16:9, consistent characters and style, no text inside the image>

Output ONLY the IMAGE: lines, in script order."""


def parse_image_prompts(text: str) -> list[str]:
    prompts = []
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("image:"):
            line = line[6:].strip()
        if line:
            prompts.append(line)
    return prompts


# --------------------------------------------------- external tool hooks ---

def run_hook(command: str, subs: dict, timeout: int = 3600) -> None:
    """Run a configured external command, substituting {placeholders}."""
    cmd = command
    for key, val in subs.items():
        cmd = cmd.replace("{" + key + "}", str(val))
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                            timeout=timeout)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "")[-400:]
        raise RuntimeError(f"command failed (exit {result.returncode}): {tail}")
