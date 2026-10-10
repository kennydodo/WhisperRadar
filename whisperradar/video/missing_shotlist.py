"""Port of ImgToVideo.Core.Manifest.MissingShotList: the missing-image delta of a
shotlist.json in two paste-ready shapes (JSON payload + plain text)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

from .shotlist import FILE_NAME

IMAGES_DIRECTORY_NAME = "images"


@dataclass
class MissingShotListResult:
    json: str
    plain_text: str
    count: int


def build_for_project(project_folder) -> Optional[MissingShotListResult]:
    """None when the project has no shotlist.json."""
    shotlist_path = os.path.join(project_folder, FILE_NAME)
    if not os.path.isfile(shotlist_path):
        return None
    images_dir = os.path.join(project_folder, IMAGES_DIRECTORY_NAME)
    present = []
    if os.path.isdir(images_dir):
        present = [f for f in os.listdir(images_dir)
                   if os.path.isfile(os.path.join(images_dir, f))]
    with open(shotlist_path, encoding="utf-8-sig") as fh:
        return build_from_json(fh.read(), present)


def build_from_json(json_text: str, present_files) -> MissingShotListResult:
    if present_files is None:
        raise ValueError("present_files")
    root = json.loads(json_text)
    if not isinstance(root, dict):
        raise ValueError("shotlist.json must be an object.")

    on_disk = {f.upper() for f in present_files}
    missing: list = []
    images = root.get("images")
    if isinstance(images, list):
        for image in images:
            file = None
            if isinstance(image, dict) and isinstance(image.get("file"), str):
                file = image["file"].strip()
            if not file or file.upper() in on_disk:
                continue
            missing.append(image)

    style = root["style"].strip() if isinstance(root.get("style"), str) else None
    result: dict = {}
    if style:
        result["style"] = style
    if isinstance(root.get("refs"), dict):
        result["refs"] = root["refs"]
    result["images"] = missing

    return MissingShotListResult(
        json.dumps(result, indent=2, ensure_ascii=False),
        _build_plain_text(style, missing),
        len(missing))


def _build_plain_text(style, missing) -> str:
    parts = []
    if style:
        parts.append(f"Master prompt: {style}\n")
        parts.append("\n")
    for node in missing:
        file = node.get("file") if isinstance(node.get("file"), str) else ""
        prompt = node.get("prompt") if isinstance(node.get("prompt"), str) else ""
        parts.append(f'"file": "{file}", "prompt": "{prompt}"\n')
    return "".join(parts).rstrip("\r\n")
