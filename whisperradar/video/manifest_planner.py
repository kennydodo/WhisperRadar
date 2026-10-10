"""Port of ImgToVideo.Core.Manifest.ManifestPlanner (v2 planner: VisualManifest -> Timeline)."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Optional

from . import models as m
from . import reveal_paths
from .motion_engine import MotionEngine
from .netfmt import fixed, general, optional, signed
from .options import MotionOptions, ProjectOptions

MIN_REVEAL_STEP_FRAMES = 4

_ASSET_TYPE_MAP = {"closeup": "close_up", "infographic": "infographic",
                   "comparison": "comparison", "lifestyle": "scene", "character": "scene"}


@dataclass
class ManifestCoverage:
    duration_seconds: float = 0.0
    shot_count: int = 0
    asset_count: int = 0
    unique_assets_used: int = 0
    average_seconds_per_source_change: float = 0.0
    longest_same_source_seconds: float = 0.0
    longest_shot_seconds: float = 0.0
    missing_assets: list = field(default_factory=list)
    unused_assets: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass
class ManifestPlanResult:
    timeline: Optional[m.Timeline]
    coverage: Optional[ManifestCoverage]
    issues: list


@dataclass
class _RevealPlan:
    at_ms: list
    layout: str
    sources: Optional[list]


def _cs_enum_name(motion: str) -> str:
    """C# MotionType member name (PanLeft) as printed by ToString()."""
    return "".join(p.capitalize() for p in motion.split("_"))


def _issue(sev, code, msg):
    return m.ValidationIssue(sev, code, msg)


