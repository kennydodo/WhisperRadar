"""The `plan` command of the ImgToVideo CLI: load options, validate, load the project, plan,
write out/timeline.json and out/build-report.json."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from . import build_report, edit_planner, options as options_mod, project_loader, timeline_json
from . import models as m


@dataclass
class PlanProjectResult:
    success: bool
    timeline: Optional[m.Timeline] = None
    report: Optional[build_report.BuildReport] = None
    issues: list = field(default_factory=list)
    config_errors: list = field(default_factory=list)
    inventory: Optional[project_loader.ProjectInventory] = None
    timeline_path: Optional[str] = None
    report_path: Optional[str] = None


def plan_project(project_folder, audio_seconds: Optional[float] = None,
                 options: Optional[options_mod.ProjectOptions] = None) -> PlanProjectResult:
    folder = os.path.abspath(str(project_folder))
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"project folder not found: {folder}")

    if options is None:
        options_path = os.path.join(folder, "imgtovideo.json")
        if not os.path.isfile(options_path):
            options_mod.save(options_mod.ProjectOptions(), options_path)
        options = options_mod.load_or_default(options_path)

    config_errors = options.validate()
    if config_errors:
        return PlanProjectResult(False, config_errors=config_errors)

    inventory = project_loader.load(folder, options)
    if inventory.audio_file_path is None:
        return PlanProjectResult(False, issues=list(inventory.issues), inventory=inventory)

    if audio_seconds is None:
        from .ffprobe import ffprobe_duration_seconds
        audio_seconds = ffprobe_duration_seconds(inventory.audio_file_path, options.render.ffprobe_path)

    planned = edit_planner.plan(inventory, audio_seconds, options)
    if planned.timeline is None or not planned.success:
        return PlanProjectResult(False, issues=planned.issues, inventory=inventory)

    out_dir = os.path.join(folder, "out")
    os.makedirs(out_dir, exist_ok=True)
    timeline_path = os.path.join(out_dir, "timeline.json")
    report_path = os.path.join(out_dir, "build-report.json")
    timeline_json.save(planned.timeline, timeline_path)
    report = build_report.create(os.path.basename(folder.rstrip("/\\")), planned.timeline, options,
                                 planned.issues, planned.coverage)
    build_report.save(report, report_path)
    return PlanProjectResult(True, planned.timeline, report, planned.issues, [], inventory,
                             timeline_path, report_path)
