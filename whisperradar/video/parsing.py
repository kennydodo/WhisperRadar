"""SRT, image-filename, natural-sort and scenes.json parsing (ImgToVideo.Core.Parsing)."""
from __future__ import annotations

import json
import re
from functools import cmp_to_key
from pathlib import Path
from typing import Optional

from . import models as m
from .options import NamingOptions

_TS = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})$")
_WS = re.compile(r"\s+")


# ---------------------------------------------------------------- SRT
def parse_srt(content: str) -> list:
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    if content[:1] == "﻿":
        content = content[1:]
    lines = content.split("\n")
    blocks: list = []
    i = 0
    while i < len(lines):
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i >= len(lines):
            break
        index = 0
        first = lines[i].strip()
        try:
            index = int(first)
            i += 1
        except ValueError:
            pass
        if i >= len(lines):
            raise ValueError(f"SRT block {index}: missing timecode line.")
        start, end = _parse_time_line(lines[i].strip(), index)
        i += 1
        text_lines = []
        while i < len(lines) and lines[i].strip():
            text_lines.append(lines[i].rstrip())
            i += 1
        blocks.append(m.SubtitleBlock(index, start, end, "\n".join(text_lines)))
    return blocks


def cue_index(block: m.SubtitleBlock, position: int) -> int:
    """SRT index when present, else the block's 1-based position."""
    return block.index if block.index > 0 else position + 1


def to_compact_narration(content: str) -> str:
    blocks = parse_srt(content)
    if not blocks:
        return ""
    out = []
    for i, b in enumerate(blocks):
        out.append(f"[{cue_index(b, i)}] {_WS.sub(' ', b.text).strip()}\n")
    return "".join(out)


def _parse_time_line(line: str, block_index: int):
    sep = line.find("-->")
    if sep < 0:
        raise ValueError(f'SRT block {block_index}: invalid timecode line "{line}".')
    return (_parse_ts(line[:sep].strip(), block_index),
            _parse_ts(line[sep + 3:].strip(), block_index))


def _parse_ts(value: str, block_index: int) -> float:
    mt = _TS.match(value)
    if not mt:
        raise ValueError(f'SRT block {block_index}: invalid timestamp "{value}".')
    h, mi, s = int(mt.group(1)), int(mt.group(2)), int(mt.group(3))
    ms = int(mt.group(4).ljust(3, "0"))
    return h * 3600 + mi * 60 + s + ms / 1000.0


# ---------------------------------------------------------------- natural sort
def _read_number(s: str, i: int):
    value = 0
    while i < len(s) and s[i].isdigit():
        value = value * 10 + int(s[i])
        i += 1
    return value, i


def natural_compare(x: Optional[str], y: Optional[str]) -> int:
    if x is y:
        return 0
    if x is None:
        return -1
    if y is None:
        return 1
    ix = iy = 0
    while ix < len(x) and iy < len(y):
        if x[ix].isdigit() and y[iy].isdigit():
            nx, ix = _read_number(x, ix)
            ny, iy = _read_number(y, iy)
            if nx != ny:
                return -1 if nx < ny else 1
        else:
            a, b = x[ix].upper(), y[iy].upper()
            if a != b:
                return -1 if a < b else 1
            ix += 1
            iy += 1
    a, b = len(x) - ix, len(y) - iy
    return (a > b) - (a < b)


natural_key = cmp_to_key(natural_compare)


