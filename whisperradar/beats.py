"""Beats: plan the video from the SCRIPT, then let the app work out the cues.

Experiment (script-first planning). The writer reads script.md (plus the
elements list) and returns BEATS: one image each, in story order. A beat does
not carry cue numbers. It carries `starts_with`, the first words of the narration
it begins at, copied exactly. The app finds those words in the subtitles, so
the beat's cue range follows from the text: a beat runs from its own start to
the next beat's start. Coverage (every cue once, in order, no gaps) is then true
by construction, and the writer never counts cues.

The result is an ordinary shotlist.json, so images, motion, merge and export do
not change. Nothing here runs unless the "beats" step is started."""
from __future__ import annotations

import json
import re

from . import briefs, studio

JSON_FILE = "beats.json"
WORDS_PER_BEAT_MIN = 3              # shortest quote the matcher accepts
QUOTE_WORDS = 8                     # how many words the writer is asked to quote
STYLE_MAX = 1400                    # the compact style the writer returns
TYPE_CODES = tuple(briefs.TYPE_CODES)


# ---- the prompt --------------------------------------------------------------------

def words_per_beat(cap_s: float, tol: float, script: str, total_s: float) -> int:
    """A rough ceiling of words for one beat so its hold stays under the limit."""
    n = len(tokens(script))
    wps = (n / total_s) if total_s > 0 and n else 2.6
    return max(12, int(briefs.hold_limit(cap_s, tol) * wps * 0.8))


_HINTS = {"ST": "ST holds still", "ZI": "ZI pushes in on a subject",
          "ZO": "ZO pulls back to show a place", "PU": "PU tilts up a tall or stacked picture",
          "PD": "PD tilts down a tall or stacked picture", "PL": "PL pans left along a wide picture",
          "PR": "PR pans right along a wide picture", "PV": "PV pans across a panorama"}


def motion_hint(allowed) -> str:
    return "; ".join(_HINTS[c] for c in allowed if c in _HINTS) + "."


def prompt(script: str, elements_md: str, style: str, bible: str,
           allowed: list[str], cap_s: float, max_words: int,
           first_words: str = "") -> str:
    codes = ", ".join(allowed)
    types = "; ".join(f"{k} = {v}" for k, v in briefs.TYPE_CODES.items())
    el = ((elements_md or "").strip()
          or "(none - describe recurring things the same way each time yourself)")
    blocks = [
        "You are planning the pictures of a narrated explainer video. Read the NARRATION "
        "below and split the story into BEATS. One beat = ONE image that stays on "
        "screen while its part of the narration is spoken. Think like a storyboard "
        "artist: what does the viewer see for each part of the story?",
        "HOW BEATS WORK:\n"
        "- Beats follow the script in order and together cover ALL of it, from the "
        "very first word to the very last. No gaps.\n"
        f"- `starts_with`: the first {QUOTE_WORDS} words of the script where this beat "
        "begins, copied EXACTLY as written (same words, same order, no changes). A beat "
        "lasts until the next beat's `starts_with`. The first beat starts at the first "
        "words of the script.\n"
        f"- Keep a beat short enough to be spoken in about {cap_s:g} seconds: at most "
        f"about {max_words} words of narration. Longer passages need more than one beat; "
        "split where the idea, the subject or the example changes. Do not make beats "
        "shorter than one sentence unless the sentence itself changes the picture.\n"
        "- You do NOT count or write subtitle cue numbers. The app works those out "
        "from `starts_with`.",
        "EACH BEAT HAS:\n"
        '- "starts_with": see above.\n'
        '- "scene": a whole number; beats of one part of the story share it, and it only '
        "goes up.\n"
        f'- "type": one of {types}.\n'
        f'- "motion": one of {codes}. Vary it; do not repeat the same code many beats in a '
        "row. " + motion_hint(allowed) + "\n"
        '- "prompt": the finished image prompt for this beat (40-90 words): the one thing '
        "the viewer sees, who or what is in it, the composition. Use the elements below "
        "whenever the narration is about them, described exactly as listed. Do NOT repeat "
        "the style (it is added from the top-level style). No readable text, numbers or "
        "labels in the picture. Leave the top quarter of the frame plain and empty.",
        "ELEMENTS (recurring characters, places and objects, each with ONE fixed look; "
        "describe them exactly like this in every prompt where they appear, and do not "
        "add them to beats whose narration does not mention them):\n" + el,
    ]
    style = (style or "").strip()
    if style:
        blocks.append("CHANNEL VISUAL STYLE (the images are drawn in this style):\n" + style)
    if (bible or "").strip():
        blocks.append("CHARACTER / REFERENCE BIBLE:\n" + bible.strip())
    else:
        blocks.append("There is no character bible for this channel. Do not ask for one; "
                      "plan without it.")
    blocks.append(
        "OUTPUT: reply with ONE JSON object in ONE ```json block, as text in this chat "
        "(no file, no canvas, no download), and nothing after it:\n"
        '{"style": "<the channel style, compacted to at most '
        f'{STYLE_MAX} characters: keep palette, line and rendering rules, drop examples '
        'and repetition>", "beats": [{"starts_with": "...", "scene": 1, "type": "SCN", '
        '"motion": "ZI", "prompt": "..."}]}\n'
        "If you reach your output limit, stop after the last COMPLETE beat. When I reply "
        '"continue", output ONLY the remaining beats in a new ```json block, starting at '
        "the next beat, no repetition, finishing the document.")
    blocks.append("NARRATION (exactly what the voice says, one subtitle line per row; a beat may start in the middle of a row):\n" + (script or "").strip())
    return "\n\n".join(blocks) + "\n"


