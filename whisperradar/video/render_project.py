"""The `render-final` command of ImgToVideo.Cli/Program.cs (reveal stills, sfx, render, captions)."""
from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import threading
from typing import Callable, List, Optional, Sequence, Tuple

from . import imaging, reveal_images, sound_effects, timeline_json
from . import models as m
from . import options as options_mod
from .ffmpeg_runner import FfmpegRunner
from .render_plan import build, resolve_effective_ffmpeg_path
from .render_service import PreviewRenderService

SRT_NAMES = ()


def final_render_options(options: options_mod.ProjectOptions) -> options_mod.ProjectOptions:
    """Render at project resolution with the final preset/CRF/B-frames (a deep clone)."""
    clone = copy.deepcopy(options)
    clone.render.preview_width = clone.output.width
    clone.render.preview_height = clone.output.height
    clone.render.preview_preset = clone.render.final_preset
    clone.render.preview_crf = clone.render.final_crf
    clone.render.preview_bframes = clone.render.final_bframes
    return clone


def images_from_timeline(timeline: m.Timeline) -> List[m.ImageInfo]:
    """Inventory of the source images a timeline references (clip files + reveal sources) that exist."""
    paths: List[str] = []
    seen = set()

    def add(p):
        if p and p.lower() not in seen:
            seen.add(p.lower())
            paths.append(p)

    for scene in timeline.scenes:
        for clip in scene.clips:
            add(clip.file_path)
            if clip.reveal is not None:
                add(clip.reveal.source_path)
                for s in clip.reveal.sources:
                    add(s)
    result = []
    for p in paths:
        size = imaging.try_read(p) if os.path.isfile(p) else None
        if size:
            w, h = size[0], size[1]
            stem = os.path.splitext(os.path.basename(p))[0]
            result.append(m.ImageInfo(p, m.ParsedImageName(stem, 0, 0), w, h))
    return result


MOTION_GATE_FILE = "motion_gate.json"
_log = logging.getLogger(__name__)


def motion_gate(folder: str) -> dict:
    """The merge step records {"enabled": bool, "window_seconds": number?}
    when it resolves the channel's animated opening. No gate file (standalone
    use) means enabled with no window limit - the original behaviour."""
    try:
        with open(os.path.join(folder, MOTION_GATE_FILE), encoding="utf-8") as f:
            gate = json.load(f)
        return gate if isinstance(gate, dict) else {}
    except (OSError, ValueError):
        return {}


def motion_enabled(folder: str) -> bool:
    """False when the merge step recorded that the channel's animated opening
    is OFF (clips left in clips\\ must then not be used)."""
    return bool(motion_gate(folder).get("enabled", True))


def attach_motion_clips(timeline: m.Timeline, folder: str) -> int:
    """Point clips at their AI videos (clips\\<image stem>.mp4) when they exist.

    Render-time only (not saved into timeline.json). Reveal clips keep their
    own handling. A clip is NOT used when the channel's animated opening is
    off, when the shot has fallen outside the current window (animate_seconds
    was lowered after the clips were made), or when its still was regenerated
    after the clip was made (the pair would no longer match). Returns how
    many clips now play a video."""
    if not motion_enabled(folder):
        _log.info("animated opening is off for this channel - clips in "
                  "clips\\ are ignored")
        return 0
    gate = motion_gate(folder)
    try:
        window = float(gate.get("window_seconds") or 0) or None
    except (TypeError, ValueError):
        window = None
    fps = float(getattr(timeline, "fps", 0) or 0) or None
    n = skipped_window = 0
    for scene in timeline.scenes:
        for clip in scene.clips:
            if clip.reveal is not None or not clip.file_path:
                continue
            if window and fps and clip.start_frame / fps >= window - 1e-6:
                skipped_window += 1
                continue
            stem = os.path.splitext(os.path.basename(clip.file_path))[0]
            cand = os.path.join(folder, "clips", stem + ".mp4")
            if os.path.isfile(cand) and os.path.getsize(cand) > 10_000:
                try:
                    stale = (os.path.getmtime(clip.file_path)
                             > os.path.getmtime(cand) + 2.0)
                except OSError:
                    stale = False       # image not on disk here: cannot tell
                if stale:
                    _log.warning(
                        "clip %s.mp4 is older than its image - the still was "
                        "regenerated after the clip; using the still instead "
                        "(re-run the motion stage for this shot)", stem)
                    continue
                clip.video_path = cand
                n += 1
    if skipped_window:
        _log.info("%d clip(s) sit past the animated opening's current window "
                  "(%.0fs) and were left as stills", skipped_window, window)
    return n


def find_srt(project_folder: str) -> Optional[str]:
    try:
        names = sorted(os.listdir(project_folder))
    except OSError:
        return None
    for n in names:
        if n.lower().endswith(".srt"):
            return os.path.join(project_folder, n)
    return None


def render_final(project_folder: str, preview: bool,
                 progress: Optional[Callable[[float], None]] = None,
                 images: Optional[Sequence[m.ImageInfo]] = None,
                 timeline: Optional[m.Timeline] = None,
                 options: Optional[options_mod.ProjectOptions] = None,
                 srt_path: Optional[str] = None,
                 issue_sink: Optional[Callable[[m.ValidationIssue], None]] = None,
                 cancel_event: Optional[threading.Event] = None,
                 max_parallelism: int = 2,
                 reuse_unchanged_segments: bool = False) -> Tuple[bool, str, List[str]]:
    """Render out/timeline.json. Returns (success, output_path, errors).

    preview=True -> out/preview.mp4; else out/final/final.mp4 (+ captions.srt copied beside it).
    """
    folder = os.path.abspath(project_folder)
    if options is None:
        options = options_mod.load_or_default(os.path.join(folder, "imgtovideo.json"))
    out_dir = os.path.join(folder, "out")
    if timeline is None:
        tl_path = os.path.join(out_dir, "timeline.json")
        if not os.path.isfile(tl_path):
            return False, "", [f"timeline not found: {tl_path} (run the planner first)"]
        timeline = timeline_json.load(tl_path)
    if images is None:
        images = images_from_timeline(timeline)
    if srt_path is None:
        srt_path = find_srt(folder)

    try:
        ffmpeg_path = resolve_effective_ffmpeg_path(options.render)
        runner = FfmpegRunner(ffmpeg_path)
        images = reveal_images.ensure(timeline, images, runner, cancel_event)
        attach_motion_clips(timeline, folder)
        issues = sound_effects.resolve(timeline, folder, runner, options.render.ffprobe_path,
                                       options.sound.default_pop, cancel_event=cancel_event)
        if issue_sink is not None:
            for i in issues:
                issue_sink(i)

        service = PreviewRenderService(runner)
        if preview:
            plan = build(timeline, images, options, os.path.join(out_dir, "render"),
                         os.path.join(out_dir, "preview.mp4"))
            result = service.render(plan, max_parallelism, progress, cancel_event,
                                    reuse_unchanged_segments)
        else:
            ro = final_render_options(options)
            final_dir = os.path.join(out_dir, "final")
            plan = build(timeline, images, ro, os.path.join(final_dir, "render"),
                         os.path.join(final_dir, "final.mp4"))
            result = service.render(plan, max_parallelism, progress, cancel_event,
                                    reuse_unchanged_segments)
            if result.success and srt_path:
                shutil.copyfile(srt_path, os.path.join(final_dir, "captions.srt"))
    except RuntimeError as e:
        return False, "", [str(e)]
    return result.success, result.output_path, list(result.errors)
