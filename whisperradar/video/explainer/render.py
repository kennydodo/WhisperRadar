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
import re
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
            existing = json.load(fh)
        built_by = existing.get("builder")
        if built_by is not None and built_by != recipes.BUILDER_VERSION:
            return True
        if built_by is None and generated_from_plan(folder, existing):
            return True                    # an unmarked file the builder made: migrate (backed up first)
    except (OSError, ValueError, AttributeError):
        return True
    newest = os.path.getmtime(plan)
    srt = _find_srt(folder)
    if srt:
        newest = max(newest, os.path.getmtime(srt))
    return newest > os.path.getmtime(out)


# Keys the builder computes (timing, placement) and may change between versions: never content.
_BUILT_KEYS = {"at_ms", "start_ms", "run_ms", "count_ms", "type_ms", "zoom_ms", "beats_ms", "offset_ms",
               "duration_ms", "box", "points", "x", "y", "w", "h", "fit_to", "shot_id", "cue_range", "events"}


def _is_text_points(v) -> bool:
    """Scene `points` (spoken bullets: [{text, start_ms}]) are content; stroke `points` are coordinates."""
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) and "text" in x for x in v)


def _content(v):
    """A scene part without the builder-computed keys (recursively)."""
    if isinstance(v, dict):
        return {k: _content(x) for k, x in v.items() if k not in _BUILT_KEYS or (k == "points" and _is_text_points(x))}
    if isinstance(v, list):
        return [_content(x) for x in v]
    return v


def _contained(old, new) -> bool:
    """Every content value in `old` is what the builder makes now (`new`). Keys a newer builder
    adds are allowed; a changed value, an extra key or a different list length is a hand edit."""
    if isinstance(old, dict):
        return isinstance(new, dict) and all(k in new and _contained(x, new[k]) for k, x in old.items())
    if isinstance(old, list):
        return isinstance(new, list) and len(old) == len(new) and all(_contained(a, b) for a, b in zip(old, new))
    if isinstance(old, (int, float)) and isinstance(new, (int, float)) and not isinstance(old, bool):
        return abs(old - new) < 1e-9
    return old == new


def _same_content(existing: dict, fresh: dict) -> bool:
    """True when `existing` says nothing the builder would not say from the plan: same scenes in the
    same order, the same drawn objects with the same texts, colours and assets, and Remotion blocks
    whose labels, values, units, notes and other parameters are all what the builder makes."""
    a, b = existing.get("scenes") or [], fresh.get("scenes") or []
    if [s.get("id") for s in a] != [s.get("id") for s in b]:
        return False
    for sa, sb in zip(a, b):
        ra, rb = sa.get("remotion"), sb.get("remotion")
        if bool(ra) != bool(rb):
            return False
        if ra:
            if ra.get("component") != rb.get("component") or not _contained(_content(ra), _content(rb)):
                return False
            continue
        oa = {o.get("id"): o for o in sa.get("objects") or [] if isinstance(o, dict)}
        ob = {o.get("id"): o for o in sb.get("objects") or [] if isinstance(o, dict)}
        if set(oa) != set(ob) or not all(_contained(_content(oa[k]), _content(ob[k])) for k in oa):
            return False
    return True


def generated_from_plan(folder: str, existing: dict) -> bool:
    """True when an explainer.json without a builder stamp is what the builder makes from this
    folder's plan.json - i.e. not a hand-made or hand-edited file (pen or Remotion scenes)."""
    try:
        with open(os.path.join(folder, "plan.json"), encoding="utf-8-sig") as fh:
            plan = json.load(fh)
        fresh = recipes.build(plan, _load_cues(folder), validate_mod.project_look(folder))
    except Exception:  # noqa: BLE001 - when in doubt, never overwrite
        return False
    return _same_content(existing, fresh)


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
    target = os.path.join(folder, "explainer.json")
    if os.path.isfile(target):                 # never lose a previous file: keep it beside the new one
        try:
            with open(target, encoding="utf-8-sig") as fh:
                prev_by = json.load(fh).get("builder")
        except (OSError, ValueError, AttributeError):
            prev_by = None
        if prev_by != recipes.BUILDER_VERSION:
            import shutil
            shutil.copy2(target, os.path.join(folder, f"explainer.before-rebuild-{time.strftime('%Y%m%d-%H%M%S')}.json"))
    os.replace(tmp, target)
    log(f"[explainer] built explainer.json from plan.json: {len(doc['scenes'])} scene(s)")
    return len(doc["scenes"])


VISUAL_FPS = 10            # frames per second the coverage scan samples
VISUAL_MIN_S = 0.3         # a background-only stretch longer than this is reported
CONTENT_CONTRAST = 60      # a frame has content when some pixel differs from the board this much (luma)


