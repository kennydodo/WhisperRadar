"""Studio: human-in-the-loop YouTube production pipeline logic.

Every stage can be completed by an automated tool (if its hook is configured)
or by hand (paste text / upload files) - the human stays in charge.
"""

import json
import logging
import re
import shutil
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
    for candidate in (pid_dir / "out" / "final" / "final.mp4",
                      pid_dir / "final.mp4"):
        if candidate.exists():
            return candidate
    return None


# ------------------------------------------------- Renderly / ImgToVideo --

def renderly_ready(url: str, timeout: int = 2) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/channels", timeout=timeout):
            return True
    except Exception:
        return False


def ensure_renderly_channel(cfg) -> int:
    """Find or create the 'whisperradar' channel in Renderly. Returns its id."""
    if cfg.renderly_channel:
        return int(cfg.renderly_channel)
    with urllib.request.urlopen(f"{cfg.renderly_url}/api/channels",
                                timeout=10) as r:
        channels = json.loads(r.read())
    for ch in channels:
        if ch.get("name") == "whisperradar":
            return ch["id"]
    req = urllib.request.Request(
        f"{cfg.renderly_url}/api/channels",
        data=json.dumps({"name": "whisperradar",
                         "description": "Studio batch image generations"}
                        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["id"]


def run_imagegen(cfg, pid_dir: Path) -> int:
    """Render the production's shotlist images through ImgToVideo.ImageGen
    in Renderly mode. Returns how many new images landed in images\\."""
    repo = cfg.imgtovideo_repo
    if not repo or not Path(repo, "src", "ImgToVideo.ImageGen").exists():
        raise RuntimeError("Set studio.imgtovideo_repo in config.yaml")
    channel = ensure_renderly_channel(cfg)
    before = {p.name for p in (pid_dir / "images").iterdir()} \
        if (pid_dir / "images").exists() else set()
    cmd = [
        "dotnet", "run", "--project",
        str(Path(repo, "src", "ImgToVideo.ImageGen")),
        "-c", "Release", "--",
        str(pid_dir),
        "--renderly", cfg.renderly_url,
        "--channel", str(channel),
        "--image-size", "1K",
        "--upscale", str(cfg.renderly_upscale),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "")[-500:]
        raise RuntimeError(f"ImageGen failed (exit {result.returncode}): {tail}")
    img_dir = pid_dir / "images"
    new = [p.name for p in img_dir.iterdir() if p.name not in before]
    return len(new)


def sanitize_shotlist(pid_dir: Path) -> int:
    """Drop shotlist images/shots whose files were never generated
    (e.g. failed on API quota) so the render can proceed with what exists.
    Returns how many shots were dropped."""
    path = pid_dir / "shotlist.json"
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    img_dir = pid_dir / "images"
    existing = {p.stem.lower() for p in img_dir.iterdir()
                if p.is_file()} if img_dir.exists() else set()
    images = [i for i in data.get("images", [])
              if Path(i.get("file", "")).stem.lower() in existing]
    dropped = {Path(i["file"]).stem.lower() for i in data.get("images", [])
               if Path(i.get("file", "")).stem.lower() not in existing}
    shots = [s for s in data.get("shots", [])
             if Path(s.get("asset", "")).stem.lower() in existing]
    if not shots:
        raise RuntimeError("sanitizing the shotlist would remove every shot")
    removed = len(data.get("shots", [])) - len(shots)
    if removed == 0 and len(images) == len(data.get("images", [])):
        return 0
    data["images"] = images
    data["shots"] = shots
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    log.warning("shotlist sanitized: dropped %d shot(s) with missing images %s",
                removed, sorted(dropped))
    return removed


def run_merge_render(cfg, pid_dir: Path) -> Path:
    """Render the final video with ImgToVideo.Cli (headless). Returns final path."""
    repo = cfg.imgtovideo_repo
    cli = Path(repo, "src", "ImgToVideo.Cli") if repo else None
    if not cli or not cli.exists():
        raise RuntimeError("Set studio.imgtovideo_repo in config.yaml")

    # project layout expectations: audio\narration.<ext>, *.srt at root
    audio_dir = pid_dir / "audio"
    audio_dir.mkdir(exist_ok=True)
    audio = find_audio(pid_dir)
    if audio and not (audio_dir / "narration.mp3").exists():
        shutil.copy(audio, audio_dir / "narration.mp3")
    sanitize_shotlist(pid_dir)

    cmd = [
        "dotnet", "run", "--project", str(cli),
        "-c", "Release", "--", "render-final", str(pid_dir),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=14400)
    if result.returncode != 0:
        tail = ((result.stderr or "") + (result.stdout or ""))[-600:]
        raise RuntimeError(f"ImgToVideo.Cli failed (exit {result.returncode}): {tail}")
    final = find_final(pid_dir)
    if not final:
        raise RuntimeError("render finished but no final.mp4 found")
    return final


def prepare_project_folder(cfg, pid: int) -> Path:
    """Make the production folder a valid ImgToVideo project folder."""
    pdir = prod_dir(cfg, pid)
    options_file = pdir / "imgtovideo.json"
    if not options_file.exists():
        options_file.write_text(json.dumps({
            "schema_version": 1,
            "naming": {"image_extensions": [".png", ".jpg", ".jpeg", ".webp"]},
        }, indent=2), encoding="utf-8")
    return pdir


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


def style_prompt(title: str, genre: str, source_text: str,
                 word_count: int | None = None) -> str:
    text = (source_text or "").strip()
    if len(text) > 12000:
        text = text[:12000] + " ..."
    if not text:
        raise RuntimeError(
            "No source transcript available - write the style guide manually"
        )
    length_note = ""
    if word_count:
        length_note = (f"\nThe transcript is about {word_count} words "
                       f"(~{max(1, round(word_count / 150))} minutes of "
                       f"narration) - reflect this in the Structure section.")
    return f"""You are a writing coach for a {genre} YouTube channel.
Analyze the WRITING STYLE of the transcript below (from a video titled "{title}").
{length_note}
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


def parse_shotlist_json(text: str) -> dict:
    """Parse the LLM's shotlist output: strips fences, extracts the object."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError("LLM returned no JSON object")
    data = json.loads(text[start:end + 1])
    if not isinstance(data.get("images"), list) or not data["images"]:
        raise RuntimeError("shotlist has no images[] entries")
    if not isinstance(data.get("shots"), list) or not data["shots"]:
        raise RuntimeError("shotlist has no shots[] entries")
    for item in data["images"]:
        if not isinstance(item, dict) or not item.get("file") or not item.get("prompt"):
            raise RuntimeError("images[] entries need 'file' and 'prompt'")
    return data


def shotlist_prompt(script_text: str, srt_numbered: str, style_guide: str,
                    extra_direction: str = "") -> str:
    style = (style_guide or "").strip()
    style_block = f"MASTER VISUAL STYLE (put this verbatim in the \"style\" field):\n{style[:4000]}" \
        if style else "No style guide provided - write a concise master visual style."
    extra = (extra_direction or "").strip()
    if extra:
        extra = f"\nADDITIONAL DIRECTION FROM THE CREATOR (follow it):\n{extra}\n"
    return f"""You are the shot planner for a faceless YouTube video. Plan the images
from the script and the subtitle cues below.

Output ONLY a JSON object (no markdown fences) with this exact shape:
{{
  "style": "<master visual style for every image>",
  "images": [ {{ "file": "S01_01_SCN_ST.png", "prompt": "<image prompt>" }} ],
  "shots": [ {{ "shot_id": "s1", "cues": "1-3", "asset": "S01_01_SCN_ST.png" }} ]
}}

Rules:
- "images[]" holds every image: "file" follows the naming convention
  S##_##_TYPE_MOTION.png (SCN=scene, CU=close-up, INF=infographic, CMP=comparison,
  PROC=process, HYB=hybrid, OVR=overview; motion ST/ZI/ZO/PL/PR/PD/PV - two digits,
  uppercase, .png lowercase). Write a rich prompt per image; portrait-quality,
  cinematic 16:9 composition with 120% overscan margin for the motion.
- "shots[]" maps each image to subtitle cue ranges ("1-3") so every cue is
  covered exactly once, in order, with no gaps and no overlaps.
- "style" is the master visual style shared by all images.
{extra}
SCRIPT:
{script_text[:12000]}

SUBTITLE CUES (index: text):
{srt_numbered[:12000]}

{style_block}"""


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