# ---- reading replies -----------------------------------------------------------------

def _balanced_end(text: str, start: int) -> int | None:
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
                return i + 1
    return None


def _object_with(text: str, key: str):
    """The biggest complete {...} in `text` that has `key` as a list."""
    text = re.sub(r"```[a-z]*", "", text or "")
    best = None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        end = _balanced_end(text, i)
        if end is None or (best is not None and end - i <= best[0]):
            continue
        try:
            obj = json.loads(text[i:end])
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get(key), list):
            best = (end - i, obj)
    return best[1] if best else None


def _clean_beat(b, prev: dict | None, allowed) -> dict | None:
    if not isinstance(b, dict):
        return None
    sw = re.sub(r"\s+", " ", str(b.get("starts_with") or "")).strip()
    pr = re.sub(r"\s+", " ", str(b.get("prompt") or "")).strip()
    if not sw or not pr:
        return None
    try:
        scene = int(b.get("scene"))
    except (TypeError, ValueError):
        scene = int(prev["scene"]) if prev else 1
    typ = str(b.get("type") or "SCN").strip().upper()
    if typ not in TYPE_CODES:
        typ = "SCN"
    mot = str(b.get("motion") or "").strip().upper()
    out = {"starts_with": sw, "scene": max(1, scene), "type": typ, "motion": mot,
           "prompt": pr}
    for k in ("land", "overlays"):
        if b.get(k) not in (None, "", [], {}):
            out[k] = b[k]
    return out


def parse(reply: str, allowed=None) -> dict | None:
    """{"style": str, "beats": [clean beats]} from a reply, None when there is
    no complete beats object (a cut-off reply)."""
    obj = _object_with(reply, "beats")
    if not obj:
        return None
    beats, prev = [], None
    for raw in obj["beats"]:
        b = _clean_beat(raw, prev, allowed)
        if b:
            beats.append(b)
            prev = b
    if not beats:
        return None
    return {"style": str(obj.get("style") or "").strip(), "beats": beats}


# ---- finding the beats in the subtitles ------------------------------------------------

def tokens(text: str) -> list[str]:
    text = (text or "").lower().replace("’", "'")
    text = re.sub(r"(?<=\w)'(?=\w)", "", text)
    return re.findall(r"[a-z0-9]+", text)


def cue_stream(cues: list[dict]) -> list[tuple[str, int]]:
    """Every word of the narration with the number of the cue it is in."""
    out = []
    for c in cues:
        for w in tokens(c.get("text", "")):
            out.append((w, int(c["index"])))
    return out


