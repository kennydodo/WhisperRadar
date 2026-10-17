"""Models of ImgToVideo.Core (Models/*). Enums are the JSON strings the C# converters use."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ---- enums as strings (same values as the C# JSON converters) -----------------
STATIC, ZOOM_IN, ZOOM_OUT = "static", "zoom_in", "zoom_out"
PAN_LEFT, PAN_RIGHT, PAN_UP, PAN_DOWN, PAN_REVEAL = (
    "pan_left", "pan_right", "pan_up", "pan_down", "pan_reveal")
MOTIONS = (STATIC, ZOOM_IN, ZOOM_OUT, PAN_LEFT, PAN_RIGHT, PAN_UP, PAN_DOWN, PAN_REVEAL)

EXPLICIT_CODE, AUTO_SELECTED, OVERRIDE = "explicit_code", "auto_selected", "override"
MOTION_SOURCES = (EXPLICIT_CODE, AUTO_SELECTED, OVERRIDE)

LINEAR, EASE_IN, EASE_OUT, EASE_IN_OUT = "linear", "ease_in", "ease_out", "ease_in_out"
EASE_OUT_HOLD = "ease_out_hold"
EASINGS = (LINEAR, EASE_IN, EASE_OUT, EASE_IN_OUT, EASE_OUT_HOLD)
EASING_NAMES = {LINEAR: "Linear", EASE_IN: "Ease in", EASE_OUT: "Ease out",
                EASE_IN_OUT: "Ease in out", EASE_OUT_HOLD: "Ease out hold"}
# ease_out_hold: constant speed for this fraction of the shot (2-3 s on a
# 6-10 s pan), then quadratic deceleration to rest; velocity-continuous at
# the split so no kink shows. y = A*t for t<=s, else 1 - B*(1-t)^2 with
# A = 2/(1+s), B = 1/(1-s^2).
PAN_HOLD_FRACTION = 0.35

TRANSITION_KINDS = (
    "none", "crossfade", "fade_black", "fade_white", "wipe_left", "wipe_right", "wipe_up",
    "wipe_down", "slide_left", "slide_right", "slide_up", "slide_down", "dissolve",
    "circle_open", "circle_close", "smooth_left", "smooth_right")
TRANSITION_NAMES = {
    "crossfade": "Crossfade", "fade_black": "Dip to black", "fade_white": "Dip to white",
    "wipe_left": "Wipe left", "wipe_right": "Wipe right", "wipe_up": "Wipe up",
    "wipe_down": "Wipe down", "slide_left": "Slide left", "slide_right": "Slide right",
    "slide_up": "Slide up", "slide_down": "Slide down",
    "dissolve": "Dissolve", "circle_open": "Circle open", "circle_close": "Circle close",
    "smooth_left": "Smooth push left", "smooth_right": "Smooth push right"}
_XFADE = {
    "crossfade": "fade", "fade_black": "fadeblack", "fade_white": "fadewhite",
    "wipe_left": "wipeleft", "wipe_right": "wiperight", "wipe_up": "wipeup",
    "wipe_down": "wipedown", "slide_left": "slideleft", "slide_right": "slideright",
    "slide_up": "slideup", "slide_down": "slidedown",
    "dissolve": "dissolve", "circle_open": "circleopen", "circle_close": "circleclose",
    "smooth_left": "smoothleft", "smooth_right": "smoothright"}


def to_xfade_name(kind: str) -> str:
    return _XFADE.get(kind, "fade")


CENTERED, LATE = "centered", "late"
TRANSITION_ALIGNMENTS = (CENTERED, LATE)

IMAGE_TYPES = ("scene", "close_up", "infographic", "comparison", "process", "hybrid", "overview")
IMAGE_TYPE_CODES = (("SCN", "scene"), ("CU", "close_up"), ("INF", "infographic"),
                    ("CMP", "comparison"), ("PROC", "process"), ("HYB", "hybrid"),
                    ("OVR", "overview"))

MOTION_CODES = (("ST", STATIC), ("ZI", ZOOM_IN), ("ZO", ZOOM_OUT), ("PL", PAN_LEFT),
                ("PR", PAN_RIGHT), ("PU", PAN_UP), ("PD", PAN_DOWN), ("PV", PAN_REVEAL))


def motion_from_suffix(suffix: str) -> Optional[str]:
    s = suffix.upper()
    for code, motion in MOTION_CODES:
        if code == s:
            return motion
    return None


def image_type_from_code(code: str) -> Optional[str]:
    c = code.upper()
    for k, t in IMAGE_TYPE_CODES:
        if k == c:
            return t
    return None


def minimum_size(motion: str) -> tuple[int, int]:
    if motion in (PAN_LEFT, PAN_RIGHT):
        return (2880, 1296)
    if motion in (PAN_UP, PAN_DOWN):
        return (2304, 2160)
    if motion == PAN_REVEAL:
        return (3840, 1296)
    return (2304, 1296)


# ---- pacing constants ---------------------------------------------------------
MIN_IMAGE_SECONDS = 2.5
PREFERRED_IMAGE_SECONDS = 5.0
MAX_IMAGE_SECONDS = 8.0
FLOOR_IMAGE_SECONDS = 2.0
CROSSFADE_SECONDS = 0.5

# ---- validation issues ---------------------------------------------------------
INFO, WARNING, ERROR = "Info", "Warning", "Error"


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    message: str


def has_errors(issues) -> bool:
    return any(i.severity == ERROR for i in issues)


# ---- geometry -------------------------------------------------------------------
@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    def is_inside(self, bounds: "Rect") -> bool:
        return (self.x >= bounds.x and self.y >= bounds.y
                and self.right <= bounds.right and self.bottom <= bounds.bottom)

    def to_dict(self) -> dict:
        # C# serializes the computed Right/Bottom too
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height,
                "right": self.right, "bottom": self.bottom}

    @staticmethod
    def from_dict(d: dict) -> "Rect":
        return Rect(float(d["x"]), float(d["y"]), float(d["width"]), float(d["height"]))


@dataclass(frozen=True)
class Resolution:
    width: int
    height: int


# ---- simple inputs ----------------------------------------------------------------
@dataclass(frozen=True)
class ParsedImageName:
    stem: str
    scene_number: int
    image_number: int
    code: Optional[str] = None            # motion
    has_unknown_suffix: bool = False
    type: Optional[str] = None            # image type


@dataclass(frozen=True)
class ImageInfo:
    file_path: str
    name: ParsedImageName
    width: int
    height: int


@dataclass(frozen=True)
class SubtitleBlock:
    index: int
    start_seconds: float
    end_seconds: float
    text: str

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


@dataclass(frozen=True)
class SceneWindow:
    scene_id: str
    start_seconds: float
    end_seconds: float


@dataclass(frozen=True)
class SceneImageGroup:
    scene_number: int
    scene_id: str
    images: tuple


@dataclass(frozen=True)
class ScenePlanInput:
    scene_id: str
    start_seconds: float
    end_seconds: float
    images: tuple


# ---- reveal -------------------------------------------------------------------------
REVEAL_ROW, REVEAL_GRID = "row", "grid"
REVEAL_MIN_ITEMS, REVEAL_MAX_ITEMS = 2, 4


def reveal_layout_valid(layout) -> bool:
    return isinstance(layout, str) and layout.lower() in (REVEAL_ROW, REVEAL_GRID)


def reveal_layout_normalize(layout) -> str:
    return REVEAL_GRID if isinstance(layout, str) and layout.lower() == REVEAL_GRID else REVEAL_ROW


@dataclass
class RevealInfo:
    layout: str = REVEAL_ROW
    count: int = 0
    visible: int = 0
    base_shot_id: Optional[str] = None
    source_path: Optional[str] = None
    sources: list = field(default_factory=list)

    @property
    def is_stack(self) -> bool:
        return len(self.sources) > 0

    def to_dict(self) -> dict:
        d = {"layout": self.layout, "count": self.count, "visible": self.visible}
        if self.base_shot_id is not None:
            d["base_shot_id"] = self.base_shot_id
        if self.source_path is not None:
            d["source_path"] = self.source_path
        d["sources"] = list(self.sources)
        d["is_stack"] = self.is_stack  # C# serializes the computed property too
        return d

    @staticmethod
    def from_dict(d: dict) -> "RevealInfo":
        return RevealInfo(d.get("layout", REVEAL_ROW), int(d.get("count", 0)),
                          int(d.get("visible", 0)), d.get("base_shot_id"),
                          d.get("source_path"), list(d.get("sources") or []))


# ---- timeline -------------------------------------------------------------------------
@dataclass
class TransitionIn:
    kind: str = "none"
    duration_frames: int = 0

    def to_dict(self) -> dict:
        return {"kind": self.kind, "duration_frames": self.duration_frames}


@dataclass
class VideoClip:
    file_path: str = ""
    scene_id: str = ""
    shot_id: Optional[str] = None
    start_frame: int = 0
    motion_duration_frames: Optional[int] = None
    duration_frames: int = 0
    motion: str = STATIC
    motion_source: str = AUTO_SELECTED
    image_type: Optional[str] = None
    easing: str = LINEAR
    start_viewport: Rect = Rect(0, 0, 0, 0)
    end_viewport: Rect = Rect(0, 0, 0, 0)
    transition: Optional[TransitionIn] = None
    reveal: Optional[RevealInfo] = None
    # AI image-to-video clip that replaces the still's pan/zoom (animated opening)
    video_path: Optional[str] = None

    def to_dict(self) -> dict:
        d: dict = {"file_path": self.file_path, "scene_id": self.scene_id}
        if self.shot_id is not None:
            d["shot_id"] = self.shot_id
        d["start_frame"] = self.start_frame
        if self.motion_duration_frames is not None:
            d["motion_duration_frames"] = self.motion_duration_frames
        d["duration_frames"] = self.duration_frames
        d["motion"] = self.motion
        d["motion_source"] = self.motion_source
        if self.image_type is not None:
            d["image_type"] = self.image_type
        d["easing"] = self.easing
        d["start_viewport"] = self.start_viewport.to_dict()
        d["end_viewport"] = self.end_viewport.to_dict()
        if self.transition is not None:
            d["transition"] = self.transition.to_dict()
        if self.reveal is not None:
            d["reveal"] = self.reveal.to_dict()
        if self.video_path:
            d["video_path"] = self.video_path
        return d

    @staticmethod
    def from_dict(d: dict) -> "VideoClip":
        tr = d.get("transition")
        rv = d.get("reveal")
        mdf = d.get("motion_duration_frames")
        return VideoClip(
            file_path=d.get("file_path", ""), scene_id=d.get("scene_id", ""),
            shot_id=d.get("shot_id"), start_frame=int(d.get("start_frame", 0)),
            motion_duration_frames=None if mdf is None else int(mdf),
            duration_frames=int(d.get("duration_frames", 0)),
            motion=d.get("motion", STATIC), motion_source=d.get("motion_source", AUTO_SELECTED),
            image_type=d.get("image_type"), easing=d.get("easing", LINEAR),
            start_viewport=Rect.from_dict(d["start_viewport"]),
            end_viewport=Rect.from_dict(d["end_viewport"]),
            transition=None if tr is None else TransitionIn(
                tr.get("kind", "none"), int(tr.get("duration_frames", 0))),
            reveal=None if rv is None else RevealInfo.from_dict(rv),
            video_path=d.get("video_path") or None)


@dataclass
class Scene:
    id: str = ""
    start_frame: int = 0
    end_frame: int = 0
    clips: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "start_frame": self.start_frame, "end_frame": self.end_frame,
                "clips": [c.to_dict() for c in self.clips]}


@dataclass
class AudioTrack:
    file_path: str = ""
    duration_frames: int = 0


@dataclass
class SoundEffect:
    name: str = ""
    file_path: str = ""
    start_frame: int = 0
    duration_frames: int = 0

    def to_dict(self) -> dict:
        return {"name": self.name, "file_path": self.file_path,
                "start_frame": self.start_frame, "duration_frames": self.duration_frames}


@dataclass
class TextOverlay:
    """Text the renderer draws over the picture (not baked into the image).

    `png_path` is a full-frame transparent PNG made by text_overlays.resolve;
    the frames are on the final timeline (the shot's start plus its offsets)."""
    text: str = ""
    style: str = "caption"
    position: str = "bottom-center"
    start_frame: int = 0
    end_frame: int = 0
    anim: str = "fade"
    png_path: str = ""

    def to_dict(self) -> dict:
        return {"text": self.text, "style": self.style, "position": self.position,
                "start_frame": self.start_frame, "end_frame": self.end_frame,
                "anim": self.anim, "png_path": self.png_path}


TIMELINE_SCHEMA_VERSION = 3


@dataclass
class Timeline:
    schema_version: int = TIMELINE_SCHEMA_VERSION
    project_name: str = ""
    resolution: Resolution = Resolution(1920, 1080)
    fps: float = 30.0
    audio: AudioTrack = field(default_factory=AudioTrack)
    scenes: list = field(default_factory=list)
    sounds: list = field(default_factory=list)
    overlays: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "schema_version": self.schema_version, "project_name": self.project_name,
            "resolution": {"width": self.resolution.width, "height": self.resolution.height},
            "fps": self.fps,
            "audio": {"file_path": self.audio.file_path,
                      "duration_frames": self.audio.duration_frames},
            "scenes": [s.to_dict() for s in self.scenes],
            "sounds": [s.to_dict() for s in self.sounds]}
        if self.overlays:                     # absent when empty: ImgToVideo timeline parity
            d["overlays"] = [o.to_dict() for o in self.overlays]
        return d

    @staticmethod
    def from_dict(d: dict) -> "Timeline":
        if d.get("schema_version") != TIMELINE_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported timeline schemaVersion {d.get('schema_version')} "
                f"(supported: {TIMELINE_SCHEMA_VERSION}).")
        res = d.get("resolution") or {}
        au = d.get("audio") or {}
        scenes = []
        for s in d.get("scenes", []):
            scenes.append(Scene(s.get("id", ""), int(s.get("start_frame", 0)),
                                int(s.get("end_frame", 0)),
                                [VideoClip.from_dict(c) for c in s.get("clips", [])]))
        sounds = [SoundEffect(x.get("name", ""), x.get("file_path", ""),
                              int(x.get("start_frame", 0)), int(x.get("duration_frames", 0)))
                  for x in d.get("sounds", [])]
        return Timeline(d["schema_version"], d.get("project_name", ""),
                        Resolution(int(res.get("width", 1920)), int(res.get("height", 1080))),
                        float(d.get("fps", 30.0)),
                        AudioTrack(au.get("file_path", ""), int(au.get("duration_frames", 0))),
                        scenes, sounds,
                        [TextOverlay(x.get("text", ""), x.get("style", "caption"),
                                     x.get("position", "bottom-center"),
                                     int(x.get("start_frame", 0)), int(x.get("end_frame", 0)),
                                     x.get("anim", "fade"), x.get("png_path", ""))
                         for x in d.get("overlays", [])])
