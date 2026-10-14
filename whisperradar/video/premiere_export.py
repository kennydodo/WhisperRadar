"""Port of ImgToVideo.Premiere.Fcp7XmlExporter: a timeline as FCP7 XML for Premiere Pro.

V1 carries every clip, V2 every fade window, laid out exactly like the ffmpeg render plan
(see transition_cuts / render_plan). Motion is sampled into Basic Motion keyframes, opacity
ramps into their own filter, sound effects into extra audio tracks above the narration.
The output matches the C# exporter's XDocument formatting (two-space indent, CRLF).
"""
from __future__ import annotations

import math
import ntpath
import os
import re
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

from . import models as m
from . import transition_cuts
from .options import ProjectOptions

NEWLINE = "\r\n"
_DIPS = ("fade_black", "fade_white")


# ---- a tiny XElement ------------------------------------------------------------
class El:
    __slots__ = ("tag", "attrs", "kids")

    def __init__(self, tag: str, *kids):
        self.tag = tag
        self.attrs: List[Tuple[str, str]] = []
        self.kids: list = []
        for k in kids:
            self.add(k)

    def attr(self, name: str, value) -> "El":
        self.attrs.append((name, str(value)))
        return self

    def add(self, kid) -> "El":
        if kid is None:
            return self
        if isinstance(kid, (list, tuple)):
            for k in kid:
                self.add(k)
        else:
            self.kids.append(kid if isinstance(kid, El) else str(kid))
        return self