def plan(manifest, images, options: ProjectOptions, audio_frames: int, overrides,
         audio_file_path: Optional[str] = None) -> ManifestPlanResult:
    issues: list = []
    fps = options.output.fps

    mf = manifest.video.fps
    if mf is not None and abs(mf - fps) > 0.01:
        issues.append(_issue(m.ERROR, "MANIFEST_FPS_MISMATCH",
                             f"Manifest fps {general(mf)} does not match the project output fps {general(fps)}."))
        return ManifestPlanResult(None, None, issues)

    assets_by_id: dict = {}          # lower-cased id -> asset (OrdinalIgnoreCase)
    for asset in manifest.assets or []:
        if not asset.id or not asset.id.strip():
            issues.append(_issue(m.ERROR, "MANIFEST_INVALID", "An asset entry is missing its id."))
            continue
        key = asset.id.lower()
        if key in assets_by_id:
            issues.append(_issue(m.ERROR, "MANIFEST_INVALID",
                                 f'Asset id "{asset.id}" is defined more than once.'))
        else:
            assets_by_id[key] = asset

    images_by_name: dict = {}
    for i in images:
        images_by_name.setdefault(_basename(i.file_path).lower(), i)

    missing_assets: list = []
    planned: list = []               # [shot, asset, image]
    reveal_sources: dict = {}
    pending = [0]
    for shot in manifest.timeline or []:
        duration_ms = max(0, shot.end_ms - shot.start_ms)
        so = overrides.for_shot(shot.shot_id)
        if so is not None and so.exclude:
            issues.append(_issue(
                m.INFO, "SHOT_EXCLUDED",
                f'Shot "{shot.shot_id}" is excluded by overrides; its screen time was absorbed by the neighbouring shot.'))
            _absorb_drop(planned, duration_ms, pending)
            continue
        asset_id = shot.asset_id
        if not asset_id:
            issues.append(_issue(m.ERROR, "MANIFEST_INVALID",
                                 f'Shot "{shot.shot_id}" references no asset_id; it was dropped.'))
            _absorb_drop(planned, duration_ms, pending)
            continue
        asset = assets_by_id.get(asset_id.lower())
        if asset is None:
            issues.append(_issue(
                m.ERROR, "MANIFEST_INVALID",
                f'Shot "{shot.shot_id}" references unknown asset "{asset_id}"; it was dropped.'))
            _absorb_drop(planned, duration_ms, pending)
            continue
        image = images_by_name.get((asset.file or "").lower())
        if image is None or image.width <= 0:
            missing_assets.append(_describe_missing_asset(shot, asset))
            _absorb_drop(planned, duration_ms, pending)
            continue

        if shot.reveal_assets:
            sources: list = []
            any_missing = False
            for extra_id in shot.reveal_assets:
                extra_asset = assets_by_id.get(extra_id.lower())
                extra_image = (images_by_name.get((extra_asset.file or "").lower())
                               if extra_asset is not None else None)
                if extra_image is not None and extra_image.width > 0:
                    sources.append(extra_image.file_path)
                else:
                    any_missing = True
                    known = extra_asset.file if extra_asset is not None else extra_id
                    missing_assets.append(
                        f'Asset "{extra_id}" (file "images/{known}") is missing — generate it.\n'
                        f"  Used by reveal shot {shot.shot_id} (images appear one after another)")
            if any_missing:
                _absorb_drop(planned, duration_ms, pending)
                continue
            reveal_sources[shot.shot_id] = sources

        if shot.end_ms <= shot.start_ms:
            issues.append(_issue(m.WARNING, "MANIFEST_SHOT_EMPTY",
                                 f'Shot "{shot.shot_id}" has end_ms <= start_ms; it was dropped.'))
            continue

        adjusted = (dataclasses.replace(shot, start_ms=shot.start_ms - pending[0])
                    if pending[0] > 0 else shot)
        pending[0] = 0
        planned.append([adjusted, asset, image])

    if not planned:
        issues.append(_issue(m.ERROR, "MANIFEST_NO_SHOTS",
                             "The manifest timeline has no renderable shots (all assets missing or invalid)."))
        return ManifestPlanResult(None, None, issues)

    clips: list = []                 # [clip, asset]
    reveals: dict = {}               # id(clip) -> _RevealPlan
    cursor = 0
    for i, (shot, asset, image) in enumerate(planned):
        start = round(shot.start_ms * fps / 1000.0)
        end = round(shot.end_ms * fps / 1000.0)
        duration = max(1, end - start)

        if i == 0 and start > 0:
            issues.append(_issue(
                m.INFO, "MANIFEST_TIMING_FIXED",
                f"Manifest timeline started at frame {start}; the first shot was extended to cover from 0."))
            duration += start
            start = 0

        if i > 0 and start > cursor:
            gap = start - cursor
            if gap > 2:
                issues.append(_issue(
                    m.INFO, "MANIFEST_TIMING_FIXED",
                    f'Narration pause of {optional(gap / float(fps), 2)}s before shot "{shot.shot_id}" '
                    "is held by the previous shot."))
            clips[-1][0].duration_frames += gap
            cursor = start
        elif i > 0 and start < cursor:
            issues.append(_issue(
                m.WARNING, "MANIFEST_TIMING_FIXED",
                f'Overlap before shot "{shot.shot_id}" ({signed(cursor - start)} frames) '
                "was closed by clamping to the previous shot."))
            duration = max(1, duration - (cursor - start))
            start = cursor

        if i == len(planned) - 1 and start + duration != audio_frames:
            drift = start + duration - audio_frames
            issues.append(_issue(
                m.INFO if abs(drift) <= 2 else m.WARNING, "MANIFEST_TIMING_FIXED",
                f"Manifest timeline ends {signed(drift)} frames off the audio duration "
                f"({fixed(audio_frames / fps, 2)}s); the last shot was "
                f"{'trimmed' if drift > 0 else 'extended'} to match."))
            duration = max(1, audio_frames - start)

        clip = _build_clip(shot, asset, image, start, duration, fps, options, overrides, issues)
        if shot.reveal_at_ms is not None and len(shot.reveal_at_ms) >= m.REVEAL_MIN_ITEMS:
            if clip.motion != m.STATIC:
                issues.append(_issue(
                    m.WARNING, "REVEAL_NEEDS_STATIC",
                    f'Shot "{shot.shot_id}" asks for a reveal but its motion is {_cs_enum_name(clip.motion)}; '
                    "a reveal shot must be static (ST). The whole image is shown instead."))
            else:
                reveals[id(clip)] = _RevealPlan(
                    list(shot.reveal_at_ms), m.reveal_layout_normalize(shot.reveal_layout),
                    reveal_sources.get(shot.shot_id))
        clips.append([clip, asset])
        cursor = start + duration

    shots_list = [p[0] for p in planned]
    _apply_transitions(clips, shots_list, fps, options, overrides)

    override_applied = False
    for clip, _ in clips:
        so = overrides.for_shot(clip.shot_id)
        if so is not None and so.duration_frames is not None and so.duration_frames > 0 \
                and so.duration_frames != clip.duration_frames:
            clip.duration_frames = so.duration_frames
            override_applied = True

    planned_sum = sum(c[0].duration_frames for c in clips)
    if override_applied and planned_sum != audio_frames:
        issues.append(_issue(
            m.WARNING, "MANIFEST_OVERRIDE_IGNORED",
            "Per-shot duration edits do not sum to the audio duration; ignoring them. "
            "Use the nudge sliders (which transfer frames between shots) instead of editing single Frames boxes."))
        for clip, _ in clips:
            shot = next(p[0] for p in planned if p[0].shot_id == clip.shot_id)
            clip.duration_frames = max(1, round(shot.end_ms * fps / 1000.0)
                                       - round(shot.start_ms * fps / 1000.0))
        fix = audio_frames - sum(c[0].duration_frames for c in clips)
        clips[-1][0].duration_frames = max(1, clips[-1][0].duration_frames + fix)

    cursor_frames = 0
    for clip, _ in clips:
        clip.start_frame = cursor_frames
        cursor_frames += clip.duration_frames

    warn_hold = options.timing.warn_hold_seconds
    for i, (clip, _) in enumerate(clips):
        shot = planned[i][0]
        issues.append(_issue(
            m.INFO, "SHOT_TIMING",
            f'Shot "{shot.shot_id}" ({clip.scene_id}) · cues {_format_cues(shot.srt_cue_ids)} · '
            f"{_format_ms(clip.start_frame * 1000.0 / fps)} → "
            f"{_format_ms((clip.start_frame + clip.duration_frames) * 1000.0 / fps)} "
            f"({fixed(clip.duration_frames / float(fps), 1)} s) · "
            f'"{_truncate_narration(shot.narration_text)}"'))
        if warn_hold > 0 and clip.duration_frames > round(warn_hold * fps):
            tail = ("" if _blank(shot.narration_text)
                    else f'\n  Narration: "{_truncate_narration(shot.narration_text)}"')
            issues.append(_issue(
                m.WARNING, "SHOT_HOLD_LONG",
                f'Shot "{shot.shot_id}" holds {optional(clip.duration_frames / float(fps), 1)}s — verify it is '
                "one continuously developing idea, or split it in shotlist.json." + tail))

    v = manifest.video
    timeline = m.Timeline(
        resolution=m.Resolution(options.output.width, options.output.height),
        fps=fps,
        audio=m.AudioTrack(audio_file_path or "", audio_frames))
    timeline.project_name = v.title if v.title is not None else (v.id if v.id is not None else "manifest")
    final_clips = _expand_reveals([c[0] for c in clips], reveals, fps, options, issues)
    timeline.sounds.extend(_build_sounds(final_clips, shots_list))
    groups: dict = {}
    for c in final_clips:
        groups.setdefault(c.scene_id, []).append(c)
    for scene_id, group in groups.items():
        timeline.scenes.append(m.Scene(scene_id, group[0].start_frame,
                                       group[-1].start_frame + group[-1].duration_frames, group))

    coverage = _build_coverage(manifest, assets_by_id, clips, audio_frames, fps, missing_assets, issues)
    return ManifestPlanResult(timeline, coverage, issues)


