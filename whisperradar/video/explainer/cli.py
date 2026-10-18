"""Explainer CLI: validate / storyboard / render-scene (repo CLI conventions).

    python -m whisperradar.video.explainer.cli validate <folder>
    python -m whisperradar.video.explainer.cli storyboard <folder> [--out dir]
    python -m whisperradar.video.explainer.cli render-scene <folder> <scene_id>
"""
from __future__ import annotations

import argparse
import os
import sys

from . import prepare as prep
from . import schema as sc
from . import validate as validate_mod
from .layout import layout_scene
from .plates import render_scene_plates
from .timing import compile_doc
from .validate import ExplainerError


def _load_context(folder: str):
    doc_path = os.path.join(folder, "explainer.json")
    if not os.path.isfile(doc_path):
        raise ExplainerError([f"no explainer.json in {folder}"])
    doc = validate_mod.load(doc_path, project_folder=folder)
    srt = _find_srt(folder)
    if srt is None:
        raise ExplainerError([f"no .srt file in {folder} (cue anchoring needs it)"])
    from whisperradar.video import parsing
    with open(srt, encoding="utf-8-sig") as fh:
        cues = parsing.parse_srt(fh.read())
    fps = _fps_for(folder, doc)
    audio = _find_audio(folder)
    total = None
    if audio:
        from whisperradar.video.ffprobe import ffprobe_duration_seconds
        total = ffprobe_duration_seconds(audio)
    return doc, cues, fps, total


def _find_audio(folder: str):
    audio_dir = os.path.join(folder, "audio")
    if os.path.isdir(audio_dir):
        for name in sorted(os.listdir(audio_dir)):
            if name.startswith("narration."):
                return os.path.join(audio_dir, name)
    for name in sorted(os.listdir(folder)):
        if name.lower().startswith("audio.") or name.lower().endswith(
                (".mp3", ".wav", ".m4a", ".flac", ".ogg")):
            return os.path.join(folder, name)
    return None


def _find_srt(folder: str):
    for name in sorted(os.listdir(folder)):
        if name.lower().endswith(".srt"):
            return os.path.join(folder, name)
    return None


def _fps_for(folder: str, doc) -> float:
    """fps from the production's imgtovideo.json when present, else 30."""
    from whisperradar.video import options as options_mod
    opts = options_mod.load_or_default(os.path.join(folder, "imgtovideo.json"))
    return float(opts.output.fps)


def cmd_validate(folder: str) -> int:
    doc, cues, fps, total = _load_context(folder)
    compiled = compile_doc(doc, cues, fps, total_seconds=total)
    conflicts = validate_mod.event_property_conflicts(doc)
    if conflicts:
        raise ExplainerError(conflicts)
    print(f"OK: {len(doc.scenes)} scene(s), {len(cues)} cues, {fps:g} fps"
          + (f", audio {total:.2f}s" if total else ""))
    for cs in compiled:
        print(f"  scene {cs.spec.id}: frames [{cs.start_frame}, {cs.end_frame}) "
              f"({cs.duration_frames} frames), {len(cs.events)} event(s)")
    return 0


