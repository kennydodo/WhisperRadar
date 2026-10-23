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
SHOT_FIELDS = ("land", "overlays", "ai_motion", "transition", "sfx", "reveal")
PLAN_MAPS = ("refs", "refPrompts")


def metadata(obj: dict) -> dict:
    """Keep reference registries separate from individual beat assignments."""
    return {k: dict(obj[k]) for k in PLAN_MAPS if isinstance(obj.get(k), dict)}


def sentences(cues: list[dict]) -> str:
    """The narration as sentences, one per line (a subtitle cue is a poor unit
    for a writer: it cuts sentences in two and invites micro-beats)."""
    text = " ".join(" ".join(str(c.get("text", "")).split()) for c in cues)
    text = re.sub(r"(?<=\d)\s+(?=[,.]\d)", "", text)          # "4 ,000" -> "4,000"
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    parts = re.split(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])", text)
    return "\n".join(p.strip() for p in parts if p.strip())


def min_words_per_beat(min_s: float, script: str, total_s: float) -> int:
    """Fewest words a beat should carry so that it is on screen for min_s."""
    n = len(tokens(script))
    wps = (n / total_s) if total_s > 0 and n else 2.6
    return max(WORDS_PER_BEAT_MIN, int(min_s * wps))



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
           first_words: str = "", animation_note: str = "",
           channel_context: str = "", min_words: int = 0) -> str:
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
        "shorter than one sentence unless the sentence itself changes the picture"
        + (f"; a beat should carry at least about {min_words} words of narration "
           "(group short sentences that belong together)" if min_words else "")
        + ".\n"
        "- You do NOT count or write subtitle cue numbers. The app works those out "
        "from `starts_with`. The opening quote is ONLY a location marker, not the "
        "length of a beat. A beat normally covers a complete developing visual idea "
        "across several narration lines. Do not cut the story into 5-8-word fragments "
        "or make a new image for each subtitle line.",
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
        "labels unless the CHANNEL text policy allows them. Preserve important exact "
        "figures from the narration when that policy permits them; never invent numbers. "
        "When renderer text is enabled use overlays instead of baked-in text. "
        "Reserve empty space only where an overlay or the channel composition needs it.",
        "ELEMENTS (recurring characters, places and objects, each with ONE fixed look; "
        "describe them exactly like this in every prompt where they appear, and do not "
        "add them to beats whose narration does not mention them):\n" + el,
    ]
    style = (style or "").strip()
    if animation_note:
        blocks.append(animation_note)
        blocks.append('Opening beats also include "ai_motion": one short sentence '
                      'describing subject and camera movement. Preserve this field in corrections.')
    if channel_context:
        blocks.insert(0, "CHANNEL PLANNING BRIEF AND INPUTS (same settings as the regular "
                      "shotlist planner):\n" + channel_context)
    blocks.append(
        "BEATS ADAPTER — overrides ONLY the brief's output schema and cue-number tasks:\n"
        "Return beats, not shots/images or cue ranges. Use starts_with quotes; the app "
        "assigns cues, filenames and timings. Keep ALL channel rules for motion shares, "
        "shot-type mix, presentation, host presence, density, holds, references, text, "
        "reveals, sound, expressions and composition. Group one developing visual idea "
        "into one beat; avoid a new image for every sentence or rephrasing.\n"
        'Optional beat fields: "refs" (names of only the references visible in this image), '
        '"land", "transition", "sfx", "overlays", "ai_motion", following the brief. '
        'Include top-level "refs" and "refPrompts" as in the brief when references are enabled; '
        'omit all reference fields when disabled. Supplied references keep their exact '
        'names and paths; never regenerate them. References go on each BEAT, and the app '
        'copies them to its image. Do not replace a supplied identity with an elements description.\n'
        'For reveals, use "reveal": {"starts_with": ["exact quote for item 1", '
        '"exact quote for item 2"], "layout": "row"}. Never supply cue numbers. '
        'For separate-image build-ups add "images": [{"prompt": "...", "refs": [...]}, ...] '
        'inside reveal, one image per quote. The app assigns their filenames. '
        'The first reveal quote starts with the beat; use ST and follow the channel reveal rules.\n'
        'Keep overlay start/end in seconds relative to the beat as specified in the brief. '
        'Do not output asset names or cue numbers. The schema below is a minimal example; '
        'include the optional fields needed for this channel.')
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
        'and repetition, but preserve character identity, reference instructions and the exact '
        'channel text/number policy>", "beats": [{"starts_with": "...", "scene": 1, "type": "SCN", '
        '"motion": "ZI", "prompt": "..."}]}\n'
        "If you reach your output limit, stop after the last COMPLETE beat. When I reply "
        '"continue", output ONLY the remaining beats in a new ```json block, starting at '
        "the next beat, no repetition, finishing the document.")
    blocks.append("NARRATION (exactly what the voice says, one sentence per row):\n" + (script or "").strip())
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
    for k in SHOT_FIELDS + ("refs",):
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
    return {"style": str(obj.get("style") or "").strip(), "beats": beats, **metadata(obj)}


