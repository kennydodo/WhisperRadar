"""Port of ImgToVideo.Core.Manifest.ShotList (ShotListParser + ShotListExpander).

shotlist.json (LLM output: shots + optional images/style) -> VisualManifest."""
from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional

from . import models as m
from . import manifest as mf
from .parsing import cue_index

FILE_NAME = "shotlist.json"
TYPO_DISTANCE_THRESHOLD = 2

_INT_RE = re.compile(r"^[ \t\r\n]*[+-]?[0-9]+[ \t\r\n]*$")
_SFX_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\- ]{0,39}$")


class CaseInsensitiveDict(Mapping):
    """string-keyed dictionary with StringComparer.OrdinalIgnoreCase semantics
    (first inserted spelling is kept for iteration)."""

    def __init__(self):
        self._d: dict = {}

    @staticmethod
    def _k(key):
        return key.upper()

    def try_add(self, key, value) -> bool:
        k = self._k(key)
        if k in self._d:
            return False
        self._d[k] = (key, value)
        return True

    def __getitem__(self, key):
        return self._d[self._k(key)][1]

    def __contains__(self, key):
        return isinstance(key, str) and self._k(key) in self._d

    def __iter__(self):
        return (orig for orig, _ in self._d.values())

    def __len__(self):
        return len(self._d)


@dataclass
class ShotListEntry:
    shot_id: Optional[str]
    first_cue: int
    last_cue: int
    asset: str
    motion: Optional[mf.VisualMotion] = None
    transition: Optional[mf.VisualTransition] = None
    framing: Optional[mf.VisualFraming] = None
    scene: Optional[str] = None
    reveal_cues: Optional[list] = None
    reveal_layout: Optional[str] = None
    reveal_assets: Optional[list] = None
    sfx: Optional[list] = None
    overlays: Optional[list] = None


@dataclass
class ShotListDocument:
    shots: list
    prompts: Mapping
    style: Optional[str] = None


# ------------------------------------------------------------------ helpers
def _len16(s: str) -> int:
    """.NET string.Length (UTF-16 code units)."""
    return len(s.encode("utf-16-le")) // 2


def _blank(s) -> bool:
    return s is None or s.strip() == ""


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _int32(v) -> Optional[int]:
    """JsonElement.GetInt32 for a number element, None where it would throw."""
    if isinstance(v, int) and not isinstance(v, bool) and -2**31 <= v < 2**31:
        return v
    return None


def _try_parse_int(s: str) -> Optional[int]:
    if not _INT_RE.match(s):
        return None
    v = int(s.strip())
    return v if -2**31 <= v < 2**31 else None


def _get_string(obj, name) -> Optional[str]:
    v = obj.get(name) if isinstance(obj, dict) else None
    return v if isinstance(v, str) else None


def _get_number(obj, name) -> Optional[float]:
    v = obj.get(name)
    return float(v) if _is_num(v) else None


def _get_long(obj, name) -> Optional[int]:
    v = obj.get(name)
    if not _is_num(v):
        return None
    if isinstance(v, int) and -2**63 <= v < 2**63:
        return v
    raise ValueError(f"shotlist.json: \"{name}\" must be an integer.")  # GetInt64 FormatException


def _stem(name: str) -> str:
    base = re.split(r"[\\/]", name)[-1]
    i = base.rfind(".")
    return base if i < 0 else base[:i]


def _has_extension(name: str) -> bool:
    base = re.split(r"[\\/]", name)[-1]
    i = base.rfind(".")
    return 0 <= i < len(base) - 1


