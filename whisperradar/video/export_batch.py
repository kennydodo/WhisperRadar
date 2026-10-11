"""`export-batch` command of ImgToVideo.Cli: shotlist images[] minus what is already in
images/, written as out/image-batch.json (master prompt + canvas/aspect per motion code)."""
from __future__ import annotations

import json
import os
from typing import Optional

from . import models as m
from . import shotlist

# Pixel size to generate at (enough overscan for a real pan/zoom) and the aspect string
# for the paid Gemini path (only PL/PR/PV need 21:9; PU/PD stay 16:9 on purpose).
CANVAS = {
    "ST": "2304x1296", "ZI": "2304x1296", "ZO": "2304x1296",
    "PL": "3024x1296", "PR": "3024x1296",
    "PU": "2304x2304", "PD": "2304x2304",
    "PV": "3840x1296",
}
ASPECT = {
    "ST": "16:9", "ZI": "16:9", "ZO": "16:9",
    "PL": "21:9", "PR": "21:9",
    "PU": "16:9", "PD": "16:9",
    "PV": "21:9",
}
NOTE = ("Missing images only — regenerate into images\\. "
        "Master prompt goes in the batch app's master box once.")


def export_batch(project_folder, options=None) -> dict:
    """Returns {"path", "missing_count", "plan_count", "issues"}.
    Raises FileNotFoundError when there is no shotlist.json, ValueError on a broken one."""
    folder = os.path.abspath(project_folder)
    shotlist_path = os.path.join(folder, shotlist.FILE_NAME)
    if not os.path.isfile(shotlist_path):
        raise FileNotFoundError("no shotlist.json in the project folder")
    with open(shotlist_path, encoding="utf-8-sig") as fh:
        text = fh.read()

    issues: list = []
    sl = getattr(options, "shotlist", None)
    shotlist.parse(text, issues,
                   sl.master_prompt_max_chars if sl else 0,
                   sl.prompt_max_chars if sl else 0)

    data = json.loads(text)
    plan_images = data.get("images")
    if not isinstance(plan_images, list):
        raise ValueError('shotlist.json has no "images" array.')

    images_dir = os.path.join(folder, "images")
    on_disk = set()
    if os.path.isdir(images_dir):
        on_disk = {f.upper() for f in os.listdir(images_dir)
                   if f.lower().endswith(".png") and os.path.isfile(os.path.join(images_dir, f))}

    missing: list = []
    for img in plan_images:
        file = img.get("file") if isinstance(img, dict) else None
        file = file if isinstance(file, str) else ""
        if file.upper() in on_disk:
            continue
        motion = (file[:-4] if file.lower().endswith(".png") else file).split("_")[-1]
        prompt = img.get("prompt")
        missing.append({
            "file": file,
            "canvas": CANVAS.get(motion, "2304x1296"),
            "aspect": ASPECT.get(motion, "16:9"),
            "motion": motion,
            "prompt": prompt if isinstance(prompt, str) else "",
        })

    style = data.get("style")
    batch = {
        "note": NOTE,
        "master_prompt": style if isinstance(style, str) else "",
        "count": len(missing),
        "images": missing,
    }
    out_dir = os.path.join(folder, "out")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "image-batch.json")
    with open(path, "w", encoding="utf-8", newline="") as fh:   # UTF-8 without BOM
        fh.write(json.dumps(batch, indent=2, ensure_ascii=False))
    return {"path": path, "missing_count": len(missing), "plan_count": len(plan_images),
            "issues": issues}
