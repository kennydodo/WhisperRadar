"""Port of RenderPlan.cs + PreviewRenderPlanFactory.cs (ffmpeg argument/filtergraph builders).

Argument lists and filtergraph strings are byte-identical to the C# output.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import List, Optional, Sequence

from . import encoder_probe, tool_locator, transition_cuts
from . import models as m

SUPERSAMPLE_MAX_FACTOR = 8
SUPERSAMPLE_MAX_GRID_WIDTH = 16384
PREVIEW_TO_GRID_RATIO = 4.8
SOUND_GAIN = 0.8


@dataclass
class SegmentCommand:
    index: int
    output_path: str
    arguments: List[str]
    frame_count: int


@dataclass
class RenderPlan:
    segments: List[SegmentCommand]
    concat_list_path: str
    concat_list_content: str
    concat_arguments: List[str]
    rough_path: str
    mux_arguments: List[str]
    preview_path: str
    total_frames: int


def F(value) -> str:
    """C# value.ToString("0.######", InvariantCulture)."""
    s = f"{float(value):.6f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _i(value) -> str:
    return str(int(value))


def _round_away(x: float) -> int:
    return int(math.copysign(math.floor(abs(x) + 0.5), x))


def _even(value: float) -> int:
    return _round_away(value / 2) * 2


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


# ---- public API -------------------------------------------------------------------

def tail_trim_frames(transition_frames: int, options) -> int:
    return transition_cuts.tail_trim(transition_frames, options)


def head_trim_frames(transition_frames: int, options) -> int:
    return transition_cuts.head_trim(transition_frames, options)


def build(timeline, images, options, output_directory: str, preview_path: str) -> RenderPlan:
    clips = [c for s in timeline.scenes for c in s.clips]
    if not clips:
        raise RuntimeError("Timeline has no clips to render.")
    dims = {i.file_path: (i.width, i.height) for i in images}
    os.makedirs(output_directory, exist_ok=True)

    cuts = transition_cuts.compute(clips, options)
    segments: List[SegmentCommand] = []
    total = 0
    join_count = 0
    for i, clip in enumerate(clips):
        size = dims.get(clip.file_path)
        if size is None or size[0] <= 0 or size[1] <= 0:
            raise RuntimeError(f'No usable dimensions for image "{clip.file_path}"; cannot render.')
        cut_in_frames = cuts[i - 1][0] if i > 0 else 0
        cut_out_frames, cut_out_kind = cuts[i] if i < len(cuts) else (0, "none")
        piece_start = transition_cuts.head_trim(cut_in_frames, options)
        piece_end = clip.duration_frames - transition_cuts.tail_trim(cut_out_frames, options)
        piece_count = piece_end - piece_start

        seg_path = os.path.join(output_directory, f"seg_{i + 1:04d}.mp4")
        segments.append(SegmentCommand(
            len(segments) + 1, seg_path,
            _build_segment_arguments(clip, size[0], size[1], piece_start, piece_count, options, seg_path),
            piece_count))
        total += piece_count

        if cut_out_frames > 0:
            nxt = clips[i + 1]
            nsize = dims.get(nxt.file_path)
            if nsize is None or nsize[0] <= 0 or nsize[1] <= 0:
                raise RuntimeError(f'No usable dimensions for image "{nxt.file_path}"; cannot render.')
            join_count += 1
            join_path = os.path.join(output_directory, f"join_{join_count:04d}.mp4")
            segments.append(SegmentCommand(
                len(segments) + 1, join_path,
                _build_join_arguments(clip, size[0], size[1], nxt, nsize[0], nsize[1],
                                      cut_out_frames, cut_out_kind, options, join_path),
                cut_out_frames))
            total += cut_out_frames

    rough = os.path.join(output_directory, "rough.mp4")
    concat_path = os.path.join(output_directory, "concat.txt")
    return RenderPlan(
        segments=segments,
        concat_list_path=concat_path,
        concat_list_content=_build_concat_list(segments),
        concat_arguments=_build_concat_arguments(concat_path, rough),
        rough_path=rough,
        mux_arguments=build_mux_arguments(rough, timeline.audio.file_path, preview_path,
                                          timeline.sounds, timeline.fps),
        preview_path=preview_path,
        total_frames=total)