# ------------------------------------------------------------------ parser
def parse(json_text: str, issues: list, master_prompt_max_chars: int = 0,
          prompt_max_chars: int = 0) -> ShotListDocument:
    """Raises ValueError (json.JSONDecodeError included) on a broken file."""
    root = json.loads(json_text)
    shots_el = root.get("shots") if isinstance(root, dict) else None
    if not isinstance(shots_el, list):
        raise ValueError('shotlist.json must be an object with a "shots" array.')

    prompts = CaseInsensitiveDict()
    style = None
    style_el = root.get("style")
    if isinstance(style_el, str) and not _blank(style_el):
        style = style_el.strip()
        if master_prompt_max_chars > 0 and _len16(style) > master_prompt_max_chars:
            issues.append(m.ValidationIssue(
                m.WARNING, "SHOTLIST_MASTER_PROMPT_LONG",
                f"shotlist.json: the master prompt (style) is {_len16(style)} characters — the batch app's "
                f"master box holds {master_prompt_max_chars}. Trim it to fit (cut adjectives and examples; "
                "keep the character-identity sentences)."))

    images_el = root.get("images")
    if isinstance(images_el, list):
        image_number = 0
        for image in images_el:
            image_number += 1
            file_el = image.get("file") if isinstance(image, dict) else None
            if not isinstance(file_el, str) or _blank(file_el):
                issues.append(m.ValidationIssue(
                    m.ERROR, "SHOTLIST_INVALID",
                    f'shotlist.json: images[{image_number}] needs a "file" name.'))
                continue
            prompt = ""
            prompt_el = image.get("prompt")
            if isinstance(prompt_el, str) and not _blank(prompt_el):
                prompt = prompt_el.strip()
                if prompt_max_chars > 0 and _len16(prompt) > prompt_max_chars:
                    issues.append(m.ValidationIssue(
                        m.WARNING, "SHOTLIST_PROMPT_LONG",
                        f'shotlist.json: the prompt for "{file_el.strip()}" is {_len16(prompt)} '
                        f"characters — the batch app's card box holds {prompt_max_chars}. Trim it to fit "
                        "(cut composition boilerplate; keep the action and layout)."))
            prompts.try_add(file_el.strip(), prompt)

    entries: list = []
    number = 0
    for shot in shots_el:
        number += 1
        if not isinstance(shot, dict):
            issues.append(m.ValidationIssue(
                m.ERROR, "SHOTLIST_INVALID", f"shotlist.json: shots[{number}] is not an object."))
            continue
        cues = _try_get_cues(shot)
        if cues is None:
            issues.append(m.ValidationIssue(
                m.ERROR, "SHOTLIST_CUES_INVALID",
                f'shotlist.json: shots[{number}] has no usable "cues" — '
                'use "1-3", "4" or [1,2,3].'))
            continue
        first_cue, last_cue = cues
        asset_el = shot.get("asset")
        if not isinstance(asset_el, str) or _blank(asset_el):
            issues.append(m.ValidationIssue(
                m.ERROR, "SHOTLIST_INVALID",
                f'shotlist.json: shots[{number}] is missing "asset" (the image filename).'))
            continue
        reveal = _parse_reveal(shot, number, issues)
        entries.append(ShotListEntry(
            _get_string(shot, "shot_id"), first_cue, last_cue, asset_el.strip(),
            _parse_motion(shot.get("motion"), shot.get("land")),
            _parse_transition(shot.get("transition")),
            _parse_framing(shot.get("framing")),
            _get_string(shot, "scene"),
            reveal[0] if reveal else None,
            reveal[1] if reveal else None,
            reveal[2] if reveal else None,
            _parse_sfx(shot, number, issues),
            _parse_overlays(shot, number, issues)))
    return ShotListDocument(entries, prompts, style)


def _try_get_cues(shot: dict):
    if "cues" not in shot:
        return None
    cues = shot["cues"]
    numbers: list = []
    if isinstance(cues, str):
        for part in cues.split(","):
            rng = re.split(r"[-/]", part.strip())
            if len(rng) not in (1, 2):
                return None
            a = _try_parse_int(rng[0].strip())
            if a is None or a < 1:
                return None
            numbers.append(a)
            if len(rng) == 2:
                b = _try_parse_int(rng[1].strip())
                if b is None or b < 1:
                    return None
                numbers.append(b)
    elif isinstance(cues, list):
        for item in cues:
            v = _int32(item) if _is_num(item) else None
            if v is None:
                return None
            numbers.append(v)
    else:
        return None
    if not numbers or any(n < 1 for n in numbers):
        return None
    return min(numbers), max(numbers)


