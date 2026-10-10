"""Port of ImgToVideo.Core.Timing.TimingEngine."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import models as m
from .netfmt import fixed
from .options import ProjectOptions


@dataclass
class TimedClip:
    file_path: str
    scene_id: str
    start_frame: int
    duration_frames: int
    explicit_motion: Optional[str] = None


@dataclass
class TimedScene:
    scene_id: str
    start_frame: int
    end_frame: int
    clips: list = field(default_factory=list)


@dataclass
class TimingResult:
    scenes: list
    issues: list


def lookup_override(overrides, file_path):
    """TryGetValue on the C# dictionary (case-insensitive when built by the planner)."""
    if not overrides:
        return None
    if file_path in overrides:
        return overrides[file_path]
    low = file_path.lower()
    for k, v in overrides.items():
        if k.lower() == low:
            return v
    return None


def plan(scenes, audio_duration_seconds: float, options: ProjectOptions,
         duration_overrides: Optional[dict] = None) -> TimingResult:
    issues: list = []
    fps = options.output.fps
    audio_frames = round(audio_duration_seconds * fps)
    ranges = _compute_scene_frame_ranges(scenes, audio_frames, fps)
    _shift_paired_boundaries(scenes, ranges, duration_overrides, issues)

    timed = []
    for i, scene in enumerate(scenes):
        start, end = ranges[i]
        if end <= start:
            issues.append(m.ValidationIssue(
                m.ERROR, "TIMING_SCENE_EMPTY",
                f"Scene {scene.scene_id} has a non-positive duration after quantization."))
            continue
        timed.append(_split_scene(scene, start, end, fps, options, duration_overrides, issues))
    return TimingResult(timed, issues)


def _compute_scene_frame_ranges(scenes, audio_frames, fps):
    ranges = []
    previous_end = 0
    for i in range(len(scenes)):
        start = previous_end
        is_last = i == len(scenes) - 1
        end = audio_frames if is_last else round(scenes[i].end_seconds * fps)
        if end < start:
            end = start
        ranges.append([start, end])
        previous_end = end
    return ranges


def _shift_paired_boundaries(scenes, ranges, overrides, issues):
    if overrides is None:
        return
    deltas = [None] * len(scenes)
    for i, sc in enumerate(scenes):
        images = sc.images
        if len(images) == 0:
            continue
        applied = 0
        full = True
        for image in images:
            frames = lookup_override(overrides, image.file_path)
            if frames is not None and frames > 0:
                applied += frames
            else:
                full = False
                break
        deltas[i] = applied - (ranges[i][1] - ranges[i][0]) if full else None
    for i in range(len(scenes) - 1):
        delta, nxt = deltas[i], deltas[i + 1]
        if delta is None or nxt is None or delta == 0 or nxt != -delta:
            continue
        new_end = ranges[i][1] + delta
        new_start = ranges[i + 1][0] + delta
        if new_end - ranges[i][0] < 1 or ranges[i + 1][1] - new_start < 1:
            issues.append(m.ValidationIssue(
                m.WARNING, "TIMING_OVERRIDE_IGNORED",
                f"Scenes {scenes[i].scene_id}/{scenes[i + 1].scene_id}: "
                "boundary shift leaves no room; ignoring it."))
            continue
        ranges[i][1] = new_end
        ranges[i + 1][0] = new_start
        deltas[i] = 0
        deltas[i + 1] = 0


def _split_scene(scene, start_frame, end_frame, fps, options, overrides, issues):
    total = end_frame - start_frame
    images = scene.images
    count = len(images)
    timing = options.timing
    if count == 0:
        issues.append(m.ValidationIssue(
            m.ERROR, "TIMING_NO_IMAGES", f"Scene {scene.scene_id} has no images."))
        return TimedScene(scene.scene_id, start_frame, end_frame, [])

    base_seconds = total / fps / count
    if base_seconds < timing.floor_image_seconds:
        issues.append(m.ValidationIssue(
            m.ERROR, "TIMING_OVERPACKED",
            f"Scene {scene.scene_id} contains {count} images in {fixed(total / fps, 1)}s; even the "
            f"{fixed(timing.floor_image_seconds, 1)}s floor cannot fit. "
            "Remove images or extend the scene."))
    elif base_seconds > timing.max_image_seconds:
        issues.append(m.ValidationIssue(
            m.WARNING, "TIMING_HOLD",
            f"Scene {scene.scene_id} contains {count} images in {fixed(total / fps, 1)}s; the last image "
            f"holds past the {fixed(timing.max_image_seconds, 1)}s maximum."))
    elif base_seconds < timing.min_image_seconds:
        issues.append(m.ValidationIssue(
            m.WARNING, "TIMING_DENSITY",
            f"Scene {scene.scene_id} contains {count} images in {fixed(total / fps, 1)}s; images show "
            f"~{fixed(base_seconds, 1)}s each, below the {fixed(timing.min_image_seconds, 1)}s minimum."))

    durations = [0] * count
    if base_seconds > timing.max_image_seconds:
        max_frames = round(timing.max_image_seconds * fps)
        assigned = 0
        for i in range(count - 1):
            durations[i] = max_frames
            assigned += max_frames
        durations[count - 1] = max(1, total - assigned)
    else:
        per = max(1, total // count)
        extra = total - per * count
        for i in range(count):
            durations[i] = per + (1 if i < extra else 0)

    if overrides is not None:
        _apply_duration_overrides(scene, images, durations, total, overrides, issues)

    clips = []
    cursor = start_frame
    for i in range(count):
        clips.append(TimedClip(images[i].file_path, scene.scene_id, cursor, durations[i],
                               images[i].name.code))
        cursor += durations[i]
    return TimedScene(scene.scene_id, start_frame, end_frame, clips)


def _apply_duration_overrides(scene, images, durations, total_frames, overrides, issues):
    applied = []
    for i, img in enumerate(images):
        frames = lookup_override(overrides, img.file_path)
        if frames is not None and frames > 0:
            applied.append((i, frames))
    if not applied:
        return
    others = len(images) - len(applied)
    remaining = total_frames - sum(f for _, f in applied)
    if (remaining < max(1, others) and others > 0) or (others == 0 and remaining != 0):
        issues.append(m.ValidationIssue(
            m.WARNING, "TIMING_OVERRIDE_IGNORED",
            f"Scene {scene.scene_id}: duration overrides do not fit the scene window; ignoring them."))
        return
    for index, frames in applied:
        durations[index] = frames
    if others > 0:
        per_other = remaining // others
        extra = remaining - per_other * others
        applied_idx = {a[0] for a in applied}
        other_index = 0
        for i in range(len(images)):
            if i in applied_idx:
                continue
            durations[i] = per_other + (1 if other_index < extra else 0)
            other_index += 1
