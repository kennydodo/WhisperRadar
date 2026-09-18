"""Studio: human-in-the-loop YouTube production pipeline logic.

Every stage can be completed by an automated tool (if its hook is configured)
or by hand (paste text / upload files) - the human stays in charge.
"""

import json
import logging
import random
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("whisperradar")

OLLAMA_URL = "http://localhost:11434"

# random directives so regenerating a script produces a genuinely fresh take
VARIATION_ANGLES = [
    "Take a slightly different narrative angle than any previous version.",
    "Open with a different hook pattern than a question.",
    "Lead with the most surprising fact and restructure the beats around it.",
    "Use a more story-driven approach built on one concrete anecdote.",
    "Emphasize the practical steps more than the theory.",
    "Frame the topic as a mistake people make and reverse-engineer the fix.",
]


def variation_nudge() -> str:
    angle = random.choice(VARIATION_ANGLES)
    return (f"[VARIATION {random.randint(1000, 9999)}] {angle} "
            "Produce a fresh take: different wording and rhythm from any "
            "earlier attempt at this script.")
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".flac", ".ogg")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def prod_dir(cfg, pid: int) -> Path:
    """The production's working folder: a user-selected directory when the
    production has one, otherwise data\\studio\\<id>.

    Folders the app creates (or adopts while empty) get a marker file so
    delete knows they are safe to remove."""
    work_dir = None
    try:
        import sqlite3

        conn = sqlite3.connect(cfg.db_path)
        try:
            row = conn.execute(
                "SELECT work_dir FROM productions WHERE id = ?", (pid,)
            ).fetchone()
            if row and row[0]:
                work_dir = row[0]
        finally:
            conn.close()
    except Exception:
        pass
    d = (Path(work_dir).expanduser() if work_dir
         else cfg.studio_dir / str(pid))
    created = not d.exists()
    d.mkdir(parents=True, exist_ok=True)
    if created or not any(d.iterdir()):
        _write_marker(d)
    return d


MARKER = ".whisperradar-production"


def _write_marker(d: Path) -> None:
    try:
        (d / MARKER).touch(exist_ok=True)
    except OSError:
        pass


def is_managed_dir(cfg, pdir: Path) -> bool:
    """True when the production folder was created/adopted by WhisperRadar."""
    try:
        pdir.resolve().relative_to(cfg.studio_dir.resolve())
        return True
    except ValueError:
        return (pdir / MARKER).exists()


def validate_work_dir(cfg, new_dir: str | Path) -> Path:
    """Reject work_dir values that would let production delete wipe
    unrelated folders (drive roots, the program folder, non-empty
    folders WhisperRadar did not create)."""
    p = Path(new_dir).expanduser().resolve()
    if p.parent == p:
        raise RuntimeError(
            f"{p} is a drive root - pick a folder inside it instead")
    base = cfg.base_dir.resolve()
    if p == base:
        raise RuntimeError("work_dir cannot be the WhisperRadar folder")
    try:
        base.relative_to(p)
        raise RuntimeError(
            f"{p} contains the WhisperRadar program folder - not allowed")
    except ValueError:
        pass
    if p.exists() and any(p.iterdir()) and not (p / MARKER).exists():
        raise RuntimeError(
            f"{p} is not empty and is not a WhisperRadar production folder - "
            "pick an empty folder (or one WhisperRadar created before)")
    return p


MOVE_ITEMS = ["script.md", "style.md", "source_transcript.txt", "subtitles.srt",
              "shotlist.json", "shotlist.json.bak", "imgtovideo.json",
              "prompts.txt", "batch_sheet.txt", "final.mp4", "audio",
              "audio_previous", "images", "out", "script_versions", "versions"]


