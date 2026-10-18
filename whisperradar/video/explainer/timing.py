"""Timing compiler: SRT cue anchors -> integer frames.

Repo conventions (timing_engine.py / models.py):
- frames = round(seconds * fps) (round-half-away from zero);
- end frames are exclusive;
- narration/audio is the master clock.

Compile order (matters): scene frame ranges are computed from the SRT first,
then gap fill / scene-0 pinning are applied to the RANGES, and only then are
events compiled against the final ranges - so event windows always line up
with the frames the scene actually occupies. Events that fall outside their
scene's final range are errors, not silent clamps.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import schema as sc
from .validate import ExplainerError

MS_PER_SECOND = 1000.0


@dataclass
class CompiledEvent:
    event: sc.EventSpec
    start_frame: int
    end_frame: int                      # exclusive; == start_frame for instant
    duration_frames: int

    @property
    def is_instant(self) -> bool:
        return self.duration_frames <= 0


@dataclass
class CompiledScene:
    spec: sc.SceneSpec
    start_frame: int
    end_frame: int                      # exclusive
    fps: float
    events: list = field(default_factory=list)   # CompiledEvent, sorted by start

    @property
    def duration_frames(self) -> int:
        return self.end_frame - self.start_frame


def _round_away(x: float) -> int:
    """Math.Round away-from-zero, matching the engine's frame math on .5."""
    import math
    return int(math.copysign(math.floor(abs(x) + 0.5), x))


def frames(seconds: float, fps: float) -> int:
    return _round_away(seconds * fps)


def ms_frames(ms: int, fps: float) -> int:
    return frames(ms / MS_PER_SECOND, fps)


class CueClock:
    """Cue id -> (start_seconds, end_seconds) from the production SRT."""

    def __init__(self, cues):
        # cues: list of models.SubtitleBlock (or anything with index/start/end)
        self._by_id = {}
        for pos, c in enumerate(cues):
            idx = c.index if getattr(c, "index", 0) > 0 else pos + 1
            self._by_id[idx] = (float(c.start_seconds), float(c.end_seconds))

    def __len__(self):
        return len(self._by_id)

    def get(self, cue_id: int):
        return self._by_id.get(int(cue_id))

    def require(self, cue_id: int, where: str):
        found = self._by_id.get(int(cue_id))
        if found is None:
            raise ExplainerError([
                f"{where}: cue {cue_id} does not exist in subtitles.srt "
                f"(the SRT has {len(self._by_id)} cues)."])
        return found


def compile_scene(scene: sc.SceneSpec, clock: CueClock, fps: float,
                  start_frame: int = None, end_frame: int = None) -> CompiledScene:
    """Compile events against explicit scene bounds when given (the gap-filled
    ranges), else the scene's own cue range."""
    if start_frame is None:
        first, last = scene.cue_range
        start_s = clock.require(first, f"Scene {scene.id} cue_range")[0]
        start_frame = frames(start_s, fps)
    if end_frame is None:
        first, last = scene.cue_range
        end_s = clock.require(last, f"Scene {scene.id} cue_range")[1]
        end_frame = frames(end_s, fps)
    if end_frame <= start_frame:
        raise ExplainerError([
            f"Scene {scene.id}: cue range rounds to a non-positive frame "
            f"count at {fps:g} fps."])

    errors = []
    compiled = []
    for ev in scene.events:
        where = (f"Scene {scene.id}, event({ev.action} -> {ev.target}, "
                 f"cue {ev.at.cue} {ev.at.edge})")
        cue_start, cue_end = clock.require(ev.at.cue, where)
        t = cue_start if ev.at.edge == "start" else cue_end
        t += ev.offset_ms / 1000.0
        local = frames(t, fps) - start_frame
        dur_f = ms_frames(ev.duration_ms, fps)
        end = local + dur_f
        if local < 0:
            errors.append(f"{where}: starts {start_frame - frames(t, fps)} "
                          "frame(s) before the scene begins (anchor to a cue "
                          "inside the scene's cue_range, or reduce the "
                          "negative offset).")
            continue
        if end > end_frame - start_frame:
            over = end - (end_frame - start_frame)
            errors.append(f"{where}: runs {over} frame(s) past the scene end "
                          "(shorten duration_ms, or extend the scene's "
                          "cue_range).")
            continue
        compiled.append(CompiledEvent(ev, local, end, max(0, end - local)))
    if errors:
        raise ExplainerError(errors)
    compiled.sort(key=lambda c: (c.start_frame, c.event.action))
    return CompiledScene(scene, start_frame, end_frame, fps, compiled)


def compile_doc(doc, cues, fps: float, total_seconds: float = None,
                fill_gaps: bool = True):
    """ExplainerDoc + cues -> list[CompiledScene]. Raises ExplainerError.

    Order: (1) compute each scene's cue range in frames; (2) reject overlaps;
    (3) fill gaps (each scene holds until the next scene's first cue, the
    first scene is pinned to frame 0, the last runs to the audio duration);
    (4) compile events against the FINAL ranges so event windows match the
    frames the scene actually occupies.
    """
    clock = CueClock(cues)
    if len(clock) == 0:
        raise ExplainerError(["subtitles.srt has no cues; explainer scenes "
                              "cannot be timed."])
    errors = []
    ranges = []
    for scene in doc.scenes:
        first, last = scene.cue_range
        if first > len(clock) or last > len(clock):
            errors.append(f"Scene {scene.id}: cue_range [{first}, {last}] exceeds "
                          f"the {len(clock)} cues in subtitles.srt.")
            continue
        start_s = clock.require(first, f"Scene {scene.id} cue_range")[0]
        end_s = clock.require(last, f"Scene {scene.id} cue_range")[1]
        if end_s <= start_s:
            errors.append(f"Scene {scene.id}: cue range [{first}, {last}] has "
                          "zero or negative duration in the SRT.")
            continue
        ranges.append((scene, frames(start_s, fps), frames(end_s, fps)))
    if errors:
        raise ExplainerError(errors)
    if not ranges:
        raise ExplainerError(["explainer.json contains no valid scenes."])

    ranges.sort(key=lambda r: r[1])
    for i in range(1, len(ranges)):
        if ranges[i][1] < ranges[i - 1][2]:
            errors.append(
                f"Scene {ranges[i][0].id}: its cue range starts at frame "
                f"{ranges[i][1]} before scene {ranges[i - 1][0].id} ends "
                f"({ranges[i - 1][2]}) - cue ranges must not overlap.")
    if errors:
        raise ExplainerError(errors)

    if fill_gaps:
        fixed = []
        for i, (scene, start, end) in enumerate(ranges):
            if i > 0 and start < ranges[i - 1][2]:
                start = ranges[i - 1][2]   # overlaps were rejected above; keep
            fixed.append([scene, start, end])
        if fixed[0][1] > 0:
            fixed[0][1] = 0   # the timeline starts at frame 0
        for i in range(len(fixed) - 1):
            nxt = fixed[i + 1]
            if nxt[1] > fixed[i][2]:
                fixed[i][2] = nxt[1]
        if total_seconds is not None:
            total_f = frames(total_seconds, fps)
            if fixed[-1][2] < total_f:
                fixed[-1][2] = total_f
        ranges = [(s, a, b) for s, a, b in fixed]

    errors = []
    out = []
    for scene, start, end in ranges:
        try:
            out.append(compile_scene(scene, clock, fps, start, end))
        except ExplainerError as e:
            errors.extend(e.errors)
    if errors:
        raise ExplainerError(errors)
    return out


def scene_local_to_global(scene: CompiledScene, local_frame: int) -> int:
    return scene.start_frame + local_frame