def build_clip_preview_arguments(clip, source_width, source_height, options, output_path) -> List[str]:
    return _build_segment_arguments(clip, source_width, source_height, 0, clip.duration_frames,
                                    options, output_path)


def build_clip_preview_mux_arguments(video_path, audio_path, start_seconds, duration_seconds,
                                     output_path) -> List[str]:
    return ["-hide_banner", "-loglevel", "error", "-y",
            "-i", video_path, "-ss", F(start_seconds), "-i", audio_path,
            "-t", F(duration_seconds), "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", output_path]


def _source_input_arguments(file_path: str, options) -> List[str]:
    return ["-loop", "1", "-r", F(options.output.fps), "-i", file_path]


def _build_segment_arguments(clip, sw, sh, piece_start, piece_count, options, output_path) -> List[str]:
    if piece_count < 1:
        raise RuntimeError(f'Clip "{clip.file_path}" has an empty render range; cannot render.')
    return _wrap_segment_arguments(
        _source_input_arguments(clip.file_path, options), None,
        _build_side_chain(clip, sw, sh, piece_start, piece_count, options),
        piece_count, options.render, output_path)


def _build_join_arguments(outgoing, ow, oh, incoming, iw, ih, transition_frames, kind, options,
                          output_path) -> List[str]:
    out_tail = transition_cuts.tail_trim(transition_frames, options)
    in_head = transition_cuts.head_trim(transition_frames, options)
    outgoing_chain = _build_side_chain(outgoing, ow, oh, outgoing.duration_frames - out_tail,
                                       transition_frames, options)
    incoming_chain = _build_side_chain(incoming, iw, ih, in_head - transition_frames,
                                       transition_frames, options)
    fc = (f"[0:v]{outgoing_chain}[va];[1:v]{incoming_chain}[vb];"
          f"[va][vb]xfade=transition={m.to_xfade_name(kind)}:"
          f"duration={F(transition_frames / float(options.output.fps))}:offset=0,"
          "format=yuv420p")
    return _wrap_segment_arguments(
        _source_input_arguments(outgoing.file_path, options)
        + _source_input_arguments(incoming.file_path, options),
        fc, None, transition_frames, options.render, output_path)


