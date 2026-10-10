"""overrides.json (ImgToVideo.Core.Overrides): per-clip/per-shot/per-cut editor overrides."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import models as m

CURRENT_SCHEMA_VERSION = 1
FILE_NAME = "overrides.json"


def _normalize(path: str) -> str:
    return path.replace("\\", "/")


@dataclass
class ClipOverride:
    file: str = ""
    shot: Optional[str] = None
    motion: Optional[str] = None
    easing: Optional[str] = None
    duration_frames: Optional[int] = None
    exclude: bool = False
    order: Optional[int] = None

    def to_dict(self) -> dict:
        d: dict = {"file": self.file}
        for k in ("shot", "motion", "easing", "duration_frames"):
            if getattr(self, k) is not None:
                d[k] = getattr(self, k)
        d["exclude"] = self.exclude
        if self.order is not None:
            d["order"] = self.order
        return d


@dataclass
class CutOverride:
    before_file: str = ""
    transition: str = "crossfade"

    def to_dict(self) -> dict:
        return {"before_file": self.before_file, "transition": self.transition}


@dataclass
class ProjectOverrides:
    schema_version: int = CURRENT_SCHEMA_VERSION
    clips: list = field(default_factory=list)
    cuts: list = field(default_factory=list)

    def for_clip(self, file_path: str) -> Optional[ClipOverride]:
        key = _normalize(file_path).lower()
        for c in self.clips:
            if _normalize(c.file).lower() == key:
                return c
        return None

    def for_shot(self, shot_id) -> Optional[ClipOverride]:
        if not shot_id:
            return None
        sid = shot_id.lower()
        for c in self.clips:
            if c.shot is not None and c.shot.lower() == sid:
                return c
        return None

    def cut_for(self, incoming_file_path: str) -> Optional[str]:
        key = _normalize(incoming_file_path).lower()
        for c in self.cuts:
            if _normalize(c.before_file).lower() == key:
                return c.transition
        return None

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version,
                "clips": [c.to_dict() for c in self.clips],
                "cuts": [c.to_dict() for c in self.cuts]}


def _enum(value, allowed, what):
    if not isinstance(value, str):
        raise ValueError(f"{what} value is null.")
    if value not in allowed:
        raise ValueError(f'Unknown {what} "{value}".')
    return value


def load_from_json(text: str) -> ProjectOverrides:
    """ValueError (incl. json.JSONDecodeError) on a broken file."""
    data = json.loads(text)
    ver = data.get("schema_version") if isinstance(data, dict) else None
    if isinstance(ver, bool) or not isinstance(ver, (int, float)):
        raise ValueError("overrides.json is missing schema_version.")
    if int(ver) != CURRENT_SCHEMA_VERSION:
        raise ValueError(f"Unsupported overrides schema_version {int(ver)} "
                         f"(supported: {CURRENT_SCHEMA_VERSION}).")
    result = ProjectOverrides(int(ver))
    for c in data.get("clips") or []:
        co = ClipOverride(file=c.get("file") or "")
        co.shot = c.get("shot")
        if c.get("motion") is not None:
            co.motion = _enum(c["motion"], m.MOTIONS, "motion")
        if c.get("easing") is not None:
            co.easing = _enum(c["easing"], m.EASINGS, "easing")
        if c.get("duration_frames") is not None:
            co.duration_frames = int(c["duration_frames"])
        co.exclude = bool(c.get("exclude", False))
        if c.get("order") is not None:
            co.order = int(c["order"])
        result.clips.append(co)
    for c in data.get("cuts") or []:
        t = c.get("transition", "crossfade")
        result.cuts.append(CutOverride(c.get("before_file") or "",
                                       _enum(t, m.TRANSITION_KINDS, "transition kind")))
    return result


def load(path) -> ProjectOverrides:
    return load_from_json(Path(path).read_text(encoding="utf-8-sig"))


def load_or_default(path) -> ProjectOverrides:
    return load(path) if Path(path).exists() else ProjectOverrides()


def to_json(overrides: ProjectOverrides) -> str:
    return json.dumps(overrides.to_dict(), indent=2)


def save(overrides: ProjectOverrides, path) -> None:
    Path(path).write_text(to_json(overrides), encoding="utf-8")
