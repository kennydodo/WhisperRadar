"""Storyboard editor for plan.json (the scene plan an explainer production is drawn from).

The page lists every scene with its layout, title, footer and items; you can
reword, change an icon or the narration words an item appears on, switch the
layout, reorder or delete scenes. Anything else a scene carries (compare sides,
memory capacity, chart numbers...) stays editable as a small JSON box, so no
key is ever lost. A save is checked the same way the scene planner checks its
own plan, and a plan with real faults is never written.
"""
from __future__ import annotations

import json
import os
import shutil

from .video.explainer import recipes

KNOWN = ("id", "start", "layout", "title", "footer", "items")
ITEM_KNOWN = ("label", "icon", "at")
# pacing notes that are advice, not faults: editing is allowed to go past them
SOFT = ("scenes for", "is too many for")


def layouts() -> list:
    return list(recipes.LAYOUTS) + [l for l in recipes.REMOTION_LAYOUTS if l not in recipes.LAYOUTS]


def _dump(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False, indent=1) if d else ""


def rows(plan: dict) -> list:
    """The plan's scenes as flat dicts for the form."""
    out = []
    for i, s in enumerate(plan.get("scenes") or []):
        if not isinstance(s, dict):
            continue
        items = []
        for it in s.get("items") or []:
            if not isinstance(it, dict):
                continue
            items.append({"label": it.get("label", ""), "icon": it.get("icon", ""), "at": it.get("at", ""),
                          "extra": _dump({k: v for k, v in it.items() if k not in ITEM_KNOWN})})
        out.append({"n": i, "id": s.get("id", f"scene{i + 1}"), "start": s.get("start", ""),
                    "layout": s.get("layout", ""), "title": s.get("title", ""), "footer": s.get("footer", ""),
                    "items": items, "extra": _dump({k: v for k, v in s.items() if k not in KNOWN})})
    return out


def _json_dict(text: str, where: str, errors: list) -> dict:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        d = json.loads(text)
    except ValueError as exc:
        errors.append(f"{where}: the other-settings box is not valid JSON ({exc})")
        return {}
    if not isinstance(d, dict):
        errors.append(f"{where}: the other-settings box must be a JSON object {{...}}")
        return {}
    return d


def plan_from_form(form, base: dict) -> tuple:
    """(new plan, errors). The base plan supplies every top-level key but the scenes."""
    errors = []
    try:
        count = int(form.get("scene_count") or 0)
    except ValueError:
        count = 0
    scenes = []
    for i in range(count):
        p = f"s{i}_"
        if form.get(p + "delete"):
            continue
        sid = (form.get(p + "id") or "").strip() or f"scene{i + 1}"
        where = f"Scene {sid}"
        s = {"id": sid}
        for key in ("start", "layout", "title", "footer"):
            v = (form.get(p + key) or "").strip()
            if v:
                s[key] = v
        items = []
        labels = form.getlist(p + "item_label")
        icons = form.getlist(p + "item_icon")
        ats = form.getlist(p + "item_at")
        extras = form.getlist(p + "item_extra")
        for j, label in enumerate(labels):
            label = label.strip()
            if not label:
                continue
            it = {"label": label}
            icon = (icons[j] if j < len(icons) else "").strip()
            at = (ats[j] if j < len(ats) else "").strip()
            if icon:
                it["icon"] = icon
            if at:
                it["at"] = at
            it.update(_json_dict(extras[j] if j < len(extras) else "", f"{where}, item {j + 1}", errors))
            items.append(it)
        if items:
            s["items"] = items
        extra = _json_dict(form.get(p + "extra"), where, errors)
        for k, v in extra.items():
            if k not in KNOWN:
                s[k] = v
        try:
            order = float(form.get(p + "order") or i + 1)
        except ValueError:
            order = i + 1
        scenes.append((order, i, s))
    scenes.sort(key=lambda t: (t[0], t[1]))
    plan = {k: v for k, v in base.items() if k != "scenes"}
    plan["scenes"] = [s for _, _, s in scenes]
    if not plan["scenes"]:
        errors.append("A plan needs at least one scene.")
    return plan, errors


def faults(plan: dict, cues: list, total_seconds: float, look: dict = None) -> tuple:
    """(blocking faults, advice) from the scene planner's own checker."""
    from . import explainer_scenes as es
    found = es.check(plan, cues, total_seconds, look)
    hard = [f for f in found if not any(m in f for m in SOFT)]
    soft = [f for f in found if f not in hard]
    return hard, soft


def save(pdir, plan: dict) -> str:
    """Write plan.json, keeping the previous one as plan_before_edit.json. Returns the backup path."""
    path = os.path.join(str(pdir), "plan.json")
    backup = os.path.join(str(pdir), "plan_before_edit.json")
    if os.path.isfile(path):
        shutil.copyfile(path, backup)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(plan, ensure_ascii=False, indent=1) + "\n")
    os.replace(tmp, path)
    return backup
