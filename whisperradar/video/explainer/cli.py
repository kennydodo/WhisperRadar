"""Explainer CLI: validate / storyboard / render-scene (repo CLI conventions).

    python -m whisperradar.video.explainer.cli validate <folder>
    python -m whisperradar.video.explainer.cli storyboard <folder> [--out dir]
    python -m whisperradar.video.explainer.cli render-scene <folder> <scene_id>
    python -m whisperradar.video.explainer.cli icons <word> [<word> ...]
    python -m whisperradar.video.explainer.cli build <folder>      (plan.json -> explainer.json)
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


def cmd_build(folder: str) -> int:
    """plan.json + subtitles.srt -> explainer.json (then validated like any other)."""
    import json
    from . import recipes
    plan_path = os.path.join(folder, "plan.json")
    if not os.path.isfile(plan_path):
        raise ExplainerError([f"no plan.json in {folder}"])
    srt = _find_srt(folder)
    if srt is None:
        raise ExplainerError([f"no .srt file in {folder} (cue anchoring needs it)"])
    from whisperradar.video import parsing
    with open(plan_path, encoding="utf-8-sig") as fh:
        try:
            plan = json.load(fh)
        except json.JSONDecodeError as e:
            raise ExplainerError([f"plan.json is not valid JSON: {e}"]) from None
    with open(srt, encoding="utf-8-sig") as fh:
        cues = parsing.parse_srt(fh.read())
    doc = recipes.build(plan, cues)
    out = os.path.join(folder, "explainer.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")
    print(f"built explainer.json: {len(doc['scenes'])} scene(s), "
          f"{sum(len(sc_['objects']) for sc_ in doc['scenes'])} objects")
    return cmd_validate(folder)


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


def cmd_render_demo(folder: str, preview: bool = True, draft: bool = False) -> int:
    """Full assembly: build from plan.json when stale, render every scene,
    join them and add the narration (same code as the Studio's Render)."""
    from . import render
    print(render.render_video(folder, final=not preview, draft=draft))
    return 0


def cmd_icons(words, limit) -> int:
    """Find "lucide:<name>" icons by name or tag."""
    from . import strokes_library as lib
    query = " ".join(words)
    if not query.strip():
        print(f"{len(lib.lucide_names())} lucide icons. "
              "Search: cli icons <word> (e.g. 'server', 'money', 'lock').")
        return 0
    hits = lib.lucide_search(query, limit)
    if not hits:
        print(f"no icon matches {query!r}")
        return 1
    for name, tags in hits:
        extra = f"   [{', '.join(tags[:6])}]" if tags else ""
        print(f"lucide:{name}{extra}")
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
    p4.add_argument("--draft", action="store_true",
                    help="draw at half size (own cache) into out/preview.mp4")
    p6 = sub.add_parser("build", help="plan.json + subtitles.srt -> explainer.json")
    p6.add_argument("folder")
    p5 = sub.add_parser("icons", help="search the lucide icon library")
    p5.add_argument("words", nargs="*")
    p5.add_argument("--limit", type=int, default=15)
    ns = ap.parse_args(argv)
    try:
        if ns.cmd == "icons":
            return cmd_icons(ns.words, ns.limit)
        if ns.cmd == "build":
            return cmd_build(ns.folder)
        if ns.cmd == "validate":
            return cmd_validate(ns.folder)
        if ns.cmd == "storyboard":
            return cmd_storyboard(ns.folder, ns.out)
        if ns.cmd == "render-scene":
            return cmd_render_scene(ns.folder, ns.scene_id)
        if ns.cmd == "render-demo":
            return cmd_render_demo(ns.folder, preview=not ns.final, draft=ns.draft)
    except ExplainerError as e:
        for err in e.errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
