"""Render an explainer production: plan -> scenes -> one video with narration.

The one entry point the app and the CLI share, so the Studio's Render button
and `cli render-demo` always do the same thing:

    build_from_plan(folder)   plan.json + subtitles -> explainer.json (when stale)
    render_video(folder, ...) every scene to a cached silent clip, joined, with audio

Finished scene clips are cached by content, so a second render only draws the
scenes that changed.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

from . import prepare as prep
from . import recipes
from . import validate as validate_mod
from .timing import compile_doc
from .validate import ExplainerError

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _find_srt(folder: str):
    for name in sorted(os.listdir(folder)):
        if name.lower().endswith(".srt"):
            return os.path.join(folder, name)
    return None


def _load_cues(folder: str):
    from whisperradar.video import parsing
    srt = _find_srt(folder)
    if srt is None:
        raise ExplainerError([f"no .srt file in {folder} (scenes are timed from the subtitles)"])
    with open(srt, encoding="utf-8-sig") as fh:
        return parsing.parse_srt(fh.read())


def needs_build(folder: str) -> bool:
    """True when plan.json exists and explainer.json is missing or older than
    the plan or the subtitles."""
    plan = os.path.join(folder, "plan.json")
    if not os.path.isfile(plan):
        return False
    out = os.path.join(folder, "explainer.json")
    if not os.path.isfile(out):
        return True
    try:                                   # built by an older builder: rebuild so its fixes apply
        with open(out, encoding="utf-8-sig") as fh:
            built_by = json.load(fh).get("builder")
        if built_by is not None and built_by != recipes.BUILDER_VERSION:
            return True
    except (OSError, ValueError, AttributeError):
        return True
    newest = os.path.getmtime(plan)
    srt = _find_srt(folder)
    if srt:
        newest = max(newest, os.path.getmtime(srt))
    return newest > os.path.getmtime(out)


def build_from_plan(folder: str, log=print) -> int:
    """plan.json + subtitles -> explainer.json. Returns the scene count."""
    plan_path = os.path.join(folder, "plan.json")
    with open(plan_path, encoding="utf-8-sig") as fh:
        try:
            plan = json.load(fh)
        except json.JSONDecodeError as e:
            raise ExplainerError([f"plan.json is not valid JSON: {e}"]) from None
    look = validate_mod.project_look(folder)
    doc = recipes.build(plan, _load_cues(folder), look)
    tmp = os.path.join(folder, ".explainer-build.json")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")
    os.replace(tmp, os.path.join(folder, "explainer.json"))
    log(f"[explainer] built explainer.json from plan.json: {len(doc['scenes'])} scene(s)")
    return len(doc["scenes"])


VISUAL_HEAD_S = 3.0        # how much of each clip's start the coverage check looks at
VISUAL_MIN_S = 0.5         # background-only longer than this is reported


def head_blank_seconds(path: str, background: str, ffmpeg: str = "ffmpeg", head: float = VISUAL_HEAD_S) -> float:
    """Seconds at the start of a clip where the frame is only the board colour (no drawing, text
    or diagram). Frames are sampled at 10 fps at 160x90; a frame counts as content when more than
    0.2% of its pixels differ clearly from the board colour."""
    h = (background or "#000000").lstrip("#")
    bg = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    w_, h_ = 160, 90
    proc = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-t", f"{head:.2f}", "-i", path,
                           "-vf", f"fps=10,scale={w_}:{h_}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                          capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    data, size = proc.stdout, w_ * h_ * 3
    blank = 0
    for f in range(len(data) // size):
        fr = data[f * size:(f + 1) * size]
        diff = sum(1 for i in range(0, size, 3)
                   if abs(fr[i] - bg[0]) + abs(fr[i + 1] - bg[1]) + abs(fr[i + 2] - bg[2]) > 60)
        if diff > w_ * h_ * 0.002:
            break
        blank += 1
    return blank / 10.0


def visual_coverage(clips: list, compiled: list, background: str, ffmpeg: str = "ffmpeg") -> dict:
    """Per scene, how long its clip opens on the bare board. Pen scenes are drawn on the board colour;
    Remotion scenes draw their own backdrop, so only pen clips are measured here (the planner's
    check covers Remotion openings)."""
    rows, empty = [], []
    for path, cs in zip(clips, compiled):
        if getattr(cs.spec, "remotion", None):
            continue
        sec = head_blank_seconds(path, background, ffmpeg)
        rows.append({"scene": cs.spec.id, "blank_head_seconds": sec})
        if sec > VISUAL_MIN_S:
            empty.append({"scene": cs.spec.id, "seconds": sec})
    return {"checked": rows, "empty": empty}


def _fps_for(folder: str) -> float:
    from whisperradar.video import options as options_mod
    opts = options_mod.load_or_default(os.path.join(folder, "imgtovideo.json"))
    return float(opts.output.fps)


def _run(args, what):
    proc = subprocess.run(args, capture_output=True, text=True, creationflags=_NOWIN)
    if proc.returncode != 0:
        raise RuntimeError(f"{what} failed (exit {proc.returncode}): {proc.stderr[-300:]}")


FADE_S = {"short": 0.25, "long": 0.5}


PEN_JOIN_HOLD_S = 0.3      # a pen scene opens on the board while its first stroke starts: hold the
                           # outgoing picture under it this long, so a join never flashes the bare board


def join_fades(compiled: list, mode: str = "") -> list:
    """Seconds of cross-fade at each join (len = scenes - 1; 0 = a hard cut). "scene fade" fades
    every join; otherwise only a join INTO a pen scene gets PEN_JOIN_HOLD_S."""
    d = FADE_S.get(mode or "")
    return [d if d else (0.0 if getattr(compiled[i + 1].spec, "remotion", None) else PEN_JOIN_HOLD_S)
            for i in range(len(compiled) - 1)]


def crossfade_graph(durations: list, fps: float, fades) -> str:
    """filter_complex joining every clip; a join with a fade > 0 cross-fades: the outgoing scene's
    last frame is held under the incoming scene, which fades in over it. A join with 0 is a hard cut.
    Every scene still starts at its own time (offsets are the original durations), the total length
    is unchanged, and the picture never dips to the bare board. Output label [v]."""
    n = len(durations)
    if not isinstance(fades, (list, tuple)):
        fades = [float(fades)] * (n - 1)
    parts = []
    for i in range(n):
        pad = f",tpad=stop_mode=clone:stop_duration={fades[i]:.3f}" if i < n - 1 and fades[i] > 0 else ""
        parts.append(f"[{i}:v]fps={fps:g},format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS{pad}[s{i}]")
    if n == 1:
        parts.append("[s0]null[v]")
        return ";".join(parts)
    last, t = "s0", 0.0
    for i in range(1, n):
        t += durations[i - 1]
        out = "v" if i == n - 1 else f"x{i}"
        if fades[i - 1] > 0:
            parts.append(f"[{last}][s{i}]xfade=transition=fade:duration={fades[i - 1]:.3f}:offset={t:.3f}[{out}]")
        else:
            parts.append(f"[{last}][s{i}]concat=n=2:v=1:a=0[{out}]")
        last = out
    return ";".join(parts)


def fade_filter(doc, compiled, clips, fps, mode) -> str:
    """Kept for callers that only want to know whether joins are faded: "" when off. The joins
    themselves are cross-faded by crossfade_graph (never a dip to the board colour)."""
    return "crossfade" if FADE_S.get(mode or "") and len(clips) >= 2 else ""


def render_video(folder: str, audio: str = "", final: bool = True,
                 log=print, cancel=None, draft: bool = False) -> str:
    """Render every scene, join them and add the narration. Returns the path.

    final=True writes out/final/final.mp4 (what the Studio treats as the
    finished video); final=False writes out/preview.mp4. `cancel` is a
    threading.Event. draft=True draws everything at half size (960x540) into
    its own cache, always as out/preview.mp4, so a check never costs a full render
    and never replaces the finished video. Raises ExplainerError for a bad document, RuntimeError
    for an ffmpeg failure, and the pipe's cancel error when cancelled.
    """
    if needs_build(folder):
        build_from_plan(folder, log)
    doc_path = os.path.join(folder, "explainer.json")
    if not os.path.isfile(doc_path):
        raise ExplainerError([f"no explainer.json (or plan.json) in {folder}"])
    doc = validate_mod.load(doc_path, project_folder=folder)
    cues = _load_cues(folder)
    fps = _fps_for(folder)
    if not audio:
        from .cli import _find_audio
        audio = _find_audio(folder) or ""
    total = None
    if audio:
        from whisperradar.video.ffprobe import ffprobe_duration_seconds
        total = ffprobe_duration_seconds(audio)
    compiled = compile_doc(doc, cues, fps, total_seconds=total)
    conflicts = validate_mod.event_property_conflicts(doc)
    if conflicts:
        raise ExplainerError(conflicts)
    from whisperradar.video import tool_locator
    ffmpeg = tool_locator.resolve("ffmpeg", "ffmpeg")

    canvas = None
    if draft:
        final = False
        canvas = (doc.canvas[0] // 2, doc.canvas[1] // 2)
        log(f"[explainer] draft: scenes are drawn at {canvas[0]}x{canvas[1]}")
    clips, n, t0 = [], len(compiled), time.time()
    for i, cs in enumerate(compiled, 1):
        before = time.time()
        clips.append(prep.render_scene_clip(doc, cs, ffmpeg, folder, fps,
                                            cancel_event=cancel, canvas=canvas))
        took = time.time() - before
        left = (time.time() - t0) / i * (n - i)
        log(f"[explainer] scene {i}/{n} '{cs.spec.id}' "
            f"{'cached' if took < 0.5 else f'{took:.0f}s'}"
            + (f" - about {left / 60:.0f} min left" if i < n and left > 60 else ""))
        if cancel is not None and cancel.is_set():
            raise RuntimeError("Render cancelled.")

    os.makedirs(prep.cache_dir(folder), exist_ok=True)
    concat_path = os.path.join(prep.cache_dir(folder), "concat.txt")
    with open(concat_path, "w", encoding="utf-8", newline="") as fh:
        fh.write("ffconcat version 1.0\n")
        for c in clips:
            fh.write("file '" + os.path.abspath(c).replace("'", "'\\''") + "'\n")
    rough = os.path.join(prep.cache_dir(folder), "rough_draft.mp4" if draft else "rough.mp4")
    fades = join_fades(compiled, validate_mod.project_look(folder).get("scene_fade"))
    log("[explainer] joining the scenes...")
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-bf", "0", "-pix_fmt", "yuv420p", rough]
    if any(f > 0 for f in fades):
        # hold the outgoing picture under the incoming one at those joins; every start time unchanged
        durations = [c.duration_frames / fps for c in compiled]
        inputs = []
        for c in clips:
            inputs += ["-i", c]
        _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y"] + inputs
             + ["-filter_complex", crossfade_graph(durations, fps, fades), "-map", "[v]"] + enc,
             "joining the scenes")
    else:
        # re-encode on concat: stream copy can drop frames at boundaries
        _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
              "-safe", "0", "-i", concat_path] + enc, "joining the scenes")
    try:
        report = visual_coverage(clips, compiled, doc.theme.background, ffmpeg)
        with open(os.path.join(folder, "out", "visual_coverage.json"), "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1)
        for r in report["empty"]:
            log(f"[explainer] WARNING scene '{r['scene']}': only the bare board for the first "
                f"{r['seconds']:.1f} s - give it a title or anchor its first element earlier")
    except Exception as exc:  # noqa: BLE001 - the check never breaks a finished render
        log(f"[explainer] visual coverage check skipped: {exc}")
    if not audio:
        return rough
    out_path = (os.path.join(folder, "out", "final", "final.mp4") if final
                else os.path.join(folder, "out", "preview.mp4"))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    log("[explainer] adding the narration...")
    tmp = out_path + ".part.mp4"
    _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", rough,
          "-i", audio, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
          "-c:a", "aac", "-b:a", "192k", "-shortest", tmp], "adding the narration")
    os.replace(tmp, out_path)
    log(f"[explainer] done: {out_path}")
    return out_path