def find_starts(beats: list[dict], cues: list[dict]) -> tuple[list[int], list[str]]:
    """The cue each beat starts in (one number per beat, 0 = not found) and the
    faults. Beats are searched forward only, in order."""
    stream = cue_stream(cues)
    words = [w for w, _ in stream]
    faults: list[str] = []
    starts: list[int] = []
    cursor = 0
    for n, b in enumerate(beats, 1):
        q = tokens(b["starts_with"])
        found = -1
        for k in range(min(len(q), QUOTE_WORDS), WORDS_PER_BEAT_MIN - 1, -1):
            probe = q[:k]
            i = cursor
            while i <= len(words) - k:
                if words[i:i + k] == probe:
                    found = i
                    break
                i += 1
            if found != -1:
                break
        if found == -1:
            starts.append(0)
            faults.append(
                f"beat {n}: its \"starts_with\" ({b['starts_with'][:70]!r}) was not found in "
                f"the narration after beat {n - 1}. Copy the first {QUOTE_WORDS} words of "
                f"where the beat begins exactly as written in the script.")
            continue
        starts.append(stream[found][1])
        cursor = found + 1
    return starts, faults


def cue_ranges(starts: list[int], n_cues: int) -> tuple[list[tuple[int, int]], list[str]]:
    """(first, last) cue per beat. The first beat always starts at cue 1. Two
    beats starting in the same cue, or out of order, are faults."""
    faults: list[str] = []
    fixed = list(starts)
    if fixed:
        fixed[0] = 1
    prev = 0
    for n, s in enumerate(fixed, 1):
        if s == 0:
            continue
        if s <= prev and n > 1:
            faults.append(
                f"beat {n - 1} and beat {n} both start in cue {s}: one subtitle cue can only "
                f"carry one image. Merge them into one beat, or start beat {n} at the "
                f"narration of a later cue.")
        else:
            prev = s
    ranges: list[tuple[int, int]] = []
    for i, s in enumerate(fixed):
        nxt = next((x for x in fixed[i + 1:] if x), n_cues + 1)
        ranges.append((s, max(s, nxt - 1)) if s else (0, 0))
    return ranges, faults


def _secs(ts: str) -> float:
    return studio._srt_seconds(ts)


def hold(cues: list[dict], a: int, b: int) -> float:
    by = {int(c["index"]): c for c in cues}
    if a not in by or b not in by:
        return 0.0
    return _secs(by[b]["end"]) - _secs(by[a]["start"])


def hold_faults(beats, ranges, cues, cap_s: float, tol: float) -> list[str]:
    top = briefs.hold_limit(cap_s, tol)
    out = []
    for n, (a, b) in enumerate(ranges, 1):
        if not a:
            continue
        h = hold(cues, a, b)
        if h > top:
            out.append(
                f"beat {n} (cues {a}-{b}) lasts {h:.1f}s, over the {top:.1f}s limit "
                f"({cap_s:g}s plus the accepted tolerance): split it into two or more beats "
                f"at a change of idea.")
    return out


# ---- building the shotlist -----------------------------------------------------------------

def asset_name(scene: int, idx: int, typ: str, motion: str) -> str:
    return f"S{scene:02d}_{idx:02d}_{typ}_{motion}.png"


def build_shotlist(beats: list[dict], ranges: list[tuple[int, int]], style: str) -> tuple[dict, list[str]]:
    """The shotlist.json for these beats and the asset name of each beat."""
    shots, images, names, per_scene = [], [], [], {}
    for b, (a, z) in zip(beats, ranges):
        per_scene[b["scene"]] = per_scene.get(b["scene"], 0) + 1
        mot = b["motion"] or "ZI"
        name = asset_name(b["scene"], per_scene[b["scene"]], b["type"], mot)
        names.append(name)
        shot = {"cues": str(a) if a == z else f"{a}-{z}", "asset": name,
                "scene": f"S{b['scene']:02d}", "motion": mot}
        for k in ("land", "overlays"):
            if k in b:
                shot[k] = b[k]
        shots.append(shot)
        images.append({"file": name, "prompt": b["prompt"]})
    data = {"style": style, "shots": shots, "images": images}
    return data, names


def motion_faults(beats, allowed) -> list[str]:
    bad = [str(n) for n, b in enumerate(beats, 1)
           if b["motion"] not in allowed]
    if not bad:
        return []
    return [f"beat(s) {', '.join(bad[:20])}: \"motion\" must be one of "
            f"{', '.join(allowed)}."]


def check(beats, cues, allowed, cap_s, tol) -> tuple[list[int], list[tuple[int, int]], list[str]]:
    """Everything the app can say about beats BEFORE the shotlist code checks:
    (starts, ranges, faults)."""
    starts, f1 = find_starts(beats, cues)
    ranges, f2 = cue_ranges(starts, len(cues))
    faults = f1 + f2
    faults += motion_faults(beats, allowed)
    if not f1 and not f2:
        faults += hold_faults(beats, ranges, cues, cap_s, tol)
    return starts, ranges, faults