# ------------------------------------------------------------------ helpers
def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _blank(s) -> bool:
    return s is None or not s.strip()


def _absorb_drop(planned, duration_ms, pending):
    if planned:
        prev = planned[-1]
        planned[-1] = [dataclasses.replace(prev[0], end_ms=prev[0].end_ms + duration_ms),
                       prev[1], prev[2]]
    else:
        pending[0] += duration_ms


def _format_cues(cues) -> str:
    if not cues:
        return "?"
    ordered = sorted(set(cues))
    parts = []
    run_start = run_end = ordered[0]
    for i in range(1, len(ordered) + 1):
        if i < len(ordered) and ordered[i] == run_end + 1:
            run_end = ordered[i]
            continue
        parts.append(f"{run_start}" if run_start == run_end else f"{run_start}-{run_end}")
        if i < len(ordered):
            run_start = run_end = ordered[i]
    return ", ".join(parts)


def _format_ms(ms: float) -> str:
    total = round(ms)
    return f"{total // 60000}:{total // 1000 % 60:02d}.{total % 1000:03d}"


def _truncate_narration(text) -> str:
    if _blank(text):
        return "—"
    clean = text.strip()
    return clean if len(clean) <= 90 else clean[:90] + "…"


def _describe_missing_asset(shot, asset) -> str:
    narration = "" if _blank(shot.narration_text) else f'\n  Narration: "{shot.narration_text}"'
    intent = "" if shot.visual_intent is None else f"\n  Intent: {shot.visual_intent}"
    change = "" if shot.change_reason is None else f" ({shot.change_reason})"
    return (f'Asset "{asset.id}" (file "images/{asset.file}") is missing — generate it.'
            f"\n  Used by shot {shot.shot_id} for beat {shot.beat_id if shot.beat_id is not None else '?'}"
            f"{change}{intent}{narration}")