def join_continuation(body: str, chunk: str) -> str:
    """Keep the original prefix when a continuation restarts its beats wrapper."""
    decoder = json.JSONDecoder()

    def parts(text):
        match = re.search(r'"beats"\s*:\s*\[', text)
        start = match.end() if match else 0
        prefix = text[:start] if match else ""
        items = []
        pos = start
        while pos < len(text):
            while pos < len(text) and (text[pos].isspace() or text[pos] == ','):
                pos += 1
            if pos == len(text) or text[pos] == ']':
                return prefix, items, pos, pos < len(text)
            try:
                item, end = decoder.raw_decode(text, pos)
            except ValueError:
                return prefix, items, pos, False
            if not isinstance(item, dict) or "starts_with" not in item:
                return prefix, items, pos, False
            items.append(item)
            pos = end
        return prefix, items, pos, False

    prefix, old, _, _ = parts(body)
    _, new, tail, closed = parts(chunk)
    if not prefix or not new:
        return body + "\n" + chunk
    # A writer may restart the last complete beat as well as the cut-off one.
    anchor = tokens(str(new[0].get("starts_with", "")))
    for i in range(len(old) - 1, -1, -1):
        if tokens(str(old[i].get("starts_with", ""))) == anchor:
            old = old[:i]
            break
    merged = prefix + "\n" + ",\n".join(json.dumps(b, ensure_ascii=False) for b in old + new)
    if closed:
        # Preserve registries or other top-level fields following the final list.
        return merged + chunk[tail:]
    # A further continuation starts after the last complete entry. Keep a comma
    # so the next entry can be appended without discarding the accumulated list.
    return merged + ",\n" + chunk[tail:]


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
        # A short quote is usable only when it identifies one remaining place.
        if len(q) == 2:
            matches = [i for i in range(cursor, len(words) - 1)
                       if words[i:i + 2] == q]
            if len(matches) == 1:
                found = matches[0]
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
        if n == 1 and found != 0:
            starts.append(0)
            faults.append(f'beat 1: the plan starts late. Its starts_with must begin with '
                          f'the narration opening: {" ".join(cues[0]["text"].split()[:QUOTE_WORDS])!r}. '
                          'Restore the missing opening beats; do not stretch a later image over them.')
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


def build_shotlist(beats: list[dict], ranges: list[tuple[int, int]], style: str,
                   plan_meta: dict | None = None, cues: list | None = None) -> tuple[dict, list[str]]:
    """The shotlist.json for these beats and the asset name of each beat."""
    shots, images, names, per_scene = [], [], [], {}
    for b, (a, z) in zip(beats, ranges):
        per_scene[b["scene"]] = per_scene.get(b["scene"], 0) + 1
        mot = b["motion"] or "ZI"
        name = asset_name(b["scene"], per_scene[b["scene"]], b["type"], mot)
        names.append(name)
        shot = {"cues": str(a) if a == z else f"{a}-{z}", "asset": name,
                "scene": f"S{b['scene']:02d}", "motion": mot}
        for k in SHOT_FIELDS:
            if k in b:
                shot[k] = b[k]
        reveal = b.get("reveal")
        reveal_images = []
        if (isinstance(reveal, dict) and isinstance(reveal.get("starts_with"), list)
                and cues is not None):
            local = [c for c in cues if a <= int(c["index"]) <= z]
            starts, _ = find_starts(
                [{"starts_with": str(q)} for q in reveal["starts_with"]], local)
            shot["reveal"] = {"cues": starts, "layout": reveal.get("layout", "row")}
            reveal_images = reveal.get("images") or []
            if not isinstance(reveal_images, list) or not all(isinstance(i, dict) for i in reveal_images):
                reveal_images = []
            if reveal_images:
                assets = [name]
                for item in reveal_images[1:]:
                    per_scene[b["scene"]] += 1
                    assets.append(asset_name(b["scene"], per_scene[b["scene"]], b["type"], mot))
                shot["reveal"]["assets"] = assets
        shots.append(shot)
        image = {"file": name, "prompt": b["prompt"]}
        if "refs" in b:
            image["refs"] = b["refs"]
        if reveal_images:
            for asset, item in zip(shot["reveal"]["assets"], reveal_images):
                images.append({"file": asset, "prompt": item.get("prompt", ""),
                               **({"refs": item["refs"]} if "refs" in item else {})})
        else:
            images.append(image)
    data = {"style": style, "shots": shots, "images": images, **metadata(plan_meta or {})}
    return data, names


