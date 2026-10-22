"""Elements: the recurring characters, places and objects of a script, with ONE
fixed visual description each.

Experiment (branch exp/script-first-plan). A chat reads script.md and lists the
things that come back in the video; the list is saved next to the script as
elements.json (data) and elements.md (what the shotlist planner reads), and the
planner is told to describe each element exactly the way the list does. Nothing
here runs unless somebody starts the "elements" step, so a production without
elements.md plans exactly as before."""
from __future__ import annotations

import json
import re
from pathlib import Path

KINDS = ("character", "place", "object")
JSON_FILE = "elements.json"
MD_FILE = "elements.md"
MAX_ELEMENTS = 12
MAX_DESCRIPTION = 400


def prompt(script: str, style: str = "") -> str:
    """The message that asks a chat for the element list."""
    style = (style or "").strip()
    style_block = (f"\n\nCHANNEL VISUAL STYLE (every description must fit it):\n{style}"
                   if style else "")
    return (
        "You are preparing the visual plan of a narrated explainer video. Read the "
        "SCRIPT below and list the ELEMENTS: the characters, places and objects that "
        "come back in the video or are central to it, so every image can show them "
        "the same way.\n\n"
        "Rules:\n"
        "- Only things that come back in several DIFFERENT parts of the script, or "
        "that the whole video is about. No one-off props, no abstract ideas, nothing "
        "that is mentioned in only one passage.\n"
        f"- At most {MAX_ELEMENTS} elements - fewer is better; the most important "
        "first. Fewer, clearly different elements beat a long list.\n"
        "- MERGE variants into ONE element: different species, sizes, ages or "
        "versions of the same thing, and products made of the same material, are one "
        "entry (name the group, put the one shared look in the description). Two "
        "elements must never look nearly the same.\n"
        "- `kind` is exactly one of: character, place, object.\n"
        "- `description` is ONE fixed visual description (one or two sentences, at "
        f"most {MAX_DESCRIPTION} characters): what it looks like - shape, colours, "
        "clothing or materials, size, one distinguishing detail. Describe only what "
        "can be drawn. No names written in the picture, no text, no numbers.\n"
        "- Do not invent facts the script does not support; a visual choice (a colour, "
        "a clothing item) is fine, a new story fact is not.\n"
        "- Use the name the script uses.\n\n"
        "Reply with ONLY one JSON object, no commentary, in one ```json block:\n"
        '{"elements": [{"name": "...", "kind": "character|place|object", '
        '"description": "..."}]}'
        + style_block + "\n\nSCRIPT:\n" + (script or "").strip() + "\n")


def parse(reply: str) -> list[dict]:
    """The element list in a chat reply, cleaned: unknown kinds dropped, names
    de-duplicated, descriptions trimmed. [] when nothing usable is in the reply."""
    data = _json_object(reply)
    items = data.get("elements") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        name = re.sub(r"\s+", " ", str(it.get("name") or "")).strip()
        kind = str(it.get("kind") or "").strip().lower()
        desc = re.sub(r"\s+", " ", str(it.get("description") or "")).strip()
        if not name or not desc or kind not in KINDS:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "kind": kind, "description": desc[:MAX_DESCRIPTION]})
        if len(out) >= MAX_ELEMENTS:
            break
    return out


def _json_object(text: str):
    text = (text or "").strip()
    text = re.sub(r"^(json|copy|download)\s*$", "", text, flags=re.I | re.M)
    text = re.sub(r"```[a-z]*", "", text)
    pairs = sorted((("{", "}"), ("[", "]")),
                   key=lambda p: text.find(p[0]) if p[0] in text else len(text))
    for opener, closer in pairs:         # whichever starts first: a bare list or an object
        start = text.find(opener)
        while start != -1:
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
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except ValueError:
                            break
            start = text.find(opener, start + 1)
    return None


def render(elements: list[dict]) -> str:
    """elements.md: the list the shotlist planner reads."""
    lines = []
    for kind, title in (("character", "Characters"), ("place", "Places"),
                        ("object", "Objects")):
        rows = [e for e in elements if e["kind"] == kind]
        if rows:
            lines.append(f"## {title}")
            lines += [f"- **{e['name']}**: {e['description']}" for e in rows]
            lines.append("")
    return "\n".join(lines).strip() + "\n" if lines else ""


def save(pdir: Path, elements: list[dict]) -> None:
    pdir = Path(pdir)
    (pdir / JSON_FILE).write_text(
        json.dumps({"elements": elements}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8")
    (pdir / MD_FILE).write_text(render(elements), encoding="utf-8")


def load_text(pdir: Path) -> str:
    """elements.md of a production, '' when there is none."""
    try:
        return (Path(pdir) / MD_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def planner_block(text: str) -> str:
    """The block added to the shotlist planner prompt ('' = no elements)."""
    text = (text or "").strip()
    if not text:
        return ""
    return (
        "\n\nINPUT 4 - ELEMENTS (the recurring characters, places and objects, each with "
        "ONE fixed look). Whenever a shot shows one of them, its image prompt must "
        "describe it exactly as listed here - same shape, colours and clothing, in the "
        "same words - so it looks the same in every image. Do not give an element a new "
        "look, and do not add the listed elements to shots whose narration does not "
        "mention them:\n" + text)