def _try_parse_motion(text: str):
    normalized = text.strip().upper()
    if normalized in ("STATIC", "NONE"):
        return m.STATIC
    return m.motion_from_suffix(normalized)


def _is_pan_motion(motion) -> bool:
    return motion in (m.PAN_LEFT, m.PAN_RIGHT, m.PAN_UP, m.PAN_DOWN, m.PAN_REVEAL)


def _resolve_easing(motion, motion_seconds: float, mo: MotionOptions) -> str:
    if _is_pan_motion(motion) and motion_seconds > mo.long_pan_linear_threshold_seconds:
        return m.LINEAR
    return mo.easing


def _build_clip(shot, asset, image, start, duration, fps, options, overrides, issues) -> m.VideoClip:
    shot_override = overrides.for_shot(shot.shot_id)

    motion_text = (shot.motion.type if shot.motion is not None else None) or "STATIC"
    motion_source = m.EXPLICIT_CODE
    motion = _try_parse_motion(motion_text)
    if motion is None:
        issues.append(_issue(
            m.WARNING, "MANIFEST_MOTION_UNKNOWN",
            f'Shot "{shot.shot_id}" requests unknown motion "{motion_text}"; using STATIC.'))
        motion = m.STATIC
        motion_text = "STATIC"
        motion_source = m.AUTO_SELECTED
    elif shot.motion is None:
        from_name = image.name.code
        if from_name is not None:
            motion = from_name
            motion_text = _cs_enum_name(from_name).upper()
            issues.append(_issue(
                m.INFO, "MOTION_FROM_FILENAME",
                f'Shot "{shot.shot_id}" specifies no motion; using the filename code {motion_text} '
                "(the image was composed for it)."))
        else:
            motion = m.STATIC
            motion_text = "STATIC"
            motion_source = m.AUTO_SELECTED

    if shot_override is not None and shot_override.motion is not None:
        motion = shot_override.motion
        motion_text = _cs_enum_name(motion).upper()
        motion_source = m.OVERRIDE

    safe = {s.upper() for s in (asset.safe_motion or [])}
    if ((shot_override is None or shot_override.motion is None) and safe
            and motion != m.STATIC and motion_text.upper() not in safe):
        issues.append(_issue(
            m.WARNING, "MANIFEST_UNSAFE_MOTION",
            f'Shot "{shot.shot_id}" uses {motion_text.upper()} but asset '
            f'"{asset.id}" declares safe motion: {", ".join(asset.safe_motion or [])}.'))

    motion_duration_frames = None
    motion_ms = (shot.motion.duration_ms if shot.motion is not None and shot.motion.duration_ms is not None
                 else options.motion.motion_duration_ms)
    if motion_ms > 0 and motion != m.STATIC:
        motion_frames = round(motion_ms * fps / 1000.0)
        if motion_frames < duration:
            motion_duration_frames = max(2, motion_frames)

    start_vp, end_vp = _resolve_viewports(shot, asset, motion, motion_source, image, options, issues)

    effective_seconds = (motion_duration_frames if motion_duration_frames is not None else duration) / fps
    easing = (shot_override.easing if shot_override is not None and shot_override.easing is not None
              else _resolve_easing(motion, effective_seconds, options.motion))

    return m.VideoClip(
        file_path=image.file_path, shot_id=shot.shot_id, scene_id=shot.scene_id or "S01",
        start_frame=start, duration_frames=duration, motion=motion, motion_source=motion_source,
        image_type=_ASSET_TYPE_MAP.get((asset.type or "").lower()), easing=easing,
        start_viewport=start_vp, end_viewport=end_vp, motion_duration_frames=motion_duration_frames)