def supersample_for(source_width: int, target_width: int) -> int:
    if target_width <= 0 or source_width >= target_width:
        return 1
    grid_cap = int(min(SUPERSAMPLE_MAX_FACTOR, SUPERSAMPLE_MAX_GRID_WIDTH // int(source_width)))
    if grid_cap < 2:
        return 1
    factor = int(math.ceil(target_width / float(source_width)))
    return _clamp(factor, 2, grid_cap)


def supersample_target_for(options) -> int:
    r = options.render
    if r.supersample_target_width <= 0:
        return 0
    return max(int(r.supersample_target_width), int(round(r.preview_width * PREVIEW_TO_GRID_RATIO)))


def _viewport_size_changes(clip) -> bool:
    return (abs(clip.start_viewport.width - clip.end_viewport.width) > 0.01
            or abs(clip.start_viewport.height - clip.end_viewport.height) > 0.01)


def _build_side_chain(clip, sw, sh, piece_start, piece_count, options) -> str:
    out_aspect = options.output.width / options.output.height
    bounds = m.Rect(0, 0, sw, sh)
    sv, ev = clip.start_viewport, clip.end_viewport
    start_aspect = sv.width / sv.height
    end_aspect = ev.width / ev.height
    matches = abs(start_aspect - out_aspect) <= 0.02 and abs(end_aspect - out_aspect) <= 0.02
    r = options.render

    if not matches:
        return (f"scale={r.preview_width}:{r.preview_height}:force_original_aspect_ratio=decrease,"
                f"pad={r.preview_width}:{r.preview_height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p")

    inside = sv.is_inside(bounds) and ev.is_inside(bounds)
    size_changes = _viewport_size_changes(clip)

    if inside:
        ss = supersample_for(sw, supersample_target_for(options))
        scaled_w, scaled_h = sw * ss, sh * ss
        chain = f"scale={scaled_w}:{scaled_h}:flags=lanczos," if ss > 1 else ""
        if size_changes:
            return (chain + _build_zoom_pan_filter(clip, sw, scaled_w, scaled_h, piece_start,
                                                   piece_count, options) + ",format=yuv420p")
        return (chain + _loop_stage(piece_count)
                + _build_fixed_size_crop_filter(clip, sw, scaled_w, scaled_h, piece_start,
                                                piece_count, options) + ",format=yuv420p")

    canvas_w = _even(max(max(sv.right, ev.right), sw))
    canvas_h = _even(max(max(sv.bottom, ev.bottom), sh))
    f = supersample_for(canvas_w, supersample_target_for(options))
    sc_cw, sc_ch = canvas_w * f, canvas_h * f
    sc_iw, sc_ih = sw * f, sh * f
    pad_chain = (f"scale={sc_iw}:{sc_ih}:flags=lanczos,"
                 f"pad={sc_cw}:{sc_ch}:(ow-iw)/2:(oh-ih)/2,")
    if size_changes:
        return (pad_chain + _build_zoom_pan_filter(clip, canvas_w, sc_cw, sc_ch, piece_start,
                                                   piece_count, options) + ",format=yuv420p")
    return (pad_chain + _loop_stage(piece_count)
            + _build_fixed_size_crop_filter(clip, canvas_w, sc_cw, sc_ch, piece_start,
                                            piece_count, options) + ",format=yuv420p")


def _wrap_segment_arguments(inputs, filter_complex, video_filter, frame_count, render, output_path):
    args = ["-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats", "-y"]
    args += list(inputs)
    if filter_complex is not None:
        args += ["-filter_complex", filter_complex]
    elif video_filter is not None:
        args += ["-vf", video_filter]
    args += ["-frames:v", _i(frame_count)]
    enc = render.encoder
    resolved = resolve_encoder(render) if (not enc or not enc.strip() or enc.strip().lower() == "auto") else enc
    args += video_encoder_args(resolved, render.preview_preset, render.preview_crf, render.preview_bframes)
    args += ["-an", output_path]
    return args


def resolve_encoder(render) -> str:
    configured = (render.encoder or "auto").strip().lower()
    if configured in ("libx264", "cpu"):
        return "libx264"
    if configured in ("h264_nvenc", "nvenc"):
        candidates = ["h264_nvenc"]
    elif configured in ("h264_amf", "amf"):
        candidates = ["h264_amf"]
    elif configured in ("h264_qsv", "qsv"):
        candidates = ["h264_qsv"]
    else:
        candidates = ["h264_nvenc", "h264_amf", "h264_qsv"]
    ffmpeg_path = resolve_effective_ffmpeg_path(render)
    for c in candidates:
        if encoder_probe.supports(ffmpeg_path, c):
            return c
    return "libx264"


def _has_working_hardware_encoder(path: str) -> bool:
    return (encoder_probe.supports(path, "h264_nvenc") or encoder_probe.supports(path, "h264_amf")
            or encoder_probe.supports(path, "h264_qsv"))


def resolve_effective_ffmpeg_path(render) -> str:
    configured_path = tool_locator.resolve("ffmpeg", render.ffmpeg_path)
    encoder = (render.encoder or "auto").strip()
    is_auto = encoder == "" or encoder.lower() == "auto"
    if not is_auto or _has_working_hardware_encoder(configured_path):
        return configured_path
    for cand in tool_locator.resolve_all_candidates("ffmpeg", render.ffmpeg_path):
        if cand.lower() != configured_path.lower() and _has_working_hardware_encoder(cand):
            return cand
    return configured_path


def describe_effective_encoder(render) -> str:
    enc = render.encoder
    encoder = resolve_encoder(render) if (not enc or not enc.strip() or enc.strip().lower() == "auto") else enc
    return f"{encoder} via {resolve_effective_ffmpeg_path(render)}"


def video_encoder_args(encoder: str, preset: str, crf: int, bframes: int = 3) -> List[str]:
    crf_t, bf = _i(crf), _i(bframes)
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", _nvenc_preset(preset), "-rc", "vbr",
                "-cq", crf_t, "-b:v", "0", "-bf", bf]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", _qsv_preset(preset), "-global_quality", crf_t, "-bf", bf]
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", _amf_quality(preset), "-rc", "cqp",
                "-qp_i", crf_t, "-qp_p", crf_t, "-bf", bf]
    return ["-c:v", "libx264", "-preset", preset, "-crf", crf_t, "-bf", bf]


