"""Thumbnail storage for a finished video.

The thumbnails themselves are made by Generate with GPT Pro (see
gpt_packaging.py): finished 1280x720 images with the words in the picture.
This module keeps only what that flow and the pages need: where the set is
stored (thumbnails/thumbs.json), the choice, and a phone-size contact sheet.
(The old concept writer/judge, image-engine art and Pillow text compositing
were removed once GPT replaced them.)
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import prodfiles

THUMB_DIR = "thumbnails"
THUMBS_FILE = "thumbs.json"
W, H = 1280, 720
MAX_BYTES = 2_000_000          # YouTube's thumbnail limit is 2 MB
TEXT_MAX_WORDS = 4
TEXT_MAX_CHARS = 28
LAYOUTS = {
    "character_host": "the character and the host together",
    "character": "the character alone",
    "host": "the host alone",
}


# ---- storage ---------------------------------------------------------------

def thumbs_dir(pdir) -> Path:
    return prodfiles.path(pdir, THUMB_DIR)


def empty_thumbs() -> dict:
    return {"concepts": [], "chosen": None, "status": "none", "score": None}


def load_thumbs(pdir) -> dict:
    try:
        data = json.loads((thumbs_dir(pdir) / THUMBS_FILE)
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_thumbs()
    base = empty_thumbs()
    if isinstance(data, dict):
        base.update(data)
    if not isinstance(base["concepts"], list):
        base["concepts"] = []
    return base


def save_thumbs(pdir, data: dict) -> None:
    d = thumbs_dir(pdir)
    d.mkdir(parents=True, exist_ok=True)
    (d / THUMBS_FILE).write_text(
        json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def clean_text(text) -> str:
    """The words on the thumbnail: trimmed, at most 4 words / 28 characters."""
    words = re.sub(r"\s+", " ", str(text or "")).strip().split(" ")
    words = [w for w in words if w][:TEXT_MAX_WORDS]
    out = " ".join(words)
    while len(out) > TEXT_MAX_CHARS and len(words) > 1:
        words.pop()
        out = " ".join(words)
    return out[:TEXT_MAX_CHARS].strip()


def contact_sheet(pdir, title: str = "") -> Path | None:
    """How the thumbnails look in a phone feed: each at ~170 px wide, on a
    dark and a light background, side by side."""
    from PIL import Image, ImageDraw
    pdir = Path(pdir)
    data = load_thumbs(pdir)
    finals = [pdir / c["final"] for c in data["concepts"]
              if c.get("final") and (pdir / c["final"]).is_file()]
    if not finals:
        return None
    tw, th, gap = 170, 96, 12
    cols = min(len(finals), 5)
    sheet_w = cols * (tw + gap) + gap
    sheet = Image.new("RGB", (sheet_w, 2 * (th + 2 * gap)), (15, 15, 15))
    draw = ImageDraw.Draw(sheet)
    draw.rectangle((0, th + 2 * gap, sheet_w, 2 * (th + 2 * gap)),
                   fill=(250, 250, 250))
    for row in (0, 1):
        for i, f in enumerate(finals):
            im = Image.open(f).convert("RGB").resize((tw, th), Image.LANCZOS)
            sheet.paste(im, (gap + i * (tw + gap),
                             row * (th + 2 * gap) + gap))
    out = thumbs_dir(pdir) / "sheet.jpg"
    sheet.save(out, "JPEG", quality=90)
    return out