def _apply_transitions(clips, shots, fps, options, overrides):
    for i in range(len(clips) - 1):
        incoming = clips[i + 1][0]
        transition = shots[i].transition_out
        ttype = ((transition.type if transition is not None else None) or "CUT").upper()
        frames = round((transition.duration_ms if transition is not None else 0) * fps / 1000.0)

        editor_cut = overrides.cut_for(incoming.file_path)
        if editor_cut is not None:
            editor_frames = round(options.transitions.duration_seconds * fps)
            incoming_tr = (None if editor_cut == "none" or editor_frames < 2
                           else m.TransitionIn(editor_cut, editor_frames))
        elif transition is not None and ttype != "CUT":
            if ttype == "CROSSFADE" and frames >= 2:
                incoming_tr = m.TransitionIn("crossfade", frames)
            elif ttype == "DIP" and frames >= 2:
                incoming_tr = m.TransitionIn("fade_black", frames)
            elif ttype == "DIP_WHITE" and frames >= 2:
                incoming_tr = m.TransitionIn("fade_white", frames)
            else:
                incoming_tr = None
        elif options.transitions.enabled:
            is_boundary = clips[i][0].scene_id.lower() != incoming.scene_id.lower()
            settings_frames = round(options.transitions.duration_seconds * fps)
            incoming_tr = (m.TransitionIn(
                options.transitions.scene_boundary_kind if is_boundary else options.transitions.kind,
                settings_frames) if settings_frames >= 2 else None)
        else:
            incoming_tr = None
        incoming.transition = incoming_tr


def _resolve_viewports(shot, asset, motion, motion_source, image, options, issues):
    if motion not in (m.STATIC, m.ZOOM_IN, m.ZOOM_OUT):
        pan = MotionEngine(options.motion, options.output).plan_clip(
            motion, motion_source, motion != m.PAN_LEFT, image.width, image.height)
        return pan.start, pan.end
    base = _resolve_base_rect(shot, asset, image, options, issues)
    s0, s1 = _resolve_scales(shot, motion, options.motion)
    return _scale_rect(base, s0, image), _scale_rect(base, s1, image)


def _centered_band(width, height, fraction) -> m.Rect:
    return m.Rect(0, (height - height * fraction) / 2, width, height * fraction)


