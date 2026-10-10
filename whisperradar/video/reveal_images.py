"""Port of RevealImageWriter.cs: write the partial stills a reveal shot needs."""
from __future__ import annotations

import os
from typing import List, Sequence, Tuple

from . import imaging  # noqa: F401  (kept for parity; sizes come from ImageInfo)
from . import models as m
from .ffmpeg_runner import FfmpegRunner


def _same_path(a: str, b: str) -> bool:
    return os.path.abspath(a).lower() == os.path.abspath(b).lower()


def _missing_steps(timeline, images) -> List[Tuple[str, m.RevealInfo]]:
    known = {i.file_path.lower() for i in images}
    found, seen = [], set()
    for scene in timeline.scenes:
        for clip in scene.clips:
            rv = clip.reveal
            if rv is None or clip.file_path.lower() in known or clip.file_path.lower() in seen:
                continue
            seen.add(clip.file_path.lower())
            if not rv.is_stack and (rv.source_path is None or rv.visible >= rv.count):
                continue
            found.append((clip.file_path, rv))
    return found


def needs_images(timeline, images) -> bool:
    return len(_missing_steps(timeline, images)) > 0


def ensure(timeline, images: Sequence[m.ImageInfo], runner: FfmpegRunner, cancel_event=None) -> List[m.ImageInfo]:
    missing = _missing_steps(timeline, images)
    if not missing:
        return list(images)
    result = list(images)
    width, height = timeline.resolution.width, timeline.resolution.height
    for path, info in missing:
        sources = list(info.sources[:info.visible]) if info.is_stack else [info.source_path]
        first = next((i for i in images if _same_path(i.file_path, sources[0])), None)
        if first is None:
            continue
        w, h = (width, height) if info.is_stack else (first.width, first.height)
        up_to_date = os.path.exists(path) and all(
            os.path.getmtime(path) >= os.path.getmtime(s) for s in sources if os.path.exists(s))
        if not up_to_date:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if info.is_stack:
                args = build_stack_arguments(sources, w, h, info.count, info.layout, path)
            elif info.layout == m.REVEAL_GRID:
                args = build_grid_arguments(sources[0], w, h, info.visible, path)
            else:
                args = build_arguments(sources[0], w, h, info.visible, info.count, path)
            proc = runner.run(args, cancel_event=cancel_event)
            if not proc.success or not os.path.exists(path):
                raise RuntimeError(
                    f'Could not write the reveal still "{os.path.basename(path)}": {proc.error_tail}')
        result.append(m.ImageInfo(path, first.name, w, h))
    return result


def build_arguments(source_path: str, width: int, height: int, k: int, n: int, output_path: str) -> List[str]:
    visible = max(1, width * k // n)
    return ["-hide_banner", "-loglevel", "error", "-y", "-i", source_path,
            "-vf", f"crop={visible}:{height}:0:0,pad={width}:{height}:0:0:color=black",
            "-frames:v", "1", output_path]


def build_grid_arguments(source_path: str, width: int, height: int, k: int, output_path: str) -> List[str]:
    half_w, half_h = width // 2, height // 2
    corners = [(0, 0), (width - half_w, 0), (0, height - half_h), (width - half_w, height - half_h)]
    boxes = [f"drawbox=x={corners[q][0]}:y={corners[q][1]}:w={half_w}:h={half_h}:color=black:t=fill"
             for q in range(max(0, k), 4)]
    return ["-hide_banner", "-loglevel", "error", "-y", "-i", source_path,
            "-vf", ",".join(boxes) if boxes else "null", "-frames:v", "1", output_path]


def build_stack_arguments(sources: Sequence[str], width: int, height: int, count: int, layout: str,
                          output_path: str) -> List[str]:
    slots = slot_rects(width, height, count, layout)
    args = ["-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:d=1:r=1"]
    for s in sources:
        args += ["-i", s]
    graph = []
    previous = "0:v"
    for i in range(len(sources)):
        x, y, w, h = slots[i]
        graph.append(f"[{i + 1}:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1[s{i}]")
        label = f"o{i}"
        graph.append(f"[{previous}][s{i}]overlay={x}:{y}[{label}]")
        previous = label
    args += ["-filter_complex", ";".join(graph), "-map", f"[{previous}]", "-frames:v", "1", output_path]
    return args


def slot_rects(width: int, height: int, count: int, layout: str):
    slots = []
    if m.reveal_layout_normalize(layout) == m.REVEAL_GRID:
        w, h = width // 2, height // 2
        for i in range(count):
            slots.append((0 if i % 2 == 0 else width - w, 0 if i // 2 == 0 else height - h, w, h))
    else:
        for i in range(count):
            x0 = width * i // count
            x1 = width * (i + 1) // count
            slots.append((x0, 0, x1 - x0, height))
    return slots