def _esc_text(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\r", "&#xD;"))


def _esc_attr(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("\r", "&#xD;").replace("\n", "&#xA;")
            .replace("\t", "&#x9;"))


def serialize(el: El, depth: int = 0) -> str:
    """XElement.ToString(): two-space indent, `<x />` for empty, text-only inline."""
    pad = "  " * depth
    attrs = "".join(f' {n}="{_esc_attr(v)}"' for n, v in el.attrs)
    if not el.kids:
        return f"{pad}<{el.tag}{attrs} />"
    if all(isinstance(k, str) for k in el.kids):
        return f"{pad}<{el.tag}{attrs}>{_esc_text(''.join(el.kids))}</{el.tag}>"
    inner = NEWLINE.join(serialize(k, depth + 1) if isinstance(k, El)
                         else "  " * (depth + 1) + _esc_text(k) for k in el.kids)
    return f"{pad}<{el.tag}{attrs}>{NEWLINE}{inner}{NEWLINE}{pad}</{el.tag}>"


def _t(tag: str, text) -> El:
    return El(tag, str(text))


def _f(value: float) -> str:
    """double.ToString("0.######", InvariantCulture)."""
    s = f"{value:.6f}".rstrip("0").rstrip(".")
    return s or "0"


def _rate(timebase: int) -> El:
    return El("rate", _t("timebase", timebase), _t("ntsc", "FALSE"))


def _center(horiz: float, vert: float) -> El:
    return El("value", _t("horiz", _f(horiz)), _t("vert", _f(vert)))


_WIN_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def to_path_url(file_path: str) -> str:
    """file://localhost/<escaped full path> (Path.GetFullPath + Uri.EscapeDataString per segment)."""
    if _WIN_DRIVE.match(file_path) or file_path.startswith("\\\\"):
        full = ntpath.normpath(file_path)
    else:
        full = os.path.abspath(file_path)
    segments = full.replace("\\", "/").split("/")
    escaped = "/".join(quote(s, safe="-_.~").replace("%3A", ":") for s in segments)
    return "file://localhost/" + escaped


def _basename(path: str) -> str:
    return re.split(r"[\\/]", path)[-1]


def _eased(easing: str, progress: float) -> float:
    p = min(1.0, max(0.0, progress))
    if easing == m.EASE_IN:
        return p * p
    if easing == m.EASE_OUT:
        return 1 - (1 - p) * (1 - p)
    if easing == m.EASE_IN_OUT:
        return p * p * p * (p * (p * 6 - 15) + 10)
    return p


def _round(x: float) -> int:
    return int(round(x))        # banker's rounding, like Math.Round


# ---- the exporter -----------------------------------------------------------------
def export(timeline: m.Timeline, images: Sequence[m.ImageInfo],
           include_motion_keyframes: bool = True,
           options: Optional[ProjectOptions] = None) -> str:
    project = options or ProjectOptions()
    dims: Dict[str, Tuple[int, int]] = {i.file_path: (i.width, i.height) for i in images}
    clips = [c for s in timeline.scenes for c in s.clips]
    if not clips:
        raise ValueError("Timeline has no clips to export.")

    timebase = _round(timeline.fps)
    total_frames = sum(c.duration_frames for c in clips)
    audio_frames = (timeline.audio.duration_frames if timeline.audio.duration_frames > 0
                    else total_frames)
    res = timeline.resolution

    base_items: List[El] = []
    overlay_items: List[El] = []
    cuts = transition_cuts.compute(clips, project)

    for index, clip in enumerate(clips):
        head_in = (transition_cuts.head_trim(cuts[index - 1][0], project)
                   if index > 0 and cuts[index - 1][0] > 0 else 0)
        has_next = index < len(cuts) and cuts[index][0] > 0
        hold_out = transition_cuts.head_trim(cuts[index][0], project) if has_next else 0

        opacity: List[Tuple[int, float]] = []
        if has_next and cuts[index][1] in _DIPS:
            cue = clip.duration_frames - head_in
            opacity.append((cue, 100.0))
            opacity.append((cue + max(1, cuts[index][0] // 2), 0.0))

        base_items.append(_video_clip_item(
            clip, dims, index, timebase, res, include_motion_keyframes,
            start_offset=head_in, duration=clip.duration_frames - head_in + hold_out,
            frozen_motion=False, opacity_ramp=opacity or None, motion_head_frames=head_in))

        if index > 0 and cuts[index - 1][0] > 0:
            cut_frames = cuts[index - 1][0]
            half = max(1, cut_frames // 2)
            dip = cuts[index - 1][1] in _DIPS
            window = cut_frames - (half if dip else 0)
            overlay_items.append(_video_clip_item(
                clip, dims, index, timebase, res, include_motion_keyframes,
                start_offset=-transition_cuts.tail_trim(cut_frames, project) + (half if dip else 0),
                duration=window, frozen_motion=True,
                opacity_ramp=[(0, 0.0), (window, 100.0)]))

    video_media = El(
        "video",
        El("format", El("samplecharacteristics", _rate(timebase), _t("width", res.width),
                        _t("height", res.height), _t("pixelaspectratio", "square"))),
        El("track", base_items, _t("enabled", "TRUE")))
    if overlay_items:
        video_media.add(El("track", overlay_items, _t("enabled", "TRUE")))

    audio_track = El("track", _audio_clip_item(timeline, timebase, audio_frames),
                     _t("enabled", "TRUE"))

    sequence = El("sequence")
    sequence.attr("id", "sequence-1").attr("TL.SQAudioVisibleBase", "0") \
        .attr("TL.SQVideoVisibleBase", "0") \
        .attr("MZ.Sequence.PreviewFrameSizeHeight", res.height) \
        .attr("MZ.Sequence.PreviewFrameSizeWidth", res.width) \
        .attr("explodedTracks", "true")
    sequence.add([
        _t("name", timeline.project_name),
        _t("duration", total_frames),
        _rate(timebase),
        El("timecode", _rate(timebase), _t("string", "00:00:00:00"), _t("frame", "0"),
           _t("displayformat", "NDF")),
        El("media", video_media,
           El("audio",
              El("samplecharacteristics", _t("depth", "16"), _t("samplerate", "48000")),
              audio_track, _sound_tracks(timeline, timebase, total_frames)))])

    root = El("xmeml", sequence).attr("version", "5")
    return '<?xml version="1.0" encoding="UTF-8"?>' + NEWLINE + serialize(root)


def _video_clip_item(clip, dims, index, timebase, res, include_motion, start_offset, duration,
                     frozen_motion, opacity_ramp, motion_head_frames=0) -> El:
    # The animated opening: a clip backed by its AI video plays the mp4 itself
    # (with its own in-file motion), like the ffmpeg render plan - the still's
    # pan/zoom keyframes must NOT be applied to it, and the media must point at
    # the mp4 or Premiere shows the still instead of the clip.
    video_path = getattr(clip, "video_path", None)
    source = video_path if video_path else clip.file_path
    # mp4 dimensions are unknown to the images inventory; the render pipeline
    # scales/crops to the project resolution anyway, so pass the project res.
    source_w, source_h = dims.get(source) or (res.width, res.height)
    file_name = _basename(source)
    id_suffix = str(index + 1) + ("-x" if frozen_motion else "")
    start = clip.start_frame + start_offset

    item = El("clipitem").attr("id", f"clipitem-{id_suffix}")
    item.add([
        _t("masterclipid", f"masterclip-{id_suffix}"),
        _t("name", file_name),
        _t("enabled", "TRUE"),
        _t("duration", duration),
        _rate(timebase),
        _t("start", start),
        _t("end", start + duration),
        _t("in", "0"),
        _t("out", duration),
        _file_el(id_suffix, file_name, source, timebase, duration, source_w, source_h)])
    if include_motion and not video_path:
        item.add(El("filter", _motion_effect(
            clip, source_w, source_h, res, id_suffix,
            1 if frozen_motion else duration, timebase, frozen_motion, motion_head_frames)))
    if opacity_ramp:
        item.add(_opacity_filter(opacity_ramp))
    return item


def _file_el(id_suffix, file_name, path, timebase, duration, w, h) -> El:
    f = El("file").attr("id", f"file-{id_suffix}")
    f.add([_t("name", file_name), _t("pathurl", to_path_url(path)), _rate(timebase),
           _t("duration", duration),
           El("media", El("video", El("samplecharacteristics", _rate(timebase),
                                      _t("width", w), _t("height", h))))])
    return f


def _motion_effect(clip, source_w, source_h, res, id_suffix, duration, timebase,
                   frozen_motion=False, motion_head_frames=0) -> El:
    sv, ev = clip.start_viewport, clip.end_viewport

    def progress(f):
        if frozen_motion or clip.duration_frames <= 1:
            return 0.0
        return _eased(clip.easing, (f + motion_head_frames) / float(clip.duration_frames - 1))

    def vw(f):
        return sv.width + (ev.width - sv.width) * progress(f)

    def vh(f):
        return sv.height + (ev.height - sv.height) * progress(f)

    def cx(f):
        return (sv.x + sv.width / 2.0
                + (ev.x + ev.width / 2.0 - (sv.x + sv.width / 2.0)) * progress(f))

    def cy(f):
        return (sv.y + sv.height / 2.0
                + (ev.y + ev.height / 2.0 - (sv.y + sv.height / 2.0)) * progress(f))

    def scale_at(f):
        return res.width * 100.0 / vw(f)

    def horiz_at(f):
        return (source_w / 2.0 - cx(f)) * (res.width / vw(f)) / source_w

    def vert_at(f):
        return (source_h / 2.0 - cy(f)) * (res.height / vh(f)) / source_h

    if frozen_motion:
        sample_count = 1
    else:
        sample_count = min(13, max(2, int(math.ceil(duration / (timebase / 2.0)))))
    samples: List[int] = []
    for i in range(sample_count):
        samples.append(_round(i * float(duration - 1) / (sample_count - 1))
                       if duration > 1 and not frozen_motion else 0)

    def param(pid, name, first_value, keyframes):
        p = El("parameter").attr("authoringApp", "PremierePro")
        p.add([_t("parameterid", pid), _t("name", name), first_value])
        for f, val in keyframes:
            p.add(El("keyframe", _t("when", f), val))
        return p

    scale = param("scale", "Scale", _t("value", _f(scale_at(samples[0]))),
                  [(f, _t("value", _f(scale_at(f)))) for f in samples])
    center = param("center", "Center", _center(horiz_at(samples[0]), vert_at(samples[0])),
                   [(f, _center(horiz_at(f), vert_at(f))) for f in samples])
    rotation = El("parameter").attr("authoringApp", "PremierePro")
    rotation.add([_t("parameterid", "rotation"), _t("name", "Rotation"), _t("value", "0")])

    effect = El("effect").attr("id", f"basic-motion-{id_suffix}")
    effect.add([_t("name", "Motion"), _t("effectid", "basic"), _t("effectcategory", "motion"),
                _t("effecttype", "transform"), _t("mediatype", "video"),
                scale, center, rotation])
    return effect


def _opacity_filter(keyframes: Sequence[Tuple[int, float]]) -> El:
    p = El("parameter").attr("authoringApp", "PremierePro")
    p.add([_t("parameterid", "opacity"), _t("name", "opacity"), _t("valuemin", "0"),
           _t("valuemax", "100"), _t("value", _f(keyframes[0][1]))])
    for when, value in keyframes:
        p.add(El("keyframe", _t("when", when), _t("value", _f(value))))
    return El("filter", El(
        "effect", _t("name", "Opacity"), _t("effectid", "opacity"),
        _t("effectcategory", "motion"), _t("effecttype", "motion"), _t("mediatype", "video"),
        _t("pproBypass", "false"), p))


def _audio_clip_item(timeline: m.Timeline, timebase: int, audio_frames: int) -> El:
    file_name = _basename(timeline.audio.file_path)
    item = El("clipitem").attr("id", "clipitem-audio-1")
    f = El("file").attr("id", "file-audio-1")
    f.add([_t("name", file_name), _t("pathurl", to_path_url(timeline.audio.file_path)),
           _rate(timebase), _t("duration", audio_frames),
           El("media", El("audio",
                          El("samplecharacteristics", _t("depth", "16"), _t("samplerate", "48000")),
                          _t("channelcount", "2")))])
    item.add([_t("name", file_name), _t("enabled", "TRUE"), _t("duration", audio_frames),
              _rate(timebase), _t("start", "0"), _t("end", audio_frames), _t("in", "0"),
              _t("out", audio_frames), f,
              El("sourcetrack", _t("mediatype", "audio"), _t("trackindex", "1"))])
    return item


def _sound_tracks(timeline: m.Timeline, timebase: int, total_frames: int) -> List[El]:
    sounds = sorted((s for s in timeline.sounds
                     if s.file_path and s.file_path.strip() and s.start_frame < total_frames),
                    key=lambda s: s.start_frame)        # stable, like LINQ OrderBy
    tracks: List[list] = []                              # [end, items]
    number = 0
    for sound in sounds:
        number += 1
        length = max(1, min(sound.duration_frames if sound.duration_frames > 0 else timebase,
                            total_frames - sound.start_frame))
        index = next((i for i, t in enumerate(tracks) if t[0] <= sound.start_frame), -1)
        if index < 0:
            tracks.append([0, []])
            index = len(tracks) - 1
        file_name = _basename(sound.file_path)
        file_duration = sound.duration_frames if sound.duration_frames > 0 else length
        f = El("file").attr("id", f"file-sfx-{number}")
        f.add([_t("name", file_name), _t("pathurl", to_path_url(sound.file_path)),
               _rate(timebase), _t("duration", file_duration),
               El("media", El("audio",
                              El("samplecharacteristics", _t("depth", "16"),
                                 _t("samplerate", "48000")),
                              _t("channelcount", "1")))])
        item = El("clipitem").attr("id", f"clipitem-sfx-{number}")
        item.add([_t("name", file_name), _t("enabled", "TRUE"), _t("duration", file_duration),
                  _rate(timebase), _t("start", sound.start_frame),
                  _t("end", sound.start_frame + length), _t("in", "0"), _t("out", length), f,
                  El("sourcetrack", _t("mediatype", "audio"), _t("trackindex", "1"))])
        tracks[index][1].append(item)
        tracks[index][0] = sound.start_frame + length
    return [El("track", t[1], _t("enabled", "TRUE")) for t in tracks]