def _resolve_base_rect(shot, asset, image, options, issues) -> m.Rect:
    framing = ((shot.framing.type if shot.framing is not None else None) or "wide").lower()
    w = float(image.width)
    h = float(image.height)
    if framing == "wide":
        return m.Rect(0, 0, w, h)
    if framing == "medium":
        return _centered_band(w, h, 0.85)
    if framing == "close":
        return _centered_band(w, h, 0.70)
    if framing == "detail":
        return _centered_band(w, h, 0.50)
    if framing == "crop":
        region_id = shot.framing.focal_region if shot.framing is not None else None
        region = None
        for r in asset.focal_regions or []:
            if region_id is not None and r.id is not None and r.id.lower() == region_id.lower():
                region = r
                break
        if region is None:
            issues.append(_issue(
                m.WARNING, "MANIFEST_FOCAL_MISSING",
                f'Shot "{shot.shot_id}" requests crop "{region_id if region_id is not None else "?"}" but asset '
                f'"{asset.id}" defines no such focal region; using the full image.'))
            return m.Rect(0, 0, w, h)
        rect = m.Rect(region.x * w, region.y * h, region.width * w, region.height * h)
        target = options.output.width / options.output.height
        aspect = rect.width / rect.height
        if abs(aspect - target) > 0.2:
            issues.append(_issue(
                m.WARNING, "MANIFEST_FOCAL_REFRAMED",
                f'Shot "{shot.shot_id}": focal region "{region.id}" has aspect {fixed(aspect, 2)} vs output '
                f"{fixed(target, 2)}; the viewport was re-framed to the largest 16:9 window inside the region."))
        if aspect > target:
            new_w, new_h = rect.height * target, rect.height
        else:
            new_w, new_h = rect.width, rect.width / target
        return m.Rect(rect.x + (rect.width - new_w) / 2, rect.y + (rect.height - new_h) / 2, new_w, new_h)
    issues.append(_issue(m.WARNING, "MANIFEST_FRAMING_UNKNOWN",
                         f'Shot "{shot.shot_id}" requests unknown framing "{framing}"; using wide.'))
    return m.Rect(0, 0, w, h)


def _resolve_scales(shot, motion, mo: MotionOptions):
    ss = shot.motion.start_scale if shot.motion is not None else None
    es = shot.motion.end_scale if shot.motion is not None else None
    if motion == m.ZOOM_IN:
        return (ss if ss is not None else mo.push_in_start_percent / 100.0,
                es if es is not None else mo.push_in_end_percent / 100.0)
    if motion == m.ZOOM_OUT:
        return (ss if ss is not None else mo.zoom_out_start_percent / 100.0,
                es if es is not None else mo.zoom_out_end_percent / 100.0)
    s0 = ss if ss is not None else 1.0
    return s0, (es if es is not None else (ss if ss is not None else 1.0))


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def _scale_rect(rect: m.Rect, scale: float, image) -> m.Rect:
    safe = max(1.0, scale)
    w = rect.width / safe
    h = rect.height / safe
    cx = rect.x + rect.width / 2.0
    cy = rect.y + rect.height / 2.0
    x = _clamp(cx - w / 2.0, 0, max(0, image.width - w))
    y = _clamp(cy - h / 2.0, 0, max(0, image.height - h))
    return m.Rect(x, y, w, h)


