"""Explainer planner: explainer.json scenes -> engine Timeline.

The timeline's clips reference PREPARED clips (out/explainer/<scene>_<hash>.mp4)
via the explicit explainer marker; motion/reveal/zoompan never touch them.

Prepared clips are prepared lazily here (temp+atomic rename, content-hash
filenames, ffprobe-verified) so timeline.json only ever references real files.
"""
from __future__ import annotations

import os

from . import models as m
from . import options as options_mod
from .explainer import prepare as prep
from .explainer import validate as va
from .explainer.schema import FILE_NAME
from .explainer import timing as tg
from .explainer.validate import ExplainerError


def explainer_file_path(folder: str) -> str:
    return os.path.join(folder, FILE_NAME)


def has_explainer(folder: str) -> bool:
    return os.path.isfile(explainer_file_path(folder))


def issues_from(explainer_error: ExplainerError) -> list:
    return [m.ValidationIssue(m.ERROR, "EXPLAINER_INVALID", e)
            for e in explainer_error.errors]


def plan(inventory, audio_duration_seconds: float, options, project_folder: str,
         visual_mode: str = "motion_graphics"):
    """-> edit_planner.PlanningResult-shaped result.

    visual_mode: motion_graphics | whiteboard. The doc's style decides the
    scene rendering; an explicit whiteboard mode renders stroke scenes even
    if the sidecar omits "style" (mode wins, doc 16.1)."""
    from .edit_planner import PlanningResult
    from .explainer.schema import STYLE_WHITEBOARD

    issues = []
    fps = float(options.output.fps)
    try:
        doc = va.load(explainer_file_path(project_folder),
                      project_folder=project_folder)
        if visual_mode == "whiteboard" and doc.style != STYLE_WHITEBOARD:
            doc.style = STYLE_WHITEBOARD
            if doc.theme.name != "whiteboard_light":
                from .explainer.themes import resolve as resolve_theme
                doc.theme = resolve_theme("whiteboard_light")
        issues.append(m.ValidationIssue(
            m.INFO, "EXPLAINER_MODE",
            f"Planner branch: {visual_mode} ({len(doc.scenes)} scene(s))."))
        cues = inventory.subtitles or []
        if not cues and inventory.srt_file_path:
            from . import parsing
            with open(inventory.srt_file_path, encoding="utf-8-sig") as fh:
                cues = parsing.parse_srt(fh.read())
        if not cues:
            issues.append(m.ValidationIssue(
                m.ERROR, "EXPLAINER_NO_CUES",
                "explainer.json needs subtitles.srt to anchor cue timings."))
            return PlanningResult(False, None, issues)
        compiled = tg.compile_doc(doc, cues, fps,
                                  total_seconds=audio_duration_seconds)
        conflicts = va.event_property_conflicts(doc)
        if conflicts:
            issues.extend(m.ValidationIssue(m.ERROR, "EXPLAINER_INVALID", c)
                          for c in conflicts)
            return PlanningResult(False, None, issues)

        width = options.output.width
        height = options.output.height
        from whisperradar.video import tool_locator
        ffmpeg_path = tool_locator.resolve(
            "ffmpeg", options.render.ffmpeg_path or "ffmpeg")
        clips = []
        for cs in compiled:
            path = prep.render_scene_clip(
                doc, cs, ffmpeg_path, project_folder, fps,
                canvas=(width, height))
            clips.append((cs, path))
    except ExplainerError as e:
        return PlanningResult(False, None, issues_from(e))
    except RuntimeError as e:
        issues.append(m.ValidationIssue(m.ERROR, "EXPLAINER_PREPARE_FAILED", str(e)))
        return PlanningResult(False, None, issues)

    timeline = _build_timeline(doc, clips, audio_duration_seconds, options,
                               project_folder, issues)
    if m.has_errors(issues):
        return PlanningResult(False, None, issues)
    return PlanningResult(True, timeline, issues, None)


def _build_timeline(doc, clips, audio_seconds, options, project_folder, issues):
    fps = float(options.output.fps)
    total_frames = tg.frames(audio_seconds, fps)
    timeline = m.Timeline()
    timeline.project_name = os.path.basename(os.path.abspath(project_folder))
    timeline.resolution = m.Resolution(options.output.width, options.output.height)
    timeline.fps = fps
    timeline.audio = m.AudioTrack(
        file_path=_find_audio(project_folder) or "",
        duration_frames=total_frames)
    for cs, path in clips:
        clip = m.VideoClip(
            file_path=os.path.abspath(path),
            scene_id=cs.spec.id,
            shot_id=cs.spec.shot_id,
            start_frame=cs.start_frame,
            duration_frames=cs.end_frame - cs.start_frame,
            motion=m.STATIC,
            motion_source=m.EXPLICIT_CODE,
            explainer_clip=True,
        )
        scene = m.Scene(id=cs.spec.id, start_frame=cs.start_frame,
                        end_frame=cs.end_frame, clips=[clip])
        timeline.scenes.append(scene)
    issues.append(m.ValidationIssue(
        m.INFO, "EXPLAINER_MODE",
        f"Planned {len(clips)} explainer scene(s) from explainer.json "
        "(no image/AI stages involved)."))
    return timeline


def _find_audio(project_folder: str):
    for n in sorted(os.listdir(project_folder)):
        if n.lower().endswith((".mp3", ".wav", ".m4a", ".aac", ".flac")):
            return os.path.join(project_folder, n)
    audio_dir = os.path.join(project_folder, "audio")
    if os.path.isdir(audio_dir):
        for n in sorted(os.listdir(audio_dir)):
            if n.lower().endswith((".mp3", ".wav", ".m4a", ".aac", ".flac")):
                return os.path.join(audio_dir, n)
    return None
