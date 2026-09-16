"""Studio: human-in-the-loop YouTube production pipeline logic.

Every stage can be completed by an automated tool (if its hook is configured)
or by hand (paste text / upload files) - the human stays in charge.
"""

import json
import re
import subprocess
import urllib.request
from pathlib import Path

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


def script_prompt(title: str, genre: str, source_text: str,
                  target_words: int = 1200) -> str:
    facts = (source_text or "").strip()
    if len(facts) > 12000:
        facts = facts[:12000] + " ..."
    if facts:
        facts_block = f"FACTS gathered from research (use these, nothing else):\n{facts}"
    else:
        facts_block = "No research transcript available - write from the title alone."
    return f"""You are an original YouTube scriptwriter for a {genre} channel.

{facts_block}

TASK: Write an original YouTube script titled "{title}".

Rules:
- Use ONLY the facts above. Never reuse sentences, phrasing, or the structure of any source material.
- Hook the viewer in the first 15 seconds.
- About {target_words} words. Conversational, second person, no stage directions, no scene labels.
- End with a short call to action.

Output ONLY the script text."""


def image_prompts_prompt(script_text: str, genre: str) -> str:
    return f"""Break this {genre} YouTube script into scenes for image generation.

Script:
{script_text[:12000]}

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
