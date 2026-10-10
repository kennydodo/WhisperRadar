"""Port of ImgToVideo.Core.Reporting.BuildReport (build-report.json)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import models as m
from .options import ProjectOptions

CURRENT_SCHEMA_VERSION = 1


def _num(x):
    """Integral doubles are written without a fraction (System.Text.Json)."""
    return int(x) if isinstance(x, float) and x == int(x) else x


def _cs_name(snake: str) -> str:
    return "".join(p.capitalize() for p in snake.split("_"))


@dataclass
class BuildReport:
    project: str
    settings: dict
    inventory: dict
    timeline: dict
    issues: list
    coverage: Optional[dict] = None
    schema_version: int = CURRENT_SCHEMA_VERSION

    def to_dict(self) -> dict:
        d = {"schema_version": self.schema_version, "project": self.project,
             "settings": self.settings, "inventory": self.inventory,
             "timeline": self.timeline, "issues": self.issues}
        if self.coverage is not None:
            d["coverage"] = self.coverage
        return d


def _clip_report(clip: m.VideoClip) -> dict:
    code = next((c for c, mo in m.MOTION_CODES if mo == clip.motion), _cs_name(clip.motion))
    d = {"file": clip.file_path, "start_frame": clip.start_frame,
         "duration_frames": clip.duration_frames, "motion": code,
         "motion_source": _cs_name(clip.motion_source),
         "easing": m.EASING_NAMES.get(clip.easing) or _cs_name(clip.easing)}
    if clip.image_type is not None:
        d["image_type"] = _cs_name(clip.image_type)
    if clip.transition is not None:
        d["transition_in"] = m.TRANSITION_NAMES.get(clip.transition.kind) or _cs_name(clip.transition.kind)
    return d


def create(project_name: str, timeline: m.Timeline, options: ProjectOptions, issues,
           coverage=None) -> BuildReport:
    fps = options.output.fps
    scenes = [{"id": s.id, "start_frame": s.start_frame, "end_frame": s.end_frame,
               "clips": [_clip_report(c) for c in s.clips]} for s in timeline.scenes]
    t = options.timing
    tr = options.transitions
    settings = {
        "fps": _num(fps), "width": options.output.width, "height": options.output.height,
        "auto_motion_enabled": options.motion.auto_motion_enabled,
        "easing": m.EASING_NAMES.get(options.motion.easing) or _cs_name(options.motion.easing),
        "timing": {"floor_image_seconds": _num(t.floor_image_seconds),
                   "min_image_seconds": _num(t.min_image_seconds),
                   "preferred_image_seconds": _num(t.preferred_image_seconds),
                   "max_image_seconds": _num(t.max_image_seconds)},
        "transitions": {
            "enabled": tr.enabled,
            "kind": m.TRANSITION_NAMES.get(tr.kind) or _cs_name(tr.kind),
            "scene_boundary_kind": m.TRANSITION_NAMES.get(tr.scene_boundary_kind)
            or _cs_name(tr.scene_boundary_kind),
            "alignment": _cs_name(tr.alignment),
            "duration_frames": round(tr.duration_seconds * fps)}}
    inventory = {"scene_count": len(timeline.scenes),
                 "clip_count": sum(len(s.clips) for s in timeline.scenes)}
    if timeline.audio.file_path:
        inventory["audio_file"] = timeline.audio.file_path
    tl = {"total_frames": sum(c.duration_frames for s in timeline.scenes for c in s.clips),
          "scenes": scenes}
    issue_list = [{"severity": i.severity, "code": i.code, "message": i.message} for i in issues]
    cov = None
    if coverage is not None:
        cov = coverage.to_dict() if hasattr(coverage, "to_dict") else dict(coverage)
    return BuildReport(project_name, settings, inventory, tl, issue_list, cov)


def to_json(report: BuildReport) -> str:
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False)


def save(report: BuildReport, path) -> None:
    Path(path).write_text(to_json(report), encoding="utf-8")
