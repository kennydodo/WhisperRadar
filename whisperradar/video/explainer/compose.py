"""Filtergraph composer: scene -> ffmpeg args that animate plates.

Hybrid, FFmpeg-first: Pillow plates fade/slide via overlay+fade; counters use
drawtext %{eif}; bars/grids/blocks/paths are rendered by the frame-pipe backend
(pipe.py) when a scene mixes them with text.

MVP scene backend rule (documented in architecture.md):
- a scene with ONLY text/label/callout/image objects runs fully in the
  filtergraph;
- any scene containing path/bar/counter/grid/blocks runs through the
  frame-pipe backend, which composites plates itself (identical state model).

Event semantics are the SAME in both backends (declared events drive the
animation; nothing is automatic):
- fade_in at its event window;
- fade_out at its event window (required to remove an object);
- slide over its window (ease-out);
- type_on = plate SEQUENCE shown in successive time windows, then the final
  plate holds until the scene ends (or a fade_out window begins);
- image plates composite at their box position.
"""
from __future__ import annotations

from . import schema as sc
from .timing import CompiledScene


def needs_pipe(scene: CompiledScene) -> bool:
    return any(o.type in (sc.PATH, sc.BAR, sc.COUNTER, sc.GRID, sc.BLOCKS,
                          sc.STROKE_ASSET, sc.HANDWRITING)
               for o in scene.spec.objects)


def _f(v) -> str:
    s = f"{float(v):.6f}".rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


def _i(v) -> str:
    return str(int(v))


def build_filtergraph_scene(scene: CompiledScene, laid_out, plates: dict,
                            theme, canvas: tuple, fps: float,
                            background: str) -> list:
    """Arguments for a text/image-only scene: color background + overlay chains.

    Returns (inputs, graph). Each plate input is a still (-loop 1) whose
    opacity/position is driven per frame by the object's declared events.
    """
    W, H = canvas
    # d=0 would make the color source infinite; the caller caps with
    # -frames:v. r must match the scene fps so t-frames map 1:1.
    inputs: list = ["-f", "lavfi", "-i",
                    f"color=c={background}:s={W}x{H}:r={_f(fps)}"]
    graph: list = []
    prev = "0:v"
    file_index = 1    # 0 = the color source; counts -i occurrences, not args

    dur = scene.duration_frames / fps
    by_id = {lo.spec.id: lo for lo in laid_out.objects}

    for lo in laid_out.objects:
        spec = lo.spec
        if spec.type not in (sc.TEXT, sc.LABEL, sc.CALLOUT, sc.IMAGE):
            continue
        entry = plates.get(spec.id)
        if not entry:
            continue
        paths, kind, origin = entry
        ox, oy = origin
        events = [c for c in scene.events if c.event.target == spec.id]
        fade_in = next((c for c in events if c.event.action == sc.FADE_IN), None)
        fade_out = next((c for c in events if c.event.action == sc.FADE_OUT), None)
        slide = next((c for c in events if c.event.action == sc.SLIDE), None)
        type_on = next((c for c in events if c.event.action == sc.TYPE_ON), None)

        # first appearance
        appear = min((c.start_frame for c in events), default=0)
        start_s = appear / fps
        # the window the object is visible in: from its first event until its
        # declared fade_out starts (or the scene ends)
        if fade_out is not None:
            out_s = fade_out.start_frame / fps
            out_dur = max(fade_out.duration_frames / fps, 0.04)
        else:
            out_s = None
            out_dur = 0.0

        for pi, p in enumerate(paths):
            idx = file_index
            file_index += 1
            # -loop 1: a still input must become a real frame stream or the
            # fade/enable expressions evaluate only on a single t=0 frame.
            inputs += ["-loop", "1", "-r", _f(fps), "-i", p]
            label = f"v{idx}"

            x = _f(ox)
            y = _f(oy)
            if slide is not None and slide.event.direction:
                dist = slide.event.offset_px
                move_dur = max(slide.duration_frames / fps, 0.04)
                mv = f"({_i(dist)}*(1-min(1,(t-{_f(start_s)})/{_f(move_dur)})))"
                d = slide.event.direction
                if d == "left":
                    x = f"{x}-{mv}"
                elif d == "right":
                    x = f"{x}+{mv}"
                elif d == "up":
                    y = f"{y}-{mv}"
                else:
                    y = f"{y}+{mv}"

            # alpha chain: declared fade_in on the plate stream
            chain = ["format=rgba"]
            is_sliced = type_on is not None and len(paths) > 1
            if fade_in is not None:
                fin_dur = max(fade_in.duration_frames / fps, 0.04)
                chain.append(f"fade=t=in:st={_f(start_s)}:d={_f(fin_dur)}:alpha=1")
            # the visibility window is the OVERLAY's enable (overlay supports
            # the timeline; format does not): type-on slices, else gte(start)
            if is_sliced:
                t_dur = max(type_on.duration_frames / fps, 0.04)
                slice_t = t_dur / len(paths)
                win_in = start_s + pi * slice_t
                is_last = pi == len(paths) - 1
                win_out = out_s if (is_last and out_s is not None) \
                    else start_s + (pi + 1) * slice_t
                enable = f"between(t,{_f(win_in)},{_f(max(win_in, win_out))})"
                if fade_in is None and pi == 0:
                    chain.append(f"fade=t=in:st={_f(win_in)}:d=0.04:alpha=1")
                if is_last and out_s is not None:
                    chain.append(f"fade=t=out:st={_f(out_s)}:d={_f(out_dur)}:alpha=1")
            else:
                enable = f"gte(t,{_f(start_s)})"
                if out_s is not None:
                    chain.append(f"fade=t=out:st={_f(out_s)}:d={_f(out_dur)}:alpha=1")

            graph.append(f"[{idx}:v]{','.join(chain)}[f{idx}]")
            graph.append(
                f"[{prev}][f{idx}]overlay=x='{x}':y='{y}':eval=frame"
                f":enable='{enable}'[{label}]")
            prev = label
    if not graph:
        return inputs, None
    graph.append(f"[{prev}]format=yuv420p[vout]")
    return inputs, ";".join(graph)
