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


def _fps_for(folder: str) -> float:
    from whisperradar.video import options as options_mod
    opts = options_mod.load_or_default(os.path.join(folder, "imgtovideo.json"))
    return float(opts.output.fps)


def _run(args, what):
    proc = subprocess.run(args, capture_output=True, text=True, creationflags=_NOWIN)
    if proc.returncode != 0:
        raise RuntimeError(f"{what} failed (exit {proc.returncode}): {proc.stderr[-300:]}")


FADE_S = {"short": 0.25, "long": 0.5}


def fade_filter(doc, compiled, clips, fps, mode) -> str:
    """ffmpeg -vf that dips to the board colour at each join between two pen
    scenes (a scene's length never changes, so the narration stays in step).
    "" when off or there is nothing to fade."""
    d = FADE_S.get(mode or "")
    if not d or len(clips) < 2:
        return ""
    from whisperradar.video import tool_locator
    from whisperradar.video.ffprobe import ffprobe_duration_seconds
    probe = tool_locator.resolve("ffprobe", "ffprobe")
    colour = "0x" + doc.theme.background.lstrip("#")
    parts, t = [], 0.0
    for i, c in enumerate(clips[:-1]):
        t += ffprobe_duration_seconds(c, probe)
        a, b = compiled[i].spec, compiled[i + 1].spec
        if getattr(a, "remotion", None) or getattr(b, "remotion", None):
            continue                      # motion scenes fade themselves
        parts.append(f"fade=t=out:st={t - d:.3f}:d={d}:color={colour}")
        parts.append(f"fade=t=in:st={t:.3f}:d={d}:color={colour}")
    return ",".join(parts)


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
    vf = fade_filter(doc, compiled, clips, fps, validate_mod.project_look(folder).get("scene_fade"))
    log("[explainer] joining the scenes...")
    # re-encode on concat: stream copy can drop frames at boundaries
    _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
          "-safe", "0", "-i", concat_path] + (["-vf", vf] if vf else []) + ["-c:v", "libx264", "-preset",
          "veryfast", "-crf", "18", "-bf", "0", "-pix_fmt", "yuv420p", rough],
         "joining the scenes")
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