# ---------------------------------------------------------------- image filenames
class ImageFilenameParser:
    def __init__(self, naming: Optional[NamingOptions] = None):
        self._naming = naming or NamingOptions()
        self._sep = self._naming.separator
        prefix = re.escape(self._naming.scene_prefix)
        sep = re.escape(self._sep)
        if self._naming.motion_codes_enabled or self._naming.type_codes_enabled:
            suffix = (f"(?:{sep}(?P<suffixes>[A-Za-z]{{2,4}}(?:{sep}[A-Za-z]{{2,4}})*))?")
        else:
            suffix = ""
        self._pattern = re.compile(
            rf"^{prefix}(?P<scene>\d+){sep}(?P<index>\d+){suffix}$", re.IGNORECASE)

    def try_parse(self, file_name: str) -> Optional[m.ParsedImageName]:
        stem = Path(file_name.replace("\\", "/")).stem
        mt = self._pattern.match(stem)
        if not mt:
            return None
        scene, image = int(mt.group("scene")), int(mt.group("index"))
        code = None
        itype = None
        unknown = False
        suffixes = mt.groupdict().get("suffixes")
        if suffixes:
            segments = [s for s in suffixes.split(self._sep) if s]
            res = self._classify(segments)
            if res is None:
                return None
            code, itype, unknown = res
        return m.ParsedImageName(stem, scene, image, code, unknown, itype)

    def _classify(self, segments):
        code = itype = None
        unknown = False
        pos = len(segments) - 1
        if pos >= 0 and m.motion_from_suffix(segments[pos]) is not None:
            if not self._naming.motion_codes_enabled:
                return None
            code = m.motion_from_suffix(segments[pos])
            pos -= 1
        if pos >= 0 and m.image_type_from_code(segments[pos]) is not None:
            if not self._naming.type_codes_enabled:
                return None
            itype = m.image_type_from_code(segments[pos])
            pos -= 1
        if pos >= 0:
            unknown = True
        return code, itype, unknown

    def format_example(self, scene=8, index=2, type_="SCN", code="PR") -> str:
        n = self._naming
        pad = min(max(n.number_padding, 1), 4)
        name = f"{n.scene_prefix}{scene:0{pad}d}{n.separator}{index:0{pad}d}"
        if type_ is not None and n.type_codes_enabled:
            name += f"{n.separator}{type_.upper()}"
        if code is not None and n.motion_codes_enabled:
            name += f"{n.separator}{code.upper()}"
        return name


# ---------------------------------------------------------------- scenes.json
def parse_scene_map(text: str):
    """-> (windows, issues). Port of SceneMapParser.Parse."""
    def fail(msg):
        return [], [m.ValidationIssue(m.ERROR, "SCENE_MAP", msg)]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return fail(f"scenes.json is not valid JSON: {e}")
    if not isinstance(data, list):
        return fail("scenes.json must contain a JSON array of scene entries.")
    issues: list = []
    windows: list = []
    seen: set = set()

    def num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    for el in data:
        if (not isinstance(el, dict) or not isinstance(el.get("scene"), str)
                or not num(el.get("start")) or not num(el.get("end"))):
            issues.append(m.ValidationIssue(
                m.ERROR, "SCENE_MAP_ENTRY",
                "scenes.json contains an invalid entry (expected { scene, start, end })."))
            continue
        sid, start, end = el["scene"], float(el["start"]), float(el["end"])
        if not sid:
            issues.append(m.ValidationIssue(
                m.ERROR, "SCENE_MAP_ENTRY", "scenes.json contains an entry with an empty scene id."))
            continue
        if sid.lower() in seen:
            issues.append(m.ValidationIssue(
                m.ERROR, "SCENE_MAP_DUPLICATE", f"scenes.json lists scene {sid} more than once."))
            continue
        seen.add(sid.lower())
        if end <= start:
            issues.append(m.ValidationIssue(
                m.ERROR, "SCENE_MAP_RANGE",
                f"scenes.json entry {sid} has end <= start ({end:g} <= {start:g})."))
            continue
        windows.append(m.SceneWindow(sid, start, end))
    windows.sort(key=lambda w: w.start_seconds)  # stable, like List.Sort on distinct starts
    for i in range(1, len(windows)):
        if windows[i].start_seconds < windows[i - 1].end_seconds - 0.001:
            issues.append(m.ValidationIssue(
                m.ERROR, "SCENE_MAP_OVERLAP",
                f"scenes.json scenes {windows[i - 1].scene_id} and {windows[i].scene_id} overlap."))
    return windows, issues
