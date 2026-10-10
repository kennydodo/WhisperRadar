"""Port of ImgToVideo.Core.Models.RevealPaths (file names of generated reveal stills)."""
from __future__ import annotations

import hashlib
import os

from . import models as m


def step_path(source_path: str, k: int, count: int, layout: str = m.REVEAL_ROW) -> str:
    if k >= count:
        return source_path
    full = os.path.abspath(source_path)
    stem = os.path.splitext(os.path.basename(full))[0]
    tag = "grid" if m.reveal_layout_normalize(layout) == m.REVEAL_GRID else "reveal"
    return os.path.join(_reveal_directory(full), f"{stem}_{tag}{k}of{count}.png")


def stack_path(sources, k: int, layout: str) -> str:
    first = os.path.abspath(sources[0])
    key = ("|".join(os.path.basename(s).lower() for s in sources)
           + "|" + m.reveal_layout_normalize(layout))
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:6].lower()
    stem = os.path.splitext(os.path.basename(first))[0]
    return os.path.join(_reveal_directory(first), f"{stem}_stack{digest}_{k}of{len(sources)}.png")


def project_directory(image_path: str) -> str:
    full = os.path.abspath(image_path)
    images_dir = os.path.dirname(full) or "."
    return os.path.dirname(images_dir) or images_dir


def _reveal_directory(full_image_path: str) -> str:
    return os.path.join(project_directory(full_image_path), "out", "reveal")