def _luma(hex_colour: str) -> float:
    h = (hex_colour or "#000000").lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.299 * r + 0.587 * g + 0.114 * b


def blank_spans(path: str, background: str, ffmpeg: str = "ffmpeg", fps: int = VISUAL_FPS,
                min_s: float = VISUAL_MIN_S) -> list:
    """[(start, end)] seconds where a video shows nothing but the board/backdrop. A frame counts as
    content when its brightest (dark board) or darkest (light board) pixel differs from the board
    colour by CONTENT_CONTRAST - text, icons, bars and lines do; the board and the motion scenes'
    faint grid backdrop do not. Raises RuntimeError when the video cannot be scanned: a failed scan
    is never reported as clean."""
    if not os.path.isfile(path):
        raise RuntimeError(f"cannot scan {path}: the file does not exist")
    proc = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-i", path, "-an", "-vf",
                           f"fps={fps},scale=480:-2,signalstats,metadata=print:file=-", "-f", "null", "-"],
                          capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    ymax = [int(x) for x in re.findall(r"lavfi\.signalstats\.YMAX=(\d+)", proc.stdout)]
    ymin = [int(x) for x in re.findall(r"lavfi\.signalstats\.YMIN=(\d+)", proc.stdout)]
    if proc.returncode != 0 or not ymax or len(ymin) != len(ymax):
        raise RuntimeError(f"cannot scan {os.path.basename(path)}: ffmpeg exit {proc.returncode} "
                           f"{(proc.stderr or '')[-200:].strip()}")
    bg = _luma(background)
    dark = bg < 128
    spans, start = [], None
    for i, (hi, lo) in enumerate(zip(ymax, ymin)):
        content = (hi - bg >= CONTENT_CONTRAST) if dark else (bg - lo >= CONTENT_CONTRAST)
        if not content and start is None:
            start = i
        if content and start is not None:
            spans.append((start, i))
            start = None
    if start is not None:
        spans.append((start, len(ymax)))
    return [(a / fps, b / fps) for a, b in spans if (b - a) / fps > min_s]


def head_blank_seconds(path: str, background: str, ffmpeg: str = "ffmpeg") -> float:
    """Seconds a clip opens on the bare board (raises when it cannot be scanned)."""
    spans = blank_spans(path, background, ffmpeg, min_s=0.0)
    return spans[0][1] if spans and spans[0][0] == 0 else 0.0


def visual_coverage(video: str, compiled: list, fps: float, background: str, ffmpeg: str = "ffmpeg") -> dict:
    """Scan the whole joined video for background-only stretches and name the scene each falls in.
    {"verified": bool, "error": str, "empty": [{scene, start, end, seconds}]}"""
    try:
        spans = blank_spans(video, background, ffmpeg)
    except RuntimeError as exc:
        return {"verified": False, "error": str(exc), "empty": []}
    bounds, t = [], 0.0
    for cs in compiled:
        d = cs.duration_frames / fps
        bounds.append((cs.spec.id, t, t + d))
        t += d
    empty = []
    for a, b in spans:
        sid = next((s for s, x, y in bounds if x <= a < y), bounds[-1][0] if bounds else "")
        empty.append({"scene": sid, "start": round(a, 2), "end": round(b, 2), "seconds": round(b - a, 2)})
    return {"verified": True, "error": "", "empty": empty}


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
    """Seconds of cross-fade at each join (len = scenes - 1). "scene fade" sets it; otherwise every
    join holds the outgoing picture under the incoming one for PEN_JOIN_HOLD_S, because both pen
    scenes (first stroke) and motion scenes (fade-in) open faint."""
    d = FADE_S.get(mode or "")
    return [d or PEN_JOIN_HOLD_S for _ in range(len(compiled) - 1)]


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
    log("[explainer] checking that the screen is never empty...")
    report = visual_coverage(rough, compiled, fps, doc.theme.background, ffmpeg)
    try:
        os.makedirs(os.path.join(folder, "out"), exist_ok=True)
        with open(os.path.join(folder, "out", "visual_coverage.json"), "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1)
    except OSError:
        pass
    if not report["verified"]:
        log(f"[explainer] WARNING visual coverage NOT VERIFIED: {report['error']}")
    elif report["empty"]:
        for r in report["empty"]:
            m, sec = divmod(r["start"], 60)
            log(f"[explainer] WARNING empty screen {int(m)}:{sec:04.1f} for {r['seconds']:.1f} s "
                f"(scene '{r['scene']}')")
    else:
        log("[explainer] visual coverage: no empty screen anywhere in the video")
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