def _parse_reveal(shot: dict, number: int, issues: list):
    """Returns (cues, layout, assets) or None (warning added when unusable)."""
    if "reveal" not in shot or shot["reveal"] is None:
        return None
    element = shot["reveal"]
    layout = m.REVEAL_ROW
    assets = None
    problem = None
    if isinstance(element, dict):
        lay = element.get("layout")
        if lay is not None:
            if isinstance(lay, str) and m.reveal_layout_valid(lay):
                layout = m.reveal_layout_normalize(lay)
            else:
                problem = '"layout" must be "row" or "grid"'
        assets_el = element.get("assets")
        if assets_el is not None:
            assets = []
            if isinstance(assets_el, list):
                for item in assets_el:
                    if not isinstance(item, str) or _blank(item):
                        assets = None
                        break
                    assets.append(item.strip())
            else:
                assets = None
            if assets is None and problem is None:
                problem = '"assets" must be a list of image file names'
        element = element.get("cues")  # missing -> default (not an array)

    cues: list = []
    if isinstance(element, list):
        for item in element:
            v = _int32(item) if _is_num(item) else None
            if v is None or v < 1:
                cues = []
                break
            cues.append(v)

    if problem is None and layout == m.REVEAL_GRID and len(cues) != 4:
        problem = "a grid reveal needs exactly 4 items"
    if problem is None and assets is not None and len(assets) != len(cues):
        problem = '"assets" must have one image per cue'

    if problem is not None or len(cues) < m.REVEAL_MIN_ITEMS or len(cues) > m.REVEAL_MAX_ITEMS:
        issues.append(m.ValidationIssue(
            m.WARNING, "SHOTLIST_REVEAL_INVALID",
            f'shotlist.json: shots[{number}] has an unusable "reveal"'
            + ("" if problem is None else f" ({problem})")
            + f" - give {m.REVEAL_MIN_ITEMS} to {m.REVEAL_MAX_ITEMS} cue numbers, one per item, "
            "e.g. [41, 42, 43]. The whole image is shown instead."))
        return None
    return cues, layout, assets


def _parse_sfx(shot: dict, number: int, issues: list):
    if "sfx" not in shot or shot["sfx"] is None:
        return None
    element = shot["sfx"]
    names: list = []
    ok = True
    if isinstance(element, str):
        names.append(element)
    elif isinstance(element, list):
        for item in element:
            if not isinstance(item, str):
                ok = False
                break
            names.append(item)
    else:
        ok = False
    names = [n.strip() for n in names]
    if not ok or not names or any(not _SFX_NAME.match(n) for n in names):
        issues.append(m.ValidationIssue(
            m.WARNING, "SHOTLIST_SFX_INVALID",
            f'shotlist.json: shots[{number}] has an unusable "sfx" - use a sound name such as '
            '"pop" or a list of names, one per reveal item. No sound is added for this shot.'))
        return None
    return names


def _parse_land(v):
    """`land` on a PU/PD shot: percent down the picture, 0-100. Anything else is ignored."""
    if _is_num(v) and not isinstance(v, bool) and 0 <= v <= 100:
        return float(v)
    return None


OVERLAY_POSITIONS = (
    "top-left", "top-center", "top-right", "middle-left", "center", "middle-right",
    "bottom-left", "bottom-center", "bottom-right")
OVERLAY_ANIMS = ("none", "fade", "slide-up", "slide-down")
OVERLAY_MAX_CHARS = 200