def motion_faults(beats, allowed, allow_reveal=False) -> list[str]:
    bad = [str(n) for n, b in enumerate(beats, 1)
           if b["motion"] not in allowed and not (
               allow_reveal and b.get("reveal") and b["motion"] == "ST")]
    if not bad:
        return []
    return [f"beat(s) {', '.join(bad[:20])}: \"motion\" must be one of "
            f"{', '.join(allowed)}."]


def check(beats, cues, allowed, cap_s, tol, allow_reveal=False) -> tuple[list[int], list[tuple[int, int]], list[str]]:
    """Everything the app can say about beats BEFORE the shotlist code checks:
    (starts, ranges, faults)."""
    starts, f1 = find_starts(beats, cues)
    ranges, f2 = cue_ranges(starts, len(cues))
    faults = f1 + f2
    faults += motion_faults(beats, allowed, allow_reveal)
    if not f1 and not f2:
        faults += hold_faults(beats, ranges, cues, cap_s, tol)
        for n, (b, (a, z)) in enumerate(zip(beats, ranges), 1):
            reveal = b.get("reveal")
            if not reveal:
                continue
            quotes = reveal.get("starts_with") if isinstance(reveal, dict) else None
            if not isinstance(quotes, list) or not all(isinstance(q, str) for q in quotes):
                faults.append(f"beat {n}: reveal needs starts_with quotes, not cue numbers.")
                continue
            items = reveal.get("images")
            if items is not None and (not isinstance(items, list) or len(items) != len(quotes)
                    or not all(isinstance(i, dict) and i.get("prompt") for i in items)):
                faults.append(f"beat {n}: reveal images need one prompt per starts_with quote.")
                continue
            local = [c for c in cues if a <= int(c["index"]) <= z]
            _, rf = find_starts([{"starts_with": q} for q in quotes], local)
            faults.extend(f"beat {n}: reveal item {f}" for f in rf)
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
               style_fault: bool = False, plan_meta: dict | None = None,
               channel_context: str = "") -> str:
    text = "\n".join(f"- {f}" for f in faults)
    pointed = beats_named(text, names, [])
    if any(b.get("reveal") for b in beats):
        built, _ = build_shotlist(beats, ranges, "", plan_meta, cues)
        for n, shot in enumerate(built["shots"], 1):
            reveal = shot.get("reveal")
            if isinstance(reveal, dict) and any(
                    asset in text for asset in reveal.get("assets", [])):
                pointed = sorted(set(pointed) | {n})
    # Checker messages abbreviate asset lists; include every affected opening beat.
    if '"ai_motion"' in text:
        window = re.search(r"cues 1-(\d+)", text)
        if window:
            limit = int(window.group(1))
            pointed = sorted(set(pointed) | {
                i for i, (b, (a, z)) in enumerate(zip(beats, ranges), 1)
                if 0 < a <= limit and not str(b.get("ai_motion") or "").strip()})
    by = {int(c["index"]): c["text"] for c in cues}
    rows = []
    for n in pointed[:40]:
        b, (a, z) = beats[n - 1], ranges[n - 1]
        narr = " ".join(by.get(i, "") for i in range(a, z + 1)) if a else ""
        rows.append(f"beat {n} | cues {a}-{z} | {hold(cues, a, z):.1f}s | scene {b['scene']} "
                    f"{b['type']} {b['motion']}\n  starts_with: {b['starts_with']}\n"
                    f"  narration: {narr[:700]}\n  prompt: {b['prompt']}\n"
                    f"  optional fields: {json.dumps({k: b[k] for k in SHOT_FIELDS + ('refs',) if k in b}, ensure_ascii=False)}")
    more = f"\n(and {len(pointed) - 40} more beats of the same kind)" if len(pointed) > 40 else ""
    context = (("CHANNEL RULES AND INPUTS (still apply to corrections):\n" + channel_context + "\n\n")
               if channel_context else "")
    registry = json.dumps(metadata(plan_meta or {}), ensure_ascii=False)
    return (context +
        "BEATS CORRECTION: the channel brief still controls content and policy, but its "
        "shotlist/cue-number output instructions are replaced by the correction schema "
        "below. Do not return shots, images, asset names or cue numbers. Reveals keep "
        "starts_with quotes for each item, plus layout and optional images; the app maps "
        "them to cues and filenames. ST is allowed for channel-enabled reveal beats.\n\n"
        "My checker found problems in your beats. Fix ONLY these; everything not "
        "mentioned stays exactly as it is. Numbers below refer to the CURRENT list, "
        "not your original reply. Include original_starts_with in each replacement, "
        "copied from the beat being replaced. Do not split a preceding beat to fix "
        "another beat's quote.\n\nCURRENT BEAT INDEX:\n" +
        "\n".join(f"{i}: {b['starts_with']} | {b['type']} {b['motion']} | "
                  f"refs={b.get('refs', [])}" for i, b in enumerate(beats, 1)) +
        "\n\nCURRENT REFERENCE REGISTRIES:\n" + registry + "\n\nPROBLEMS:\n" + text +
        ("\n\nTHE BEATS CONCERNED (numbers are the numbers in your list):\n"
         + "\n".join(rows) + more if rows else "") +
        "\n\nReply with ONLY one JSON object in one ```json block, as text in this chat "
        "(no file, no download):\n"
        '{"replace": [{"beat": <current number>, "original_starts_with": "<current quote>", '
        '"through": <last current beat number being replaced; omit for a single beat>, '
        '"with": [<beat>, <beat>, ...]}], "style": "<only if '
        'the problem is the style>"}\n'
        'Each <beat> is {"starts_with", "scene", "type", "motion", "prompt"} like before. '
        'For opening beats include "ai_motion": one short sentence saying what moves '
        'and how the camera moves. Keep existing ai_motion when fixing another field. '
        'Preserve refs, land, overlays, transition, sfx and reveal on any replaced beat. '
        'To fix a registry include top-level refs/refPrompts maps containing only changes; '
        'replace may be [] for a registry-only or style-only fix. Beat refs are names '
        'from that registry, only for subjects visible in that image. '
        "`with` replaces that beat: to split a long beat give two or more beats - the first "
        "keeps the original starts_with, the next ones start at the exact narration words "
        "where the picture changes (copied exactly, at least "
        f"{QUOTE_WORDS} words). To fix a motion or a prompt, give one beat. Allowed motion "
        f"codes: {', '.join(allowed)}. To MERGE beats 6 and 7, use beat: 6, through: 7, "
        "with: [ONE combined beat]. This removes both old entries. Keep the first "
        "beat's opening quote and combine the visual idea into one depictable image. "
        "Do not return a second replacement for any beat inside that range. All numbers "
        "refer to the current list BEFORE any changes. Do not resend the whole list.")