_NVENC = {"ultrafast": "p1", "superfast": "p2", "veryfast": "p3", "faster": "p4", "fast": "p4",
          "medium": "p5", "slow": "p6", "slower": "p7", "veryslow": "p7"}


def _nvenc_preset(p: str) -> str:
    return _NVENC.get(p.lower(), "p5")


def _qsv_preset(p: str) -> str:
    l = p.lower()
    if l in ("ultrafast", "superfast", "veryfast"):
        return "veryfast"
    if l in ("faster", "fast", "medium", "slow", "slower", "veryslow"):
        return l
    return "medium"


def _amf_quality(p: str) -> str:
    l = p.lower()
    if l in ("ultrafast", "superfast", "veryfast", "faster", "fast"):
        return "speed"
    if l in ("slow", "slower", "veryslow"):
        return "quality"
    return "balanced"


def _loop_stage(piece_count: int) -> str:
    return f"loop=loop={_i(max(0, piece_count - 1))}:size=1:start=0,"


def _motion_steps(clip):
    duration = clip.duration_frames
    steps = duration - 1 if duration > 1 else 1
    mdf = clip.motion_duration_frames
    if mdf is not None and mdf > 1:
        motion_steps = max(1, min(mdf - 1, steps))
    else:
        motion_steps = steps
    return duration, steps, motion_steps


def _build_fixed_size_crop_filter(clip, source_width, scaled_width, scaled_height, piece_start,
                                  piece_count, options) -> str:
    clip_duration, steps, motion_steps = _motion_steps(clip)
    ss = scaled_width / float(source_width)
    sv, ev = clip.start_viewport, clip.end_viewport
    width = ss * sv.width
    height = ss * sv.height
    x_start, x_end = ss * sv.x, ss * ev.x
    y_start, y_end = ss * sv.y, ss * ev.y

    if piece_count <= 1:
        progress = _easing_value(clip.easing, piece_start / motion_steps if clip_duration > 1 else 0.0)
        x_expr = F(x_start + (x_end - x_start) * progress)
        y_expr = F(y_start + (y_end - y_start) * progress)
    else:
        raw = f"((n{_signed(piece_start)})/{motion_steps})"
        if motion_steps != steps:
            raw = f"min(1,{raw})"
        progress = _easing_expression(clip.easing, raw)
        x_expr = f"min(iw-ow,max(0,{F(x_start)}+({F(x_end - x_start)})*{progress}))"
        y_expr = f"min(ih-oh,max(0,{F(y_start)}+({F(y_end - y_start)})*{progress}))"
    r = options.render
    return (f"crop=w='{F(width)}':h='{F(height)}':x='{x_expr}':y='{y_expr}',"
            f"scale={r.preview_width}:{r.preview_height}")