def move_production_dir(cfg, prod, new_dir: str | None) -> tuple[Path, int]:
    """Move a production's files to new_dir (None = default). Returns
    (final directory, number of items moved). Aborts with an error when
    the destination already contains any production artifact."""
    old = prod_dir(cfg, prod["id"])
    if new_dir:
        dest = Path(new_dir).expanduser().resolve()
        if dest.resolve() != old.resolve():
            dest = validate_work_dir(cfg, dest)
        else:
            _write_marker(dest)  # adopt the current folder as managed
    else:
        dest = cfg.studio_dir / str(prod["id"])
    dest.mkdir(parents=True, exist_ok=True)
    _write_marker(dest)
    if old.resolve() == dest.resolve():
        return dest, 0
    # refuse partial moves: if any artifact already exists at the destination,
    # abort with the full list instead of silently stranding items
    collisions = [item for item in MOVE_ITEMS
                  if (old / item).exists() and (dest / item).exists()]
    if collisions:
        raise RuntimeError(
            f"{dest} already contains: {', '.join(collisions)} - "
            "remove or rename those first, then move again")
    moved = 0
    for item in MOVE_ITEMS:
        src = old / item
        d = dest / item
        if src.exists():
            shutil.move(str(src), str(d))
            moved += 1
    return dest, moved


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
    imgs = [i for i in data.get("images", []) if isinstance(i, dict)]
    images = [i for i in imgs
              if Path(i.get("file", "")).stem.lower() in existing]
    dropped = {Path(i.get("file", "")).stem.lower() for i in imgs
               if Path(i.get("file", "")).stem.lower() not in existing}
    shots = [s for s in data.get("shots", []) if isinstance(s, dict)
             and Path(s.get("asset", "")).stem.lower() in existing]
    if not shots:
        raise RuntimeError("sanitizing the shotlist would remove every shot")
    removed = len(data.get("shots", [])) - len(shots)
    if removed == 0 and len(images) == len(imgs):
        return 0
    backup = path.with_suffix(".json.bak")
    if not backup.exists():  # keep the user's full plan recoverable
        shutil.copy(path, backup)
    data["images"] = images
    data["shots"] = shots
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    log.warning("shotlist sanitized: dropped %d shot(s) with missing images %s "
                "(original kept at shotlist.json.bak)",
                removed, sorted(dropped))
    return removed


def run_merge_render(cfg, pid_dir: Path) -> Path:
    """Render the final video with ImgToVideo.Cli (headless). Returns final path."""
    repo = cfg.imgtovideo_repo
    cli = Path(repo, "src", "ImgToVideo.Cli") if repo else None
    if not cli or not cli.exists():
        raise RuntimeError("Set studio.imgtovideo_repo in config.yaml")

    # project layout expectations: audio\narration.<ext>, *.srt at root.
    # Always refresh narration from the current audio artifact - a stale copy
    # here would shadow the real audio (the loader prefers audio\).
    audio_dir = pid_dir / "audio"
    audio_dir.mkdir(exist_ok=True)
    for old in audio_dir.glob("narration.*"):
        old.unlink()
    audio = find_audio(pid_dir)
    if audio:
        shutil.copy(audio, audio_dir / f"narration{audio.suffix}")
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
        data=json.dumps({"model": model, "prompt": prompt, "stream": False,
                         "options": {"temperature": 1.0}}).encode(),
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
        "temperature": 1.0,  # creative writing; regenerations must differ
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
                 word_count: int | None = None,
                 extra_direction: str = "") -> str:
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
    extra = (extra_direction or "").strip()
    if extra:
        extra = f"\nADDITIONAL DIRECTION FROM THE CREATOR (follow it):\n{extra}\n"
    return f"""You are a writing coach for a {genre} YouTube channel.
Analyze the WRITING STYLE of the transcript below (from a video titled "{title}").
{length_note}
TRANSCRIPT TO ANALYZE:
{text}
{extra}
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
                  style_guide: str = "", target_words: int = 1200,
                  variation: str = "", extra_direction: str = "") -> str:
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
    var_block = f"\n{variation}" if variation else ""
    extra = (extra_direction or "").strip()
    if extra:
        extra = f"\nADDITIONAL DIRECTION FROM THE CREATOR (follow it):\n{extra}\n"
    return f"""You are an original YouTube scriptwriter for a {genre} channel.

{style_block}