def _parse_overlay_cue(v):
    """An overlay's `cue`: 14, "14", "14-15" -> (first, last), else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return (v, v) if v >= 1 else None
    if isinstance(v, str):
        mt = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", v)
        if mt:
            a, b = int(mt.group(1)), int(mt.group(2) or mt.group(1))
            if 1 <= a <= b:
                return (a, b)
    return None


def _finish_overlays(items, first_cue, last_cue, by_index, shot_start_s, shot_end_s, text_options):
    """The shot's overlay items with their times settled: an item that names its
    `cue` is shown for exactly that cue (seconds from the shot start); with
    text.auto_values on, the numbers/years/amounts the narration says are added."""
    from . import auto_values
    out: list = []
    for it in items or []:
        it = dict(it)
        cue = it.pop("cue", None)
        if cue is not None:
            a, b = by_index.get(cue[0]), by_index.get(cue[1])
            if a is not None and b is not None and first_cue <= cue[0] and cue[1] <= last_cue:
                it["start"] = round(max(a.start_seconds, shot_start_s) - shot_start_s, 3)
                it["end"] = round(min(b.end_seconds, shot_end_s) - shot_start_s, 3)
        out.append(it)
    if text_options is not None and getattr(text_options, "auto_values", False):
        cues = [(by_index[c].start_seconds, by_index[c].end_seconds, by_index[c].text)
                for c in range(first_cue, last_cue + 1) if c in by_index]
        have = {re.sub(r"\W+", "", (o.get("text") or "")).lower() for o in out}
        for it in auto_values.build(cues, shot_start_s, shot_end_s,
                                    style=text_options.auto_style,
                                    position=text_options.auto_position,
                                    min_seconds=text_options.auto_min_seconds,
                                    max_seconds=text_options.auto_max_seconds):
            key = re.sub(r"\W+", "", it["text"]).lower()
            if key in have:
                continue                    # the writer already shows this value
            it.pop("auto", None)
            out.append(it)
    return out or None


def _parse_overlays(shot: dict, number: int, issues: list):
    """`overlays`: text the renderer draws (own font/style) instead of text baked
    into the image. Each item: {"text", "style"?, "position"?, "start"?, "end"?,
    "anim"?}; start/end are seconds from the start of the shot (default: the whole
    shot). A bad item is skipped with a warning, the shot is kept."""
    element = shot.get("overlays")
    if element is None:
        return None
    if isinstance(element, dict):
        element = [element]
    if not isinstance(element, list):
        issues.append(m.ValidationIssue(
            m.WARNING, "SHOTLIST_OVERLAY_INVALID",
            f'shotlist.json: shots[{number}] has an unusable "overlays" - use a list of '
            '{"text": ..., "position": ...} items. No text is drawn for this shot.'))
        return None
    items: list = []
    for index, item in enumerate(element, 1):
        where = f'shots[{number}].overlays[{index}]'
        if not isinstance(item, dict):
            issues.append(m.ValidationIssue(
                m.WARNING, "SHOTLIST_OVERLAY_INVALID",
                f'shotlist.json: {where} must be an object with a "text". Skipped.'))
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            issues.append(m.ValidationIssue(
                m.WARNING, "SHOTLIST_OVERLAY_INVALID",
                f'shotlist.json: {where} has no "text". Skipped.'))
            continue
        text = text.strip()
        if len(text) > OVERLAY_MAX_CHARS:
            issues.append(m.ValidationIssue(
                m.WARNING, "SHOTLIST_OVERLAY_INVALID",
                f'shotlist.json: {where} text is {len(text)} characters (limit '
                f'{OVERLAY_MAX_CHARS}). Skipped.'))
            continue
        position = str(item.get("position") or "bottom-center").strip().lower()
        if position not in OVERLAY_POSITIONS:
            issues.append(m.ValidationIssue(
                m.WARNING, "SHOTLIST_OVERLAY_INVALID",
                f'shotlist.json: {where} position "{position}" is not one of '
                f'{", ".join(OVERLAY_POSITIONS)}. Using bottom-center.'))
            position = "bottom-center"
        anim = str(item.get("anim") or "fade").strip().lower()
        if anim not in OVERLAY_ANIMS:
            issues.append(m.ValidationIssue(
                m.WARNING, "SHOTLIST_OVERLAY_INVALID",
                f'shotlist.json: {where} anim "{anim}" is not one of '
                f'{", ".join(OVERLAY_ANIMS)}. Using fade.'))
            anim = "fade"
        times = []
        for key in ("start", "end"):
            value = item.get(key)
            if value is None:
                times.append(None)
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                issues.append(m.ValidationIssue(
                    m.WARNING, "SHOTLIST_OVERLAY_INVALID",
                    f'shotlist.json: {where} "{key}" must be seconds from the start of the '
                    'shot (a number >= 0). Using the shot\'s own limit.'))
                times.append(None)
            else:
                times.append(float(value))
        if times[0] is not None and times[1] is not None and times[1] <= times[0]:
            issues.append(m.ValidationIssue(
                m.WARNING, "SHOTLIST_OVERLAY_INVALID",
                f'shotlist.json: {where} "end" must be after "start". Using the whole shot.'))
            times = [None, None]
        cue = _parse_overlay_cue(item.get("cue"))
        if item.get("cue") is not None and cue is None:
            issues.append(m.ValidationIssue(
                m.WARNING, "SHOTLIST_OVERLAY_INVALID",
                f'shotlist.json: {where} "cue" must be a cue number or a range like "14-15". '
                'Using the start/end seconds (or the whole shot).'))
        items.append({"text": text, "style": str(item.get("style") or "caption").strip().lower(),
                      "position": position, "anim": anim,
                      "start": times[0], "end": times[1], "cue": cue})
    return items or None


def _parse_motion(e, land=None):
    if isinstance(e, str):
        return mf.VisualMotion(e, None, None, None, _parse_land(land))
    if isinstance(e, dict):
        t = _get_string(e, "type")
        return mf.VisualMotion("STATIC" if t is None else t,
                               _get_number(e, "start_scale"), _get_number(e, "end_scale"),
                               _get_long(e, "duration_ms"),
                               _parse_land(land if land is not None else e.get("land")))
    return None


def _parse_transition(e):
    if isinstance(e, str):
        return mf.VisualTransition(e, 0)
    if isinstance(e, dict):
        t = _get_string(e, "type")
        d = _get_long(e, "duration_ms")
        return mf.VisualTransition("CUT" if t is None else t, 0 if d is None else d)
    return None


def _parse_framing(e):
    if isinstance(e, str):
        return mf.VisualFraming(e, None)
    if isinstance(e, dict):
        t = _get_string(e, "type")
        return mf.VisualFraming("wide" if t is None else t, _get_string(e, "focal_region"))
    return None


# ------------------------------------------------------------------ expander
@dataclass
class _Planned:
    shot_id: Optional[str]
    entry_number: int
    first_cue: int
    last_cue: int
    asset_id: str
    file: str
    start_ms: int
    end_ms: int
    narration: str
    motion: Optional[mf.VisualMotion]
    transition: Optional[mf.VisualTransition]
    framing: Optional[mf.VisualFraming]
    scene: Optional[str]
    intent: Optional[str]
    dropped: bool = False
    reveal_at_ms: Optional[list] = None
    reveal_layout: Optional[str] = None
    reveal_assets: Optional[list] = None
    sfx: Optional[list] = None
    overlays: Optional[list] = None


def expand(document: ShotListDocument, subtitles: list, image_file_names: list,
           transitions, issues: list, text_options=None) -> Optional[mf.VisualManifest]:
    entries = document.shots
    if not entries:
        issues.append(m.ValidationIssue(m.ERROR, "SHOTLIST_EMPTY",
                                        "shotlist.json contains no usable shots."))
        return None
    if not subtitles:
        issues.append(m.ValidationIssue(
            m.ERROR, "SHOTLIST_NO_SUBTITLES",
            "The SRT has no subtitle blocks; shotlist timings cannot be derived."))
        return None

    by_index: dict = {}
    for i, sub in enumerate(subtitles):
        by_index.setdefault(cue_index(sub, i), sub)

    by_stem: dict = {}
    for f in image_file_names:
        by_stem.setdefault(_stem(f).upper(), f)

    planned: list = []
    for n, entry in enumerate(entries):
        first_block = by_index.get(entry.first_cue)
        last_block = by_index.get(entry.last_cue)
        if first_block is None or last_block is None:
            issues.append(m.ValidationIssue(
                m.ERROR, "SHOTLIST_CUE_UNKNOWN",
                f"Shot {n + 1} (asset {entry.asset}): cue {entry.first_cue}–{entry.last_cue} does not exist — "
                f"the SRT has {len(subtitles)} blocks. Cue numbers are the SRT indices."))
            continue

        intent = None
        resolved = by_stem.get(_stem(entry.asset).upper())
        if resolved is not None:
            file = resolved
        else:
            best_stem, best_dist = None, 0
            norm_asset = _normalize(entry.asset)
            for f in image_file_names:
                stem = _stem(f)
                d = _levenshtein(norm_asset, _normalize(stem))
                if best_stem is None or d < best_dist:
                    best_stem, best_dist = stem, d
            if best_stem is not None and best_dist <= TYPO_DISTANCE_THRESHOLD:
                issues.append(m.ValidationIssue(
                    m.INFO, "SHOTLIST_ASSET_TYPO",
                    f'Shot {n + 1}: asset "{entry.asset}" looks like a typo of '
                    f'"{best_stem}" — if it really is a new image, keep the name '
                    "and generate it; otherwise fix the name."))
            file = entry.asset
            if not _has_extension(file):
                file += ".png"
            intent = document.prompts.get(entry.asset)
            issues.append(m.ValidationIssue(
                m.INFO, "SHOTLIST_GENERATE_AHEAD",
                f'Shot {n + 1}: "{file}" is not in images\\ yet — it is planned as a GENERATE request; '
                "the build report lists exactly what to draw."))

        start_ms = round(first_block.start_seconds * 1000)
        end_ms = round(last_block.end_seconds * 1000)
        if end_ms <= start_ms:
            issues.append(m.ValidationIssue(
                m.WARNING, "SHOTLIST_CUE_EMPTY",
                f"Shot {n + 1} (asset {entry.asset}): cues {entry.first_cue}–{entry.last_cue} "
                "have zero duration; dropped."))
            continue

        narration = re.sub(
            r"\s+", " ",
            " ".join(by_index[c].text for c in range(entry.first_cue, entry.last_cue + 1)
                     if c in by_index)).strip()

        reveal_at = _resolve_reveal(entry, n + 1, by_index, issues)
        reveal_assets = None if reveal_at is None else _resolve_reveal_assets(entry, by_stem)
        if reveal_assets is not None:
            file = reveal_assets[0]

        planned.append(_Planned(
            entry.shot_id, n + 1, entry.first_cue, entry.last_cue, file, file,
            start_ms, end_ms, narration,
            entry.motion, entry.transition, entry.framing, entry.scene,
            intent if intent is not None else document.prompts.get(file),
            reveal_at_ms=reveal_at,
            reveal_layout=None if reveal_at is None else entry.reveal_layout,
            reveal_assets=reveal_assets,
            sfx=entry.sfx,
            overlays=_finish_overlays(entry.overlays, entry.first_cue, entry.last_cue, by_index,
                                      start_ms / 1000.0, end_ms / 1000.0, text_options)))

    # 2. narration decides the order
    ordered = sorted(planned, key=lambda p: p.start_ms)
    if any(a is not b for a, b in zip(planned, ordered)):
        issues.append(m.ValidationIssue(
            m.INFO, "SHOTLIST_REORDERED",
            "shotlist.json entries were not in narration order; they were sorted by their cue start times."))

    # 3. duplicate ids
    seen_ids: set = set()
    for i, p in enumerate(ordered):
        if p.shot_id is None:
            continue
        key = p.shot_id.upper()
        if key in seen_ids:
            issues.append(m.ValidationIssue(
                m.ERROR, "SHOTLIST_DUPLICATE_ID",
                f'shotlist.json: shot id "{p.shot_id}" is used more than once; the later shot was dropped.'))
            ordered[i] = dataclasses.replace(p, dropped=True)
        else:
            seen_ids.add(key)

    # 4. overlaps
    kept: list = []
    for original in ordered:
        shot = original
        if shot.dropped:
            continue
        if kept and shot.start_ms < kept[-1].end_ms:
            if shot.end_ms <= kept[-1].end_ms:
                issues.append(m.ValidationIssue(
                    m.WARNING, "SHOTLIST_CUES_OVERLAP",
                    f"Shot {shot.entry_number} (cues {shot.first_cue}–{shot.last_cue}) is fully covered by the "
                    "previous shot's cues; dropped."))
                continue
            issues.append(m.ValidationIssue(
                m.WARNING, "SHOTLIST_CUES_OVERLAP",
                f"Shot {shot.entry_number} (cues {shot.first_cue}–{shot.last_cue}) overlaps the previous shot; "
                "its start was clamped to the previous shot's end."))
            shot = dataclasses.replace(shot, start_ms=kept[-1].end_ms)
            if shot.end_ms <= shot.start_ms:
                continue
        kept.append(shot)

    if not kept:
        issues.append(m.ValidationIssue(
            m.ERROR, "SHOTLIST_EMPTY",
            "shotlist.json produced no usable shots (unknown assets or cue problems)."))
        return None

    # 5. materialize
    assets: list = []
    asset_ids: set = set()
    shots: list = []
    for shot in kept:
        if shot.asset_id.upper() not in asset_ids:
            asset_ids.add(shot.asset_id.upper())
            assets.append(mf.VisualAsset(id=shot.asset_id, file=shot.file))
        for extra in shot.reveal_assets or []:
            if extra.upper() not in asset_ids:
                asset_ids.add(extra.upper())
                assets.append(mf.VisualAsset(id=extra, file=extra))

        t = shot.transition
        transition_out = None
        if t is not None and t.type.upper() != "CUT":
            up = t.type.upper()
            if up in ("CROSSFADE", "DIP", "DIP_WHITE", "SLIDE_LEFT", "SLIDE_RIGHT",
                      "SLIDE_UP", "SLIDE_DOWN"):
                transition_out = mf.VisualTransition(up, round(transitions.duration_seconds * 1000))
            else:
                transition_out = t

        shots.append(mf.VisualShot(
            shot_id=shot.shot_id if shot.shot_id is not None else f"shot-{shot.entry_number:03d}",
            start_ms=shot.start_ms,
            end_ms=shot.end_ms,
            scene_id=shot.scene if shot.scene is not None else "S01",
            beat_id=f"c{shot.first_cue}",
            srt_cue_ids=list(range(shot.first_cue, shot.last_cue + 1)),
            narration_text=shot.narration,
            visual_intent=shot.intent,
            change_reason=None,
            asset_id=shot.asset_id,
            framing=shot.framing,
            motion=shot.motion,
            transition_out=transition_out,
            reveal_at_ms=shot.reveal_at_ms,
            reveal_layout=shot.reveal_layout,
            reveal_assets=shot.reveal_assets,
            sfx=shot.sfx,
            overlays=shot.overlays))

    return mf.VisualManifest("1.0", mf.VisualManifestVideo(id="shotlist"), assets, shots)


def _resolve_reveal_assets(entry: ShotListEntry, by_stem: dict):
    if not entry.reveal_assets:
        return None
    out = []
    for n in entry.reveal_assets:
        f = by_stem.get(_stem(n).upper())
        out.append(f if f is not None else (n if _has_extension(n) else n + ".png"))
    return out


def _resolve_reveal(entry: ShotListEntry, number: int, by_index: dict, issues: list):
    cues = entry.reveal_cues
    if not cues or len(cues) < 2:
        return None
    problem = None
    for i in range(len(cues)):
        if problem is not None:
            break
        if cues[i] < entry.first_cue or cues[i] > entry.last_cue:
            problem = f"cue {cues[i]} is outside the shot's cues {entry.first_cue}-{entry.last_cue}"
        elif i > 0 and cues[i] <= cues[i - 1]:
            problem = "the cues must be in increasing order, one per item"
        elif cues[i] not in by_index:
            problem = f"cue {cues[i]} does not exist in the SRT"
    if problem is not None:
        issues.append(m.ValidationIssue(
            m.WARNING, "SHOTLIST_REVEAL_INVALID",
            f"Shot {number} (asset {entry.asset}): reveal ignored - {problem}. "
            "The whole image is shown instead."))
        return None
    if cues[0] != entry.first_cue:
        issues.append(m.ValidationIssue(
            m.INFO, "SHOTLIST_REVEAL_FIRST",
            f"Shot {number} (asset {entry.asset}): the first item shows from the shot's start (cue "
            f"{entry.first_cue}), not from cue {cues[0]}."))
    times = []
    for i, c in enumerate(cues):
        src = by_index[entry.first_cue] if i == 0 else by_index[c]
        times.append(round(src.start_seconds * 1000))
    return times


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _stem(name).lower())


def _levenshtein(a: str, b: str) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[len(b)]
