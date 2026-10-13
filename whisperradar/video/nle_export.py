"""`export-premiere` / `export-capcut` commands of ImgToVideo.Cli: plan the project, make the
reveal stills, resolve the sound effects, then write the editor project under out\\."""
from __future__ import annotations

import os
from typing import Callable, Optional, Tuple

from . import plan as plan_mod
from . import capcut_export, premiere_export, reveal_images, sound_effects
from . import options as options_mod
from .ffmpeg_runner import FfmpegRunner
from .render_plan import resolve_effective_ffmpeg_path


def _prepare(project_folder: str, issue_sink: Optional[Callable] = None):
    """Plan + reveal stills + sound effects. Returns (folder, out_dir, timeline, images, options)."""
    folder = os.path.abspath(project_folder)
    options = options_mod.load_or_default(os.path.join(folder, "imgtovideo.json"))
    planned = plan_mod.plan_project(folder, options=options)
    if not planned.success or planned.timeline is None:
        errs = [i.message for i in planned.issues if i.severity == "Error"]
        errs += list(planned.config_errors or [])
        raise RuntimeError("planning failed: " + "; ".join(errs)[:600])
    runner = FfmpegRunner(resolve_effective_ffmpeg_path(options.render))
    images = list(planned.inventory.all_images) if planned.inventory else []
    images = reveal_images.ensure(planned.timeline, images, runner, None)
    # The animated opening: point clips at their AI videos (clips\<stem>.mp4),
    # exactly like the preview render does - without this the NLE projects
    # carry only the PNG stills and Premiere/CapCut never see the clips.
    from .render_project import attach_motion_clips
    attach_motion_clips(planned.timeline, folder)
    issues = sound_effects.resolve(planned.timeline, folder, runner, options.render.ffprobe_path,
                                   options.sound.default_pop)
    if issue_sink is not None:
        for i in issues:
            issue_sink(i)
    return folder, os.path.join(folder, "out"), planned.timeline, images, options


def export_premiere(project_folder: str, issue_sink: Optional[Callable] = None) -> str:
    """Write out/premiere.xml and return its path."""
    folder, out_dir, timeline, images, options = _prepare(project_folder, issue_sink)
    xml = premiere_export.export(timeline, images, True, options)
    path = os.path.join(out_dir, "premiere.xml")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(xml)
    return path


def export_capcut(project_folder: str, issue_sink: Optional[Callable] = None) -> Tuple[str, str]:
    """Write the CapCut draft folder out/capcut/<project name>/. Returns (draft folder,
    draft_content.json path)."""
    folder, out_dir, timeline, images, _options = _prepare(project_folder, issue_sink)
    draft_folder = os.path.join(out_dir, "capcut", os.path.basename(folder.rstrip("\\/")))
    content = capcut_export.export(timeline, images, draft_folder)
    return draft_folder, content