{facts_block}
{extra}
TASK: Write an original YouTube script titled "{title}".
{var_block}
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
                         style_guide: str = "",
                         extra_direction: str = "") -> str:
    style = (style_guide or "").strip()
    style_note = ""
    if style:
        style_note = f"\nVisual style should also reflect this writing style guide:\n{style[:3000]}"
    extra = (extra_direction or "").strip()
    if extra:
        extra = f"\nADDITIONAL DIRECTION FROM THE CREATOR (follow it):\n{extra}\n"
    return f"""Break this {genre} YouTube script into scenes for image generation.

Script:
{script_text[:12000]}
{style_note}{extra}
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


def shotlist_prompts(pid_dir: Path) -> list[str]:
    """Extract the image prompts (in shot order) from shotlist.json."""
    path = pid_dir / "shotlist.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    by_file = {i.get("file"): (i.get("prompt") or "").strip()
               for i in data.get("images", [])
               if isinstance(i, dict) and i.get("file")}
    return [by_file[s["asset"]] for s in data.get("shots", [])
            if isinstance(s, dict) and s.get("asset") in by_file
            and by_file[s["asset"]]]


def load_manifest_brief(cfg) -> str:
    """The ImgToVideo manifest-authoring brief: the master planning prompt
    that turns a narration SRT into shotlist.json + an image batch sheet.

    Loaded from disk on every use so edits to the brief take effect
    immediately. Path: studio.manifest_brief in config.yaml, or
    <imgtovideo_repo>\\docs\\manifest-authoring-brief.md by default."""
    path = None
    if cfg.studio_manifest_brief:
        p = Path(cfg.studio_manifest_brief)
        path = p if p.is_absolute() else cfg.base_dir / p
    elif cfg.imgtovideo_repo:
        path = (Path(cfg.imgtovideo_repo) / "docs"
                / "manifest-authoring-brief.md")
    if not path or not path.exists():
        raise RuntimeError(
            "manifest-authoring-brief.md not found - set studio.imgtovideo_repo "
            "or studio.manifest_brief in config.yaml")
    text = path.read_text(encoding="utf-8")
    if "\n---\n" in text:  # skip the how-to header, keep the prompt itself
        text = text.split("\n---\n", 1)[1]
    return text.strip()


def _extract_json_object(text: str) -> tuple[dict, str]:
    """Extract the first balanced JSON object (string-aware) plus the tail
    after it - tolerant of extra documents following the JSON."""
    start = text.find("{")
    if start == -1:
        raise RuntimeError("LLM returned no JSON object")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(text[start:i + 1])
                except ValueError as exc:
                    raise RuntimeError(f"shotlist JSON is invalid: {exc}")
                return data, text[i + 1:]
    raise RuntimeError("LLM returned an incomplete JSON object")


def parse_shotlist_output(text: str) -> tuple[dict, str]:
    """Parse the LLM's two-document output (manifest-authoring brief):
    shotlist.json first, optional IMAGE BATCH SHEET second.
    Returns (shotlist_data, batch_sheet_text)."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    data, tail = _extract_json_object(text)
    if not isinstance(data.get("shots"), list) or not data["shots"]:
        raise RuntimeError("shotlist has no shots[] entries")
    if not isinstance(data.get("images"), list) or not data["images"]:
        raise RuntimeError("shotlist has no images[] entries")
    for item in data["images"]:
        if not isinstance(item, dict) or not item.get("file") or not item.get("prompt"):
            raise RuntimeError("images[] entries need 'file' and 'prompt'")
    return data, tail.strip()


def shotlist_prompt(brief_text: str, srt_text: str, style_guide: str = "",
                    extra_direction: str = "") -> str:
    """Assemble the manifest-authoring brief with its three inputs:
    the full narration SRT, the channel visual style, and the creator's
    per-stage direction."""
    style = (style_guide or "").strip()
    style_block = (
        f"INPUT 2 - CHANNEL VISUAL STYLE INSTRUCTIONS:\n{style}"
        if style else
        "INPUT 2 - CHANNEL VISUAL STYLE INSTRUCTIONS:\n"
        "(none supplied - write a concise master visual style yourself)")
    extra = (extra_direction or "").strip()
    if extra:
        extra = f"\n\nINPUT 3 - CREATOR DIRECTION (follow it):\n{extra}"
    return f"""{brief_text.strip()}

---

INPUT 1 - THE FULL NARRATION SRT:
{srt_text.strip()}

{style_block}{extra}"""


# --------------------------------------------------- external tool hooks ---

def run_hook(command: str, subs: dict, timeout: int = 3600) -> None:
    """Run a configured external command, substituting {placeholders}.
    Every value is command-line quoted, so working folders or filenames
    containing spaces, & , ^ or % cannot inject extra commands."""
    cmd = command
    for key, val in subs.items():
        cmd = cmd.replace("{" + key + "}",
                          subprocess.list2cmdline([str(val)]))
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                            timeout=timeout)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "")[-400:]
        raise RuntimeError(f"command failed (exit {result.returncode}): {tail}")
