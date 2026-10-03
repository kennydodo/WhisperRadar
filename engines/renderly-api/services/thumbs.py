"""Cached server-side thumbnails for gallery images."""

import hashlib
from pathlib import Path

from PIL import Image

from config import BASE_DIR

THUMB_DIR = BASE_DIR / "storage" / "thumbs"
THUMB_LONG_SIDE = 160


def ensure_thumb(source: Path, key: str) -> Path:
    """Return a cached small PNG for the source image, creating it if needed."""
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    try:
        stat = source.stat()
        digest = hashlib.md5(f"{key}_{stat.st_size}_{int(stat.st_mtime)}".encode()).hexdigest()
    except OSError:
        digest = hashlib.md5(f"{key}".encode()).hexdigest()

    out = THUMB_DIR / f"{digest}.png"
    if out.exists():
        return out

    with Image.open(source) as img:
        img = img.convert("RGB")
        scale = min(1.0, THUMB_LONG_SIDE / max(img.width, img.height))
        size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
        img.resize(size, Image.LANCZOS).save(out, "PNG")
    return out