def cmd_storyboard(folder: str, out_dir: str = "") -> int:
    doc, cues, fps, total = _load_context(folder)
    compiled = compile_doc(doc, cues, fps, total_seconds=total)
    out_dir = out_dir or os.path.join(folder, "out", "explainer", "storyboard")
    os.makedirs(out_dir, exist_ok=True)
    from PIL import Image
    for cs in compiled:
        laid = layout_scene(cs.spec, doc.theme, doc.canvas,
                            project_folder=folder)
        plates_dir = os.path.join(out_dir, "plates")
        plate_map = render_scene_plates(laid, doc.canvas, plates_dir, doc.theme,
                                        folder)
        from .evaluator import evaluate
        from . import pipe as pipe_mod
        bg = tuple(int(doc.theme.background.lstrip("#")[k:k + 2], 16)
                   for k in (0, 2, 4))
        # final hold state per scene (last frame) + early/mid snapshots
        for tag, frac in (("start", 0.05), ("mid", 0.55), ("final", 0.999)):
            frame = int(cs.duration_frames * frac)
            img = Image.new("RGB", doc.canvas, bg)
            states = evaluate(cs, laid, frame)
            pipe_mod.draw_scene_frame(img, cs, laid, states, doc.theme)
            for lo in laid.objects:
                entry = plate_map.get(lo.spec.id)
                if not entry:
                    continue
                paths, _kind, origin = entry
                st = states.get(lo.spec.id)
                if st is None or not st.visible or st.alpha <= 0:
                    continue
                plate = Image.open(paths[-1]).convert("RGBA")
                pos = (origin[0] + int(st.dx), origin[1] + int(st.dy))
                img.paste(plate, pos, plate)
            small = img.resize((doc.canvas[0] // 2, doc.canvas[1] // 2))
            path = os.path.join(out_dir, f"{cs.spec.id}_{tag}.png")
            small.save(path)
            print(f"storyboard: {path}")
    return 0


def cmd_render_scene(folder: str, scene_id: str) -> int:
    doc, cues, fps, total = _load_context(folder)
    compiled = compile_doc(doc, cues, fps, total_seconds=total)
    match = [c for c in compiled if c.spec.id == scene_id]
    if not match:
        raise ExplainerError([f"no scene \"{scene_id}\" in explainer.json"])
    from whisperradar.video import tool_locator
    ffmpeg = tool_locator.resolve("ffmpeg", "ffmpeg")
    path = prep.render_scene_clip(doc, match[0], ffmpeg, folder, fps,
                                  canvas=doc.canvas)
    print(path)
    return 0


def cmd_render_demo(folder: str, preview: bool = True) -> int:
    """Full demo assembly: prepare every scene, concat, mux narration."""
    doc, cues, fps, total = _load_context(folder)
    compiled = compile_doc(doc, cues, fps, total_seconds=total)
    conflicts = validate_mod.event_property_conflicts(doc)
    if conflicts:
        raise ExplainerError(conflicts)
    from whisperradar.video import tool_locator
    ffmpeg = tool_locator.resolve("ffmpeg", "ffmpeg")
    clips = []
    for cs in compiled:
        clips.append(prep.render_scene_clip(doc, cs, ffmpeg, folder, fps))
    concat_path = os.path.join(prep.cache_dir(folder), "concat.txt")
    with open(concat_path, "w", encoding="utf-8", newline="") as fh:
        fh.write("ffconcat version 1.0\n")
        for c in clips:
            fh.write("file '" + os.path.abspath(c).replace("'", "'\\''") + "'\n")
    rough = os.path.join(prep.cache_dir(folder), "rough.mp4")
    import subprocess
    # Re-encode on concat: stream-copy can round timestamps at each boundary
    # and silently drop frames; the assembled clip must be frame-exact.
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
            "-safe", "0", "-i", concat_path, "-c:v", "libx264", "-preset",
            "veryfast", "-crf", "18", "-bf", "0", "-pix_fmt", "yuv420p", rough]
    proc = subprocess.run(args, capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        raise RuntimeError(f"concat failed (exit {proc.returncode}): {proc.stderr[-300:]}")
    audio = _find_audio(folder)
    if not audio:
        print(rough)
        return 0
    out_name = "preview.mp4" if preview else "final.mp4"
    out_path = os.path.join(folder, "out", out_name)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", rough,
            "-i", audio, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-shortest", out_path]
    proc = subprocess.run(args, capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        raise RuntimeError(f"mux failed (exit {proc.returncode}): {proc.stderr[-300:]}")
    print(out_path)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="explainer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("validate")
    p1.add_argument("folder")
    p2 = sub.add_parser("storyboard")
    p2.add_argument("folder")
    p2.add_argument("--out", default="")
    p3 = sub.add_parser("render-scene")
    p3.add_argument("folder")
    p3.add_argument("scene_id")
    p4 = sub.add_parser("render-demo")
    p4.add_argument("folder")
    p4.add_argument("--final", action="store_true",
                    help="write out/final.mp4 instead of out/preview.mp4")
    ns = ap.parse_args(argv)
    try:
        if ns.cmd == "validate":
            return cmd_validate(ns.folder)
        if ns.cmd == "storyboard":
            return cmd_storyboard(ns.folder, ns.out)
        if ns.cmd == "render-scene":
            return cmd_render_scene(ns.folder, ns.scene_id)
        if ns.cmd == "render-demo":
            return cmd_render_demo(ns.folder, preview=not ns.final)
    except ExplainerError as e:
        for err in e.errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