# ------------------------------------------------------------------ reveals / sounds
def _expand_reveals(clips, reveals, fps, options, issues):
    if not reveals:
        return clips
    result = []
    for clip in clips:
        plan = reveals.get(id(clip))
        if plan is None:
            result.append(clip)
            continue
        at_ms = plan.at_ms
        count = len(at_ms)
        layout = plan.layout
        if layout == m.REVEAL_GRID and count != 4:
            issues.append(_issue(
                m.WARNING, "REVEAL_GRID_NEEDS_FOUR",
                f'Shot "{clip.shot_id}": a grid reveal needs exactly 4 items; using a row instead.'))
            layout = m.REVEAL_ROW
        if plan.sources is not None and len(plan.sources) != count:
            issues.append(_issue(
                m.WARNING, "REVEAL_ASSETS_COUNT",
                f'Shot "{clip.shot_id}": {len(plan.sources)} images for {count} reveal cues; '
                "the reveal was ignored."))
            result.append(clip)
            continue
        sources = plan.sources
        steps = [(0, 1)]
        for k in range(2, count + 1):
            offset = round(at_ms[k - 1] * fps / 1000.0) - clip.start_frame
            last = steps[-1][0]
            if offset < last + MIN_REVEAL_STEP_FRAMES or offset > clip.duration_frames - MIN_REVEAL_STEP_FRAMES:
                issues.append(_issue(
                    m.WARNING, "REVEAL_STEP_SKIPPED",
                    f'Shot "{clip.shot_id}": item {k} appears too close to the previous item or to the '
                    "end of the shot; it shows together with the next item instead."))
                continue
            steps.append((offset, k))
        if steps[-1][1] != count:
            steps[-1] = (steps[-1][0], count)

        fade_frames = round(options.transitions.reveal_fade_seconds * fps)
        for j, (offset, visible) in enumerate(steps):
            end = steps[j + 1][0] if j + 1 < len(steps) else clip.duration_frames
            step = m.VideoClip(
                file_path=(reveal_paths.stack_path(sources, visible, layout) if sources is not None
                           else reveal_paths.step_path(clip.file_path, visible, count, layout)),
                scene_id=clip.scene_id,
                shot_id=clip.shot_id if j == 0 else f"{clip.shot_id}.{visible}",
                start_frame=clip.start_frame + offset,
                duration_frames=end - offset,
                motion=clip.motion, motion_source=clip.motion_source, image_type=clip.image_type,
                easing=clip.easing, start_viewport=clip.start_viewport, end_viewport=clip.end_viewport,
                motion_duration_frames=clip.motion_duration_frames, transition=clip.transition,
                reveal=m.RevealInfo(
                    layout=layout, count=count, visible=visible, base_shot_id=clip.shot_id,
                    source_path=clip.file_path if sources is None else None,
                    sources=[] if sources is None else list(sources)))
            if j > 0:
                previous_frames = steps[j][0] - steps[j - 1][0]
                frames = min(fade_frames, min(previous_frames, step.duration_frames) - 2)
                step.transition = m.TransitionIn("crossfade", frames) if frames >= 2 else None
            result.append(step)
    return result


def _build_sounds(clips, shots) -> list:
    by_shot: dict = {}
    for s in shots:
        if s.sfx and s.shot_id not in by_shot:
            by_shot[s.shot_id] = s.sfx
    sounds: list = []
    if not by_shot:
        return sounds
    for clip in clips:
        sid = clip.reveal.base_shot_id if clip.reveal is not None and clip.reveal.base_shot_id is not None \
            else clip.shot_id
        if sid is None or sid not in by_shot:
            continue
        names = by_shot[sid]
        index = min(clip.reveal.visible - 1, len(names) - 1) if clip.reveal is not None else 0
        sounds.append(m.SoundEffect(name=names[index], start_frame=clip.start_frame))
    return sounds


def _build_coverage(manifest, assets_by_id, clips, audio_frames, fps, missing_assets, issues):
    used = {c[1].id.lower() for c in clips}
    unused = [f"{a.id} ({a.file})" for a in manifest.assets or []
              if a.id is not None and a.id.lower() in assets_by_id and a.id.lower() not in used]
    for u in unused:
        issues.append(_issue(m.INFO, "MANIFEST_ASSET_UNUSED", f"Asset {u} is not referenced by any shot."))

    longest = 0
    run = 0
    previous = None
    changes = 0
    for clip, asset in clips:
        if asset.id == previous:
            run += clip.duration_frames
        else:
            if previous is not None:
                longest = max(longest, run)
                changes += 1
            run = clip.duration_frames
        previous = asset.id
    longest = max(longest, run)

    return ManifestCoverage(
        audio_frames / fps, len(clips), len(assets_by_id), len(used),
        0 if changes == 0 else audio_frames / fps / max(1, changes),
        longest / fps,
        0 if not clips else max(c[0].duration_frames for c in clips) / fps,
        missing_assets, unused)