# ---- fixing in place ------------------------------------------------------------------------

_ASSET_RE = re.compile(r"S\d+_\d+_[A-Z]+_[A-Z]+\.png")


def beats_named(text: str, names: list[str], faults_beats: list[int]) -> list[int]:
    """Beat numbers a fault text points at: by 'beat N' and by asset file name."""
    found = set(faults_beats)
    for m in re.finditer(r"\bbeats?\s+(\d+)(?:\s*(?:,|and|&)\s*(?:beat\s*)?(\d+))*", text, re.I):
        found.update(int(x) for x in re.findall(r"\d+", m.group(0)))
    pos = {n: i + 1 for i, n in enumerate(names)}
    for a in _ASSET_RE.findall(text):
        if a in pos:
            found.add(pos[a])
    return sorted(x for x in found if 1 <= x <= len(names))


def fix_prompt(faults: list[str], beats, ranges, cues, names, allowed, cap_s: float,
               style_fault: bool = False) -> str:
    text = "\n".join(f"- {f}" for f in faults)
    pointed = beats_named(text, names, [])
    by = {int(c["index"]): c["text"] for c in cues}
    rows = []
    for n in pointed[:40]:
        b, (a, z) = beats[n - 1], ranges[n - 1]
        narr = " ".join(by.get(i, "") for i in range(a, z + 1)) if a else ""
        rows.append(f"beat {n} | cues {a}-{z} | {hold(cues, a, z):.1f}s | scene {b['scene']} "
                    f"{b['type']} {b['motion']}\n  starts_with: {b['starts_with']}\n"
                    f"  narration: {narr[:700]}\n  prompt: {b['prompt']}")
    more = f"\n(and {len(pointed) - 40} more beats of the same kind)" if len(pointed) > 40 else ""
    return (
        "My checker found problems in your beats. Fix ONLY these; everything not "
        "mentioned stays exactly as it is.\n\nPROBLEMS:\n" + text +
        ("\n\nTHE BEATS CONCERNED (numbers are the numbers in your list):\n"
         + "\n".join(rows) + more if rows else "") +
        "\n\nReply with ONLY one JSON object in one ```json block, as text in this chat "
        "(no file, no download):\n"
        '{"replace": [{"beat": <number>, "with": [<beat>, <beat>, ...]}], "style": "<only if '
        'the problem is the style>"}\n'
        'Each <beat> is {"starts_with", "scene", "type", "motion", "prompt"} like before. '
        "`with` replaces that beat: to split a long beat give two or more beats - the first "
        "keeps the original starts_with, the next ones start at the exact narration words "
        "where the picture changes (copied exactly, at least "
        f"{QUOTE_WORDS} words). To fix a motion or a prompt, give one beat. Allowed motion "
        f"codes: {', '.join(allowed)}. Do not resend the whole list.")


def parse_fix(reply: str, allowed=None) -> dict | None:
    obj = _object_with(reply, "replace")
    if not obj:
        return None
    out = []
    for r in obj["replace"]:
        if not isinstance(r, dict):
            continue
        try:
            n = int(r.get("beat"))
        except (TypeError, ValueError):
            continue
        with_ = r.get("with")
        if isinstance(with_, dict):
            with_ = [with_]
        new, prev = [], None
        for raw in with_ or []:
            b = _clean_beat(raw, prev, allowed)
            if b:
                new.append(b)
                prev = b
        if new:
            out.append((n, new))
    if not out:
        return None
    return {"replace": out, "style": str(obj.get("style") or "").strip()}


def apply_fix(beats: list[dict], fix: dict) -> list[dict]:
    new = list(beats)
    for n, repl in sorted(fix["replace"], key=lambda x: -x[0]):
        if 1 <= n <= len(new):
            new[n - 1:n] = repl
    return new


def save(pdir, beats, ranges, names, style) -> None:
    from pathlib import Path
    rows = [dict(b, cues=(f"{a}" if a == z else f"{a}-{z}"), asset=nm)
            for b, (a, z), nm in zip(beats, ranges, names)]
    (Path(pdir) / JSON_FILE).write_text(
        json.dumps({"style": style, "beats": rows}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8")
