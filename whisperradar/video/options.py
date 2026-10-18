"""ProjectOptions (imgtovideo.json): port of ImgToVideo.Core.Options. snake_case JSON."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

from . import models as m

OPTIONS_SCHEMA_VERSION = 1
# visual modes (doc 16.1): stable stored enum values
VM_IMAGES = "images"
VM_MOTION = "motion_graphics"
VM_WHITEBOARD = "whiteboard"
VISUAL_MODES = (VM_IMAGES, VM_MOTION, VM_WHITEBOARD)
POP_VARIANTS = (
    ("pop_bubble", "Bubble - short rising pop"), ("pop_boop", "Boop - soft, rounded"),
    ("pop_drop", "Drop - falling water drop"), ("pop_blip", "Blip - tiny, clean"),
    ("pop_tap", "Tap - dry wood tap"), ("pop_snap", "Snap - finger snap"),
    ("pop_cork", "Cork - bottle-cork pop"), ("pop_thump", "Thump - low, soft bass"),
    ("pop_pluck", "Pluck - plucked string"), ("pop_marimba", "Marimba - mallet note"),
    ("pop_ping", "Ping - glass ping"), ("pop_sparkle", "Sparkle - two quick rising notes"),
    ("pop_classic", "Classic - the original pop"))
DEFAULT_POP_ID = "pop_bubble"


def is_pop_variant(pop_id) -> bool:
    return isinstance(pop_id, str) and any(v[0].lower() == pop_id.lower() for v in POP_VARIANTS)


@dataclass
class OutputOptions:
    width: int = 2560
    height: int = 1440
    fps: float = 30.0


@dataclass
class TimingOptions:
    min_image_seconds: float = 2.5
    preferred_image_seconds: float = 5.0
    max_image_seconds: float = 8.0
    floor_image_seconds: float = 2.0
    warn_hold_seconds: float = 30.0


@dataclass
class MotionOptions:
    auto_motion_enabled: bool = True
    easing: str = m.EASE_IN_OUT
    push_in_start_percent: float = 100.0
    push_in_end_percent: float = 106.0
    zoom_out_start_percent: float = 107.0
    zoom_out_end_percent: float = 100.0
    pan_max_travel_percent: float = 4.0
    # AUTO-selected pans (no filename code) are accents: travel grows with
    # shot length up to pan_max_travel_percent of the output dimension.
    # Filename-coded pans (the images composed for a pan) sweep the whole
    # overscan band edge-to-edge - the composition travels out of frame - and
    # are shaped by EASING instead of a distance cap: shots under the
    # ease-out threshold start fast and decelerate to rest (ease_out), so the
    # pan "speeds in a bit fast and slows down till the frame is out of
    # sight" rather than surging mid-shot (ease_in_out) or crawling.
    pan_travel_seconds_floor: float = 3.0
    pan_target_speed_px_per_second: float = 20.0
    pan_ease_out_below_seconds: float = 10.0
    static_every_min_shots: int = 4
    static_every_max_shots: int = 6
    motion_duration_ms: int = 0
    long_pan_linear_threshold_seconds: float = 10.0


@dataclass
class TransitionOptions:
    enabled: bool = True
    kind: str = "crossfade"
    scene_boundary_kind: str = "fade_black"
    alignment: str = m.LATE
    duration_seconds: float = 0.5
    reveal_fade_seconds: float = 0.25


@dataclass
class NamingOptions:
    scene_prefix: str = "S"
    number_padding: int = 2
    separator: str = "_"
    motion_codes_enabled: bool = True
    type_codes_enabled: bool = True
    image_extensions: list = field(default_factory=lambda: [".png"])

    def scene_id(self, scene_number: int) -> str:
        padding = min(max(self.number_padding, 1), 4)
        return f"{self.scene_prefix}{scene_number:0{padding}d}"


@dataclass
class RenderOptions:
    preview_width: int = 960
    preview_height: int = 540
    preview_preset: str = "veryfast"
    preview_crf: int = 28
    preview_bframes: int = 0
    final_preset: str = "medium"
    final_crf: int = 18
    final_bframes: int = 3
    supersample_target_width: int = 12288
    encoder: str = "auto"
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"


@dataclass
class SceneInferenceOptions:
    sentence_gap_seconds: float = 0.8
    terminal_punctuation_gap_seconds: float = 0.4


@dataclass
class ShotlistOptions:
    master_prompt_max_chars: int = 1500
    prompt_max_chars: int = 2400


@dataclass
class SoundOptions:
    default_pop: str = DEFAULT_POP_ID


@dataclass
class ProjectOptions:
    schema_version: int = OPTIONS_SCHEMA_VERSION
    output: OutputOptions = field(default_factory=OutputOptions)
    timing: TimingOptions = field(default_factory=TimingOptions)
    motion: MotionOptions = field(default_factory=MotionOptions)
    transitions: TransitionOptions = field(default_factory=TransitionOptions)
    naming: NamingOptions = field(default_factory=NamingOptions)
    render: RenderOptions = field(default_factory=RenderOptions)
    scene_inference: SceneInferenceOptions = field(default_factory=SceneInferenceOptions)
    shotlist: ShotlistOptions = field(default_factory=ShotlistOptions)
    sound: SoundOptions = field(default_factory=SoundOptions)
    # visual mode (doc 16.1): images (default, unchanged behavior),
    # motion_graphics / whiteboard (explainer.json scenes). An explicit mode
    # is never silently overridden by a sidecar's presence.
    visual_mode: str = "images"
    planner: str = "v2"

    def to_dict(self) -> dict:
        return _to_dict(self)

    def validate(self) -> list:
        errors: list = []
        o = self.output
        if o.width <= 0 or o.height <= 0 or o.width % 2 != 0 or o.height % 2 != 0:
            errors.append("Output width and height must be positive even numbers (H.264 requirement).")
        if o.fps <= 0:
            errors.append("Output fps must be positive.")
        t = self.timing
        if (t.floor_image_seconds <= 0 or t.min_image_seconds <= 0
                or t.preferred_image_seconds <= 0 or t.max_image_seconds <= 0):
            errors.append("Timing durations must be positive.")
        elif not (t.floor_image_seconds <= t.min_image_seconds
                  <= t.preferred_image_seconds <= t.max_image_seconds):
            errors.append("Timing must satisfy floor <= min <= preferred <= max.")
        mo = self.motion
        if mo.push_in_start_percent >= mo.push_in_end_percent:
            errors.append("Push-in start percent must be below push-in end percent.")
        if mo.zoom_out_start_percent <= mo.zoom_out_end_percent:
            errors.append("Zoom-out start percent must be above zoom-out end percent.")
        if mo.pan_max_travel_percent < 0 or mo.pan_max_travel_percent > 25:
            errors.append("Pan max travel percent must be between 0 and 25.")
        if mo.static_every_min_shots < 1 or mo.static_every_min_shots > mo.static_every_max_shots:
            errors.append("Static cadence must satisfy 1 <= min shots <= max shots.")
        tr = self.transitions
        if tr.enabled and tr.duration_seconds <= 0:
            errors.append("Transition duration must be positive when transitions are enabled.")
        n = self.naming
        if not n.scene_prefix.strip():
            errors.append("Scene prefix must not be empty.")
        if not n.separator:
            errors.append("Separator must not be empty.")
        if n.number_padding < 1 or n.number_padding > 4:
            errors.append("Number padding must be between 1 and 4.")
        if len(n.image_extensions) == 0:
            errors.append("At least one image extension must be listed.")
        si = self.scene_inference
        if si.sentence_gap_seconds <= 0 or si.terminal_punctuation_gap_seconds <= 0:
            errors.append("Scene inference gaps must be positive.")
        elif si.terminal_punctuation_gap_seconds > si.sentence_gap_seconds:
            errors.append("Terminal punctuation gap must not exceed the sentence gap.")
        sl = self.shotlist
        if sl.master_prompt_max_chars < 0 or sl.prompt_max_chars < 0:
            errors.append("Shotlist prompt limits must not be negative (0 disables a limit).")
        if not is_pop_variant(self.sound.default_pop):
            errors.append(f'Unknown default pop sound "{self.sound.default_pop}".')
        r = self.render
        if r.preview_width <= 0 or r.preview_height <= 0:
            errors.append("Preview width and height must be positive.")
        if r.preview_crf < 0 or r.preview_crf > 51:
            errors.append("Preview CRF must be between 0 and 51.")
        if r.final_crf < 0 or r.final_crf > 51:
            errors.append("Final CRF must be between 0 and 51.")
        if r.preview_bframes < 0 or r.preview_bframes > 16:
            errors.append("Preview B-frames must be between 0 and 16.")
        if r.final_bframes < 0 or r.final_bframes > 16:
            errors.append("Final B-frames must be between 0 and 16.")
        if r.supersample_target_width < 0 or r.supersample_target_width > 16384:
            errors.append("Supersample target width must be between 0 and 16384.")
        if self.planner not in ("v1", "v2"):
            errors.append('Planner must be "v1" or "v2".')
        if self.visual_mode not in VISUAL_MODES:
            errors.append("Visual mode must be one of "
                          f"{', '.join(VISUAL_MODES)}.")
        if r.encoder not in ("auto", "libx264", "cpu", "h264_nvenc", "nvenc",
                             "h264_amf", "amf", "h264_qsv", "qsv"):
            errors.append("Encoder must be auto, libx264, h264_nvenc, h264_amf or h264_qsv.")
        return errors


def _to_dict(obj):
    if is_dataclass(obj):
        return {f.name: _to_dict(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, list):
        return [_to_dict(x) for x in obj]
    return obj


def _apply(obj, data: dict) -> None:
    """Overlay JSON values on a defaults dataclass (unknown keys ignored, like System.Text.Json)."""
    for f in fields(obj):
        if f.name not in data:
            continue
        cur = getattr(obj, f.name)
        val = data[f.name]
        if is_dataclass(cur) and isinstance(val, dict):
            _apply(cur, val)
        elif isinstance(cur, bool):
            setattr(obj, f.name, bool(val))
        elif isinstance(cur, int):
            setattr(obj, f.name, int(val))
        elif isinstance(cur, float):
            setattr(obj, f.name, float(val))
        elif isinstance(cur, list):
            setattr(obj, f.name, list(val))
        else:
            setattr(obj, f.name, val)


def load_from_json(text: str) -> ProjectOptions:
    if not text or not text.strip():
        return ProjectOptions()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("imgtovideo.json must contain a JSON object.")
    if "schema_version" in data:
        v = data["schema_version"]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("imgtovideo.json has a non-numeric schema_version.")
        if int(v) != OPTIONS_SCHEMA_VERSION:
            raise ValueError(f"Unsupported options schema_version {int(v)} "
                             f"(supported: {OPTIONS_SCHEMA_VERSION}).")
    opts = ProjectOptions()
    _apply(opts, data)
    return opts


def load(path) -> ProjectOptions:
    return load_from_json(Path(path).read_text(encoding="utf-8-sig"))


def load_or_default(path) -> ProjectOptions:
    p = Path(path)
    return load(p) if p.exists() else ProjectOptions()


def save(options: ProjectOptions, path) -> None:
    Path(path).write_text(json.dumps(options.to_dict(), indent=2), encoding="utf-8")