def parse_fix(reply: str, allowed=None) -> dict | None:
    obj = _object_with(reply, "replace")
    if not obj:
        return None
    out = []
    targets = {}
    provided = {}
    through = {}
    for r in obj["replace"]:
        if not isinstance(r, dict):
            continue
        try:
            n = int(r.get("beat"))
            end = int(r.get("through", n))
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
            targets[n] = str(r.get("original_starts_with") or "")
            through[n] = end
            provided[n] = [set(raw) for raw in with_ if isinstance(raw, dict)
                           and _clean_beat(raw, None, allowed)]
    if not out and not metadata(obj) and not str(obj.get("style") or "").strip():
        return None
    return {"replace": out, "targets": targets, "provided": provided, "through": through,
            "style": str(obj.get("style") or "").strip(), **metadata(obj)}


def apply_fix(beats: list[dict], fix: dict) -> list[dict]:
    seen = set()
    for n, repl in fix["replace"]:
        end = fix.get("through", {}).get(n, n)
        if not 1 <= n <= end <= len(beats) or seen.intersection(range(n, end + 1)):
            raise ValueError(f"Invalid or overlapping beat range {n}-{end}; use the current index.")
        seen.update(range(n, end + 1))
        old = tokens(beats[n - 1]["starts_with"])
        target = tokens(fix.get("targets", {}).get(n, ""))
        first = tokens(repl[0]["starts_with"])
        if (target and target != old) or (not target and
                old[:min(len(old), len(first))] != first[:min(len(old), len(first))]):
            raise ValueError(f"Replacement for beat {n} targets a different quote; "
                             "use the current index and original_starts_with.")
    new = list(beats)
    for n, repl in sorted(fix["replace"], key=lambda x: -x[0]):
        end = fix.get("through", {}).get(n, n)
        if 1 <= n <= len(new):
            if len(repl) == 1 and end == n:
                # A motion/prompt correction must not silently detach its references,
                # animation or overlays. Explicit null/[] still clears a field.
                original = beats[n - 1]
                fields = fix.get("provided", {}).get(n, [set(repl[0])])[0]
                replacement = dict(repl[0])
                for k in SHOT_FIELDS + ("refs",):
                    if k not in fields and k in original:
                        replacement[k] = original[k]
                repl = [replacement]
            new[n - 1:end] = repl
    return new


def save(pdir, beats, ranges, names, style, plan_meta=None) -> None:
    from pathlib import Path
    rows = [dict(b, cues=(f"{a}" if a == z else f"{a}-{z}"), asset=nm)
            for b, (a, z), nm in zip(beats, ranges, names)]
    (Path(pdir) / JSON_FILE).write_text(
        json.dumps({"style": style, "beats": rows, **metadata(plan_meta or {})},
                   ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8")
