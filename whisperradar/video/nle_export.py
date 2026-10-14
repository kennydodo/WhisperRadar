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


def _freeze_frames(timeline, folder: str, runner: FfmpegRunner,
                   cancel_event=None) -> dict:
    """Last frame of every attached clip's mp4, for the hold part of shots
    that outlast the 8s media (the NLE equivalent of the render's tpad
    clone). Returns {mp4 path: freeze png path}."""
    from .. import motion
    out = {}
    render_dir = os.path.join(folder, "out", "render")
    os.makedirs(render_dir, exist_ok=True)
    for clip in (c for s in timeline.scenes for c in s.clips
                 if getattr(c, "video_path", None)):
        # any shot that can outlast its 8s media (MAX_STRETCH + a margin for
        # the transition trims) may need the freeze
        if clip.duration_frames / timeline.fps <= motion.MAX_STRETCH_SECONDS - 1.0:
            continue
        png = os.path.join(render_dir, f"freeze_{os.path.splitext(os.path.basename(clip.video_path))[0]}.png")
        if not (os.path.exists(png) and os.path.getmtime(png) >= os.path.getmtime(clip.video_path)):
            args = ["-y", "-v", "error", "-sseof", "-0.1", "-i", clip.video_path,
                    "-update", "1", "-frames:v", "1", png]
            proc = runner.run(args, cancel_event=cancel_event)
            if not proc.success or not os.path.exists(png):
                raise RuntimeError(
                    f'Could not extract the hold frame of "{os.path.basename(clip.video_path)}": '
                    f"{proc.error_tail}")
        out[clip.video_path] = png
    return out


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
    # Shots that outlast their 8s clip need the mp4's last frame to freeze on
    # (the render's tpad clone); export them once, here.
    freezes = _freeze_frames(planned.timeline, folder, runner)
    issues = sound_effects.resolve(planned.timeline, folder, runner, options.render.ffprobe_path,
                                   options.sound.default_pop)
    if issue_sink is not None:
        for i in issues:
            issue_sink(i)
    return folder, os.path.join(folder, "out"), planned.timeline, images, options, freezes


def export_premiere(project_folder: str, issue_sink: Optional[Callable] = None) -> str:
    """Write out/premiere.xml and return its path."""
    folder, out_dir, timeline, images, options, freezes = _prepare(project_folder, issue_sink)
    xml = premiere_export.export(timeline, images, True, options, freezes=freezes)
    path = os.path.join(out_dir, "premiere.xml")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(xml)
    return path


def export_capcut(project_folder: str, issue_sink: Optional[Callable] = None) -> Tuple[str, str]:
    """Write the CapCut draft folder out/capcut/<project name>/. Returns (draft folder,
    draft_content.json path)."""
    folder, out_dir, timeline, images, _options, freezes = _prepare(project_folder, issue_sink)
    draft_folder = os.path.join(out_dir, "capcut", os.path.basename(folder.rstrip("\\/")))
    content = capcut_export.export(timeline, images, draft_folder, freezes=freezes)
    return draft_folder, content
