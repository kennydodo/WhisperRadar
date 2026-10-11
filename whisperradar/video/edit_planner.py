"""Port of ImgToVideo.Core.Planning.EditPlanner (v1 planner + dispatch to the manifest planner)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from . import manifest_planner
from . import models as m
from . import scene_inference
from . import timing_engine
from .motion_engine import MotionContext, MotionDecision, MotionEngine
from .netfmt import fixed
from .options import ProjectOptions
from .project_loader import ProjectInventory


@dataclass
class PlanningResult:
    success: bool
    timeline: Optional[m.Timeline]
    issues: list
    coverage: Optional[manifest_planner.ManifestCoverage] = None


def _issue(sev, code, msg):
    return m.ValidationIssue(sev, code, msg)


def plan(inventory: ProjectInventory, audio_duration_seconds: float,
         options: ProjectOptions) -> PlanningResult:
    issues = list(inventory.issues)
    if m.has_errors(issues):
        return PlanningResult(False, None, issues)
    if inventory.audio_file_path is None:
        issues.append(_issue(m.ERROR, "PLANNER_NO_AUDIO", "No audio file; cannot build."))
        return PlanningResult(False, None, issues)

    if inventory.manifest is not None:
        if options.planner.lower() != "v2":
            issues.append(_issue(
                m.INFO, "MANIFEST_IGNORED",
                'visual_manifest.json found but "planner" is "v1" — set "planner": "v2" in '
                "imgtovideo.json to plan from the manifest."))
        else:
            res = manifest_planner.plan(
                inventory.manifest, inventory.all_images, options,
                round(audio_duration_seconds * options.output.fps),
                inventory.overrides, inventory.audio_file_path)
            issues.extend(res.issues)
            if res.timeline is None or m.has_errors(issues):
                return PlanningResult(False, None, issues)
            if not verify_coverage(res.timeline, audio_duration_seconds, issues):
                return PlanningResult(False, None, issues)
            return PlanningResult(True, res.timeline, issues, res.coverage)

    scene_inputs = _build_scene_inputs(inventory, audio_duration_seconds, options, issues)
    if m.has_errors(issues):
        return PlanningResult(False, None, issues)

    timing = timing_engine.plan(scene_inputs, audio_duration_seconds, options,
                                _duration_override_lookup(inventory))
    issues.extend(timing.issues)
    if m.has_errors(issues):
        return PlanningResult(False, None, issues)

    timeline = _assemble_timeline(inventory, timing, audio_duration_seconds, options, issues)
    if not verify_coverage(timeline, audio_duration_seconds, issues):
        return PlanningResult(False, None, issues)
    return PlanningResult(True, timeline, issues)


def _build_scene_inputs(inventory, audio_seconds, options, issues) -> list:
    groups = inventory.scene_groups
    if not groups:
        issues.append(_issue(m.ERROR, "PLANNER_NO_SCENES", "No image scenes found."))
        return []
    if inventory.scene_map_windows is not None:
        if not inventory.scene_map_windows:
            issues.append(_issue(m.ERROR, "SCENE_MAP_EMPTY",
                                 "scenes.json exists but defines no usable scenes."))
            return []
        return _align_by_scene_id(inventory.scene_map_windows, groups, audio_seconds, issues)
    if not inventory.subtitles:
        issues.append(_issue(m.ERROR, "PLANNER_NO_SUBTITLES",
                             "No subtitle blocks available for scene inference."))
        return []
    inferred = scene_inference.infer(inventory.subtitles, options.scene_inference)
    return _align_by_order(inferred, groups, audio_seconds, issues)


def _align_by_scene_id(windows, groups, audio_seconds, issues) -> list:
    tiled = _tile_windows(windows, audio_seconds, issues, True)
    by_id: dict = {}
    for g in groups:
        by_id.setdefault(g.scene_id.lower(), g)   # ToDictionary would throw on duplicates
    inputs = []
    for w in tiled:
        g = by_id.pop(w.scene_id.lower(), None)
        if g is not None:
            inputs.append(m.ScenePlanInput(g.scene_id, w.start_seconds, w.end_seconds, g.images))
        else:
            issues.append(_issue(m.ERROR, "SCENE_MAP_NO_IMAGES",
                                 f"Scene map defines {w.scene_id} but no images match it."))
    for orphan in by_id.values():
        issues.append(_issue(
            m.ERROR, "SCENE_MAP_UNCOVERED",
            f"Scene {orphan.scene_id} has {len(orphan.images)} images but no entry in scenes.json."))
    return inputs


def _align_by_order(windows, groups, audio_seconds, issues) -> list:
    if len(windows) != len(groups):
        issues.append(_issue(
            m.WARNING, "SCENE_COUNT_MISMATCH",
            f"Narration suggests {len(windows)} scenes but images define {len(groups)}; "
            "images were distributed evenly across the audio duration (boundaries snapped to "
            "sentence pauses). Provide scenes.json to control which narration range each scene covers."))
        return _distribute_evenly(groups, audio_seconds, windows, issues)
    tiled = _tile_windows(windows, audio_seconds, issues, False)
    return [m.ScenePlanInput(groups[i].scene_id, tiled[i].start_seconds, tiled[i].end_seconds,
                             groups[i].images) for i in range(len(tiled))]


def _distribute_evenly(groups, audio_seconds, sentence_windows, issues) -> list:
    count = len(groups)
    inputs = []
    previous = 0.0
    snapped = [0]
    for i in range(count):
        if i == count - 1:
            end = audio_seconds
        else:
            raw = audio_seconds * (i + 1) / count
            lo = previous + m.FLOOR_IMAGE_SECONDS
            hi = audio_seconds - (count - 1 - i) * m.FLOOR_IMAGE_SECONDS
            end = _snap_boundary(raw, lo, hi, sentence_windows, snapped)
        inputs.append(m.ScenePlanInput(groups[i].scene_id, previous, end, groups[i].images))
        previous = end
    if snapped[0] > 0:
        issues.append(_issue(m.INFO, "SCENE_BOUNDARIES_SNAPPED",
                             f"{snapped[0]} of {count - 1} scene boundaries snapped to sentence pauses."))
    return inputs


def _snap_boundary(raw, lo, hi, sentence_windows, snapped) -> float:
    max_snap = 2.5
    best = raw
    best_distance = max_snap
    for w in sentence_windows:
        cand = w.end_seconds
        if cand <= lo or cand >= hi:
            continue
        d = abs(cand - raw)
        if d < best_distance:
            best_distance = d
            best = cand
    if abs(best - raw) > 0.001:
        snapped[0] += 1
    return best


def _tile_windows(windows, audio_seconds, issues, warn) -> list:
    tiled = [m.SceneWindow(w.scene_id, w.start_seconds, w.end_seconds)
             for w in sorted(windows, key=lambda w: w.start_seconds)]
    if not tiled:
        return tiled
    if tiled[0].start_seconds > 0.001:
        if warn:
            issues.append(_issue(
                m.WARNING, "SCENE_MAP_LEAD_GAP",
                f"First scene starts at {fixed(tiled[0].start_seconds, 2)}s; "
                "extended to 0s to cover the audio start."))
        tiled[0] = m.SceneWindow(tiled[0].scene_id, 0, tiled[0].end_seconds)
    for i in range(1, len(tiled)):
        if tiled[i].start_seconds > tiled[i - 1].end_seconds + 0.001:
            if warn:
                issues.append(_issue(
                    m.WARNING, "SCENE_MAP_GAP",
                    f"Gap between {tiled[i - 1].scene_id} and {tiled[i].scene_id}; "
                    f"{tiled[i - 1].scene_id} extended to cover it."))
            tiled[i - 1] = m.SceneWindow(tiled[i - 1].scene_id, tiled[i - 1].start_seconds,
                                         tiled[i].start_seconds)
    if tiled[-1].end_seconds < audio_seconds - 0.001:
        if warn:
            issues.append(_issue(
                m.WARNING, "SCENE_MAP_TRAIL_GAP",
                f"Last scene ends at {fixed(tiled[-1].end_seconds, 2)}s; extended to audio end "
                f"{fixed(audio_seconds, 2)}s."))
        tiled[-1] = m.SceneWindow(tiled[-1].scene_id, tiled[-1].start_seconds, audio_seconds)
    return tiled


def _duration_override_lookup(inventory) -> dict:
    result: dict = {}
    seen: set = set()
    for c in inventory.overrides.clips:
        if c.duration_frames is None:
            continue
        k = c.file.lower()
        if k in seen:
            continue
        seen.add(k)
        result[c.file] = c.duration_frames
    return result


def _assemble_timeline(inventory, timing, audio_seconds, options, issues) -> m.Timeline:
    engine = MotionEngine(options.motion, options.output)
    fps = options.output.fps
    transition_frames = round(options.transitions.duration_seconds * fps)

    timeline = m.Timeline(
        project_name=os.path.basename(inventory.project_folder.rstrip("/\\")),
        resolution=m.Resolution(options.output.width, options.output.height),
        fps=fps,
        audio=m.AudioTrack(inventory.audio_file_path, round(audio_seconds * fps)))

    previous = None
    previous_scene_id = ""
    shots_since_static = 0
    n_scenes = len(timing.scenes)

    for si, scene in enumerate(timing.scenes):
        timed_clips = scene.clips
        scene_clips = []
        for i, timed in enumerate(timed_clips):
            image = next(img for img in inventory.all_images if img.file_path == timed.file_path)
            ctx = MotionContext(
                timed.explicit_motion,
                is_video_start=si == 0 and i == 0,
                is_video_end=si == n_scenes - 1 and i == len(timed_clips) - 1,
                is_scene_start=i == 0,
                previous=previous,
                shots_since_static=shots_since_static)
            decision = engine.select_motion(ctx)
            clip_override = inventory.overrides.for_clip(timed.file_path)
            has_motion_override = clip_override is not None and clip_override.motion is not None
            requested_motion = clip_override.motion if has_motion_override else decision.motion
            requested_source = m.OVERRIDE if has_motion_override else decision.source

            fname = os.path.basename(timed.file_path)
            if requested_source == m.EXPLICIT_CODE:
                issues.append(_issue(
                    m.INFO, "MOTION_EXPLICIT",
                    f"{fname} uses explicit motion {_cs_name(requested_motion)}."))

            pan_right = ctx.previous != m.PAN_RIGHT
            plan_ = engine.plan_clip(requested_motion, requested_source, pan_right,
                                     image.width, image.height)
            effective_motion = plan_.motion
            effective_source = requested_source if plan_.motion == requested_motion else m.AUTO_SELECTED

            for w in plan_.warnings:
                issues.append(_issue(m.WARNING, "VIEWPORT", f"{fname}: {w}"))
            if image.width <= 0 or image.height <= 0:
                issues.append(_issue(
                    m.ERROR, "IMAGE_NO_DIMENSIONS",
                    f"{fname} has unknown dimensions; cannot compute viewports."))

            easing = (clip_override.easing if clip_override is not None and clip_override.easing is not None
                      else options.motion.easing)
            final = MotionDecision(effective_motion, effective_source)
            if options.transitions.enabled and (si != 0 or i > 0):
                is_boundary = i == 0 and len(previous_scene_id) > 0 and previous_scene_id != scene.scene_id
                cut_kind = inventory.overrides.cut_for(timed.file_path)
                if cut_kind is None:
                    cut_kind = (options.transitions.scene_boundary_kind if is_boundary
                                else options.transitions.kind)
                clip = _make_clip(timed, image.name.type, final, easing, plan_.start, plan_.end,
                                  m.TransitionIn(cut_kind, transition_frames))
            else:
                clip = _make_clip(timed, image.name.type, final, easing, plan_.start, plan_.end, None)

            scene_clips.append(clip)
            previous = effective_motion
            previous_scene_id = scene.scene_id
            shots_since_static = 0 if effective_motion == m.STATIC else shots_since_static + 1

        timeline.scenes.append(m.Scene(scene.scene_id, scene.start_frame, scene.end_frame, scene_clips))
    return timeline


def _cs_name(motion: str) -> str:
    return "".join(p.capitalize() for p in motion.split("_"))


def _make_clip(timed, image_type, decision, easing, start_vp, end_vp, transition) -> m.VideoClip:
    return m.VideoClip(
        file_path=timed.file_path, scene_id=timed.scene_id, start_frame=timed.start_frame,
        duration_frames=timed.duration_frames, motion=decision.motion,
        motion_source=decision.source, image_type=image_type, easing=easing,
        start_viewport=start_vp, end_viewport=end_vp, transition=transition)


def verify_coverage(timeline: m.Timeline, audio_duration_seconds: float, issues: list) -> bool:
    audio_frames = round(audio_duration_seconds * timeline.fps)
    cursor = 0
    for scene in timeline.scenes:
        for clip in scene.clips:
            if clip.start_frame != cursor:
                issues.append(_issue(
                    m.ERROR, "COVERAGE_BROKEN",
                    f"Coverage invariant violated at {clip.file_path}: expected start {cursor}, "
                    f"found {clip.start_frame}."))
                return False
            cursor += clip.duration_frames
    if cursor != audio_frames:
        issues.append(_issue(
            m.ERROR, "COVERAGE_AUDIO_MISMATCH",
            f"Timeline ends at frame {cursor} but audio lasts {audio_frames} frames."))
        return False
    return True