def _build_zoom_pan_filter(clip, source_width, scaled_width, scaled_height, piece_start,
                           piece_count, options) -> str:
    clip_duration, steps, motion_steps = _motion_steps(clip)
    ss = scaled_width / float(source_width)
    sv, ev = clip.start_viewport, clip.end_viewport
    width_start = ss * sv.width
    width_end = ss * ev.width
    cx_start = ss * (sv.x + sv.width / 2.0)
    cx_end = ss * (ev.x + ev.width / 2.0)
    cy_start = ss * (sv.y + sv.height / 2.0)
    cy_end = ss * (ev.y + ev.height / 2.0)
    zoom_high = max(source_width / sv.width, source_width / ev.width)
    zoom_low = min(source_width / sv.width, source_width / ev.width)

    if piece_count <= 1:
        progress = _easing_value(clip.easing, piece_start / motion_steps if clip_duration > 1 else 0.0)
        width = sv.width + (ev.width - sv.width) * progress
        center_x = (sv.x + sv.width / 2.0) + ((ev.x - sv.x) * progress)
        center_y = (sv.y + sv.height / 2.0) + ((ev.y - sv.y) * progress)
        zoom = source_width / width
        zoom_expr = F(zoom)
        x_expr = F(ss * center_x - scaled_width / zoom / 2.0)
        y_expr = F(ss * center_y - scaled_height / zoom / 2.0)
    else:
        raw = f"((on{_signed(piece_start)})/{motion_steps})"
        if motion_steps != steps:
            raw = f"min(1,{raw})"
        progress = _easing_expression(clip.easing, raw)
        zoom_expr = (f"min({F(zoom_high)},max({F(zoom_low)},{F(scaled_width)}/"
                     f"({F(width_start)}+({F(width_end - width_start)})*{progress})))")
        x_expr = (f"min(max(0,({F(cx_start)}+({F(cx_end - cx_start)})*{progress})-iw/zoom/2),"
                  "iw-iw/zoom)")
        y_expr = (f"min(max(0,({F(cy_start)}+({F(cy_end - cy_start)})*{progress})-ih/zoom/2),"
                  "ih-ih/zoom)")
    r = options.render
    return (f"zoompan=z='{zoom_expr}':x='{x_expr}':y='{y_expr}'"
            f":d={_i(piece_count)}:s={r.preview_width}x{r.preview_height}:fps={F(options.output.fps)}")


def _easing_expression(easing: str, progress: str) -> str:
    clamped = f"min(1,max(0,{progress}))"
    if easing == m.EASE_IN:
        return f"pow({clamped},2)"
    if easing == m.EASE_OUT:
        return f"1-pow(1-{clamped},2)"
    if easing == m.EASE_IN_OUT:
        return f"pow({clamped},3)*({clamped}*({clamped}*6-15)+10)"
    return progress


def _easing_value(easing: str, progress: float) -> float:
    progress = _clamp(progress, 0.0, 1.0)
    if easing == m.EASE_IN:
        return progress * progress
    if easing == m.EASE_OUT:
        return 1 - (1 - progress) * (1 - progress)
    if easing == m.EASE_IN_OUT:
        return progress * progress * progress * (progress * (progress * 6 - 15) + 10)
    return progress


def _signed(piece_start: int) -> str:
    return str(int(piece_start)) if piece_start < 0 else "+" + str(int(piece_start))


def _build_concat_list(segments) -> str:
    out = "ffconcat version 1.0\n"
    for s in segments:
        out += "file '" + s.output_path.replace("'", "'\\''") + "'\n"
    return out


def _build_concat_arguments(concat_list_path: str, rough_path: str) -> List[str]:
    return ["-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_list_path, "-c", "copy", rough_path]


def build_mux_arguments(rough_path: str, audio_path: str, preview_path: str,
                        sounds: Optional[Sequence] = None, fps: float = 30) -> List[str]:
    usable = sorted((s for s in (sounds or []) if s.file_path and s.file_path.strip()),
                    key=lambda s: s.start_frame)
    args = ["-hide_banner", "-loglevel", "error", "-y", "-i", rough_path, "-i", audio_path]
    if not usable:
        args += ["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                 "-shortest", preview_path]
        return args
    common = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    graph = [f"[1:a]{common}[n]"]
    labels = "[n]"
    for i, snd in enumerate(usable):
        args += ["-i", snd.file_path]
        delay_ms = int(round(snd.start_frame * 1000.0 / fps))
        graph.append(f"[{i + 2}:a]{common},adelay={delay_ms}:all=1,volume={F(SOUND_GAIN)}[s{i}]")
        labels += f"[s{i}]"
    graph.append(f"{labels}amix=inputs={len(usable) + 1}:duration=first:dropout_transition=0:normalize=0[aout]")
    args += ["-filter_complex", ";".join(graph), "-map", "0:v:0", "-map", "[aout]",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", preview_path]
    return args
