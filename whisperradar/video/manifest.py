"""visual_manifest.json model (ImgToVideo.Core.Manifest.VisualManifest). snake_case JSON,
None fields omitted on save. Shared by the shotlist expander and the manifest planner."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Optional

SUPPORTED_SCHEMA_VERSION = "1.0"
FILE_NAME = "visual_manifest.json"


@dataclass
class FocalRegion:
    id: str = ""
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0


@dataclass
class VisualAsset:
    id: str = ""
    file: str = ""
    scene_id: Optional[str] = None
    beat_ids: Optional[list] = None
    type: Optional[str] = None
    safe_motion: Optional[list] = None
    allow_reuse: Optional[bool] = None
    focal_regions: Optional[list] = None      # [FocalRegion]


@dataclass
class VisualFraming:
    type: str = ""
    focal_region: Optional[str] = None


@dataclass
class VisualMotion:
    type: str = ""
    start_scale: Optional[float] = None
    end_scale: Optional[float] = None
    duration_ms: Optional[int] = None
    # PU/PD only: how far down the picture (0 = top edge, 100 = bottom edge) the
    # shot's subject sits. The tilt ends there instead of at the picture's edge.
    land: Optional[float] = None


@dataclass
class VisualTransition:
    type: str = ""
    duration_ms: int = 0


@dataclass
class VisualShot:
    shot_id: str = ""
    start_ms: int = 0
    end_ms: int = 0
    scene_id: Optional[str] = None
    beat_id: Optional[str] = None
    srt_cue_ids: Optional[list] = None
    narration_text: Optional[str] = None
    visual_intent: Optional[str] = None
    change_reason: Optional[str] = None
    asset_id: str = ""
    framing: Optional[VisualFraming] = None
    motion: Optional[VisualMotion] = None
    transition_out: Optional[VisualTransition] = None
    reveal_at_ms: Optional[list] = None
    reveal_layout: Optional[str] = None
    reveal_assets: Optional[list] = None
    sfx: Optional[list] = None
    overlays: Optional[list] = None


@dataclass
class VisualManifestVideo:
    id: Optional[str] = None
    title: Optional[str] = None
    duration_ms: Optional[int] = None
    fps: Optional[float] = None


@dataclass
class VisualManifest:
    schema_version: str = SUPPORTED_SCHEMA_VERSION
    video: VisualManifestVideo = field(default_factory=VisualManifestVideo)
    assets: list = field(default_factory=list)       # [VisualAsset]
    timeline: list = field(default_factory=list)     # [VisualShot]

    def to_dict(self) -> dict:
        return _dump(self)

    @staticmethod
    def from_dict(d: dict) -> "VisualManifest":
        return VisualManifest(
            schema_version=d.get("schema_version", SUPPORTED_SCHEMA_VERSION),
            video=_build(VisualManifestVideo, d.get("video") or {}),
            assets=[_asset(a) for a in d.get("assets") or []],
            timeline=[_shot(s) for s in d.get("timeline") or []])


def _dump(obj):
    if is_dataclass(obj):
        out = {}
        for f in fields(obj):
            v = getattr(obj, f.name)
            if v is None:
                continue
            out[f.name] = _dump(v)
        return out
    if isinstance(obj, list):
        return [_dump(x) for x in obj]
    return obj


def _build(cls, d: dict):
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})


def _asset(d: dict) -> VisualAsset:
    a = _build(VisualAsset, {k: v for k, v in d.items() if k != "focal_regions"})
    if d.get("focal_regions") is not None:
        a.focal_regions = [_build(FocalRegion, r) for r in d["focal_regions"]]
    return a


def _shot(d: dict) -> VisualShot:
    skip = {"framing", "motion", "transition_out"}
    s = _build(VisualShot, {k: v for k, v in d.items() if k not in skip})
    if d.get("framing") is not None:
        s.framing = _build(VisualFraming, d["framing"])
    if d.get("motion") is not None:
        s.motion = _build(VisualMotion, d["motion"])
    if d.get("transition_out") is not None:
        s.transition_out = _build(VisualTransition, d["transition_out"])
    return s


def exists(project_folder) -> bool:
    return (Path(project_folder) / FILE_NAME).exists()


def load(project_folder) -> Optional[VisualManifest]:
    """None when absent; ValueError on a broken manifest."""
    path = Path(project_folder) / FILE_NAME
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    ver = data.get("schema_version") if isinstance(data, dict) else None
    if not isinstance(ver, str):
        raise ValueError("visual_manifest.json is missing schema_version.")
    if ver != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(f'Unsupported visual_manifest schema_version "{ver}" '
                         f'(supported: "{SUPPORTED_SCHEMA_VERSION}").')
    return VisualManifest.from_dict(data)


def save(manifest: VisualManifest, path) -> None:
    Path(path).write_text(json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False),
                          encoding="utf-8")
