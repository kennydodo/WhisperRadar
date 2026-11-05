"""Scene preparation: explainer.json scenes -> silent frame-exact clips.

Prepared clips are opaque, complete-frame, silent H.264 in out/explainer/.
Temp file then atomic rename; a cancelled/failed render is never reusable.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading

from . import camera as cam_mod
from . import compose, pipe as pipe_mod, plates as plates_mod
from . import schema as sc
from .layout import layout_scene
from .timing import CompiledScene
from .validate import ExplainerError


def cache_dir(project_folder: str) -> str:
    return os.path.join(project_folder, "out", "explainer")


def _file_sha(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return "missing"


def scene_fingerprint(compiled: CompiledScene, doc, fps: float,
                      width: int, height: int, project_folder: str = "") -> str:
    """Content hash of everything that changes pixels or timing: schema
    version, resolved scene data, the COMPILED timing (SRT-dependent), theme
    values, font file contents, image asset contents, fps, dimensions.

    A filename is not evidence of content."""
    timing_data = {
        "start": compiled.start_frame, "end": compiled.end_frame,
        "events": [{"target": c.event.target, "action": c.event.action,
                    "start": c.start_frame, "end": c.end_frame}
                   for c in compiled.events]}
    fonts = {}
    for p in (doc.theme.heading_font, doc.theme.body_font, doc.theme.mono_font):
        if p:
            fonts[p] = _file_sha(p)
    assets = {}
    for obj in compiled.spec.objects:
        if obj.type == sc.IMAGE and obj.asset and project_folder:
            assets[obj.asset] = _file_sha(
                os.path.join(project_folder, obj.asset))
    body = {"v": 4, "scene": compiled.spec.to_dict(), "timing": timing_data,
            "theme": doc.theme.to_dict(), "fonts": fonts, "assets": assets,
            "fps": fps, "w": width, "h": height}
    chain = _base_chain(compiled, getattr(compiled, "_scenes", None))
    if chain:                                    # what the base scenes drew is part of this picture
        body["bases"] = [{"scene": b.spec.to_dict(),
                          "timing": [[c.event.target, c.event.action, c.start_frame, c.end_frame]
                                     for c in b.events], "end": b.end_frame - b.start_frame}
                         for b in chain]
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _base_chain(compiled, scenes):
    """Earlier scenes this one starts from, nearest first (a base can itself have a base)."""
    out, cur, seen = [], compiled, set()
    while getattr(cur.spec, "base", "") and scenes:
        b = scenes.get(cur.spec.base)
        if b is None or b.spec.id in seen:
            break
        seen.add(b.spec.id)
        out.append(b)
        cur = b
    return out


def render_scene_clip(doc, compiled: CompiledScene, ffmpeg_path: str,
                      project_folder: str, fps: float,
                      cancel_event: threading.Event = None,
                      crf: int = 20,
                      canvas: tuple = None) -> str:
    """Render one scene to a silent clip; returns the clip path.

    canvas: explicit (width, height) override; default = doc.canvas. The
    fingerprint includes the canvas so both resolutions cache separately.
    Raises ExplainerError on invalid input, RuntimeError on ffmpeg failure,
    PipeCancelled on cancel.
    """
    if getattr(compiled.spec, "remotion", None):
        from . import remotion_backend
        return remotion_backend.render_scene_clip(doc, compiled, project_folder, fps,
                                                  cancel_event, canvas, ffmpeg_path=ffmpeg_path)
    W, H = canvas or doc.canvas
    laid = layout_scene(compiled.spec, doc.theme, (W, H),
                        project_folder=project_folder)
    out_dir = cache_dir(project_folder)
    os.makedirs(out_dir, exist_ok=True)

    fp = scene_fingerprint(compiled, doc, fps, W, H, project_folder)
    final_path = os.path.join(out_dir, f"{compiled.spec.id}_{fp}.mp4")
    if os.path.isfile(final_path):
        _verify(final_path, compiled, fps, W, H)
        return final_path

    temp_path = os.path.join(out_dir, f".tmp_{compiled.spec.id}_{fp}.mp4")
    if os.path.exists(temp_path):
        try:
            os.remove(temp_path)
        except OSError:
            pass

    if getattr(compiled.spec, "camera", None) or getattr(compiled.spec, "base", ""):
        _render_pipe_camera(doc, compiled, temp_path, ffmpeg_path, fps, crf,
                            cancel_event, (W, H), project_folder)
    elif compose.needs_pipe(compiled):
        scene_obj = compiled
        scene_obj._project_folder = project_folder   # stroke assets resolve here
        _render_pipe(doc, compiled, laid, temp_path, ffmpeg_path, fps, crf,
                     cancel_event, (W, H), project_folder)
    else:
        _render_filtergraph(doc, compiled, laid, temp_path, ffmpeg_path, fps,
                            crf, cancel_event, (W, H), project_folder)

    if cancel_event is not None and cancel_event.is_set():
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise pipe_mod.PipeCancelled("Scene render cancelled.")
    _verify(temp_path, compiled, fps, W, H)
    os.replace(temp_path, final_path)
    return final_path


def _verify(path: str, compiled: CompiledScene, fps: float, w: int, h: int):
    """ffprobe the prepared clip: decodable, right size, right frame count,
    right fps, no audio. The cache is content-verified, not trusted."""
    import subprocess
    args = ["ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,nb_frames,r_frame_rate",
            "-of", "json", path]
    proc = subprocess.run(args, capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        raise RuntimeError(f"prepared scene {compiled.spec.id} is not decodable: "
                           f"{proc.stderr[-300:]}")
    import json as _json
    data = _json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError(f"prepared scene {compiled.spec.id} has no video stream.")
    st = streams[0]
    if int(st.get("width", 0)) != w or int(st.get("height", 0)) != h:
        raise RuntimeError(
            f"prepared scene {compiled.spec.id} is {st.get('width')}x"
            f"{st.get('height')}, expected {w}x{h}.")
    expected = compiled.duration_frames
    nb = int(st.get("nb_frames") or 0)
    if nb != expected:
        raise RuntimeError(
            f"prepared scene {compiled.spec.id} has {nb} frames, expected "
            f"{expected} - refusing to cache a wrong-length clip.")
    try:
        num, _, den = (st.get("r_frame_rate") or "0/1").partition("/")
        rate = float(num) / max(1.0, float(den or 1))
    except (ValueError, ZeroDivisionError):
        rate = 0.0
    if abs(rate - fps) > 0.01:
        raise RuntimeError(
            f"prepared scene {compiled.spec.id} is {rate:g} fps, expected "
            f"{fps:g}.")
    args_a = ["ffprobe", "-v", "error", "-select_streams", "a",
              "-show_entries", "stream=index", "-of", "csv=p=0", path]
    proc_a = subprocess.run(args_a, capture_output=True, text=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc_a.returncode == 0 and proc_a.stdout.strip():
        raise RuntimeError(f"prepared scene {compiled.spec.id} unexpectedly "
                           "contains audio.")


def _render_filtergraph(doc, compiled: CompiledScene, laid, out_path: str,
                        ffmpeg_path: str, fps: float, crf: int,
                        cancel_event, canvas: tuple = None,
                        project_folder: str = ""):
    canvas = canvas or doc.canvas
    plate_dir = os.path.join(cache_dir(_project_of(out_path)), "plates")
    plate_map = plates_mod.render_scene_plates(laid, canvas, plate_dir,
                                               doc.theme, project_folder)
    inputs, graph = compose.build_filtergraph_scene(
        compiled, laid, plate_map, doc.theme, canvas, fps,
        doc.theme.background)
    # -t 1 caps the lavfi color source; the color source never ends on its
    # own, the frame cap plus the output -frames:v stop the encode.
    args = [ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y"]
    args += inputs
    if graph is None:
        args += ["-frames:v", str(compiled.duration_frames)]
    else:
        args += ["-filter_complex", graph, "-map", "[vout]",
                 "-frames:v", str(compiled.duration_frames)]
    args += ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
             "-bf", "0", "-pix_fmt", "yuv420p", *pipe_mod.encode_threads(),
             out_path]
    import subprocess
    proc = subprocess.run(args, capture_output=True, text=True,
                          creationflags=pipe_mod.process_flags())
    if cancel_event is not None and cancel_event.is_set():
        raise pipe_mod.PipeCancelled("Scene render cancelled.")
    if proc.returncode != 0:
        raise RuntimeError(f"scene {compiled.spec.id} filtergraph render failed "
                           f"(exit {proc.returncode}): {proc.stderr[-400:]}")


def _render_pipe(doc, compiled: CompiledScene, laid, out_path: str,
                 ffmpeg_path: str, fps: float, crf: int, cancel_event,
                 canvas: tuple = None, project_folder: str = ""):
    from .evaluator import evaluate
    from PIL import Image
    W, H = canvas or doc.canvas
    bg_rgb = tuple(int(doc.theme.background.lstrip("#")[k:k + 2], 16)
                   for k in (0, 2, 4))
    plate_dir = os.path.join(cache_dir(_project_of(out_path)), "plates")
    plate_map = plates_mod.render_scene_plates(laid, (W, H), plate_dir,
                                               doc.theme, project_folder)
    loaded = {}
    for oid, (paths, kind, origin) in plate_map.items():
        loaded[oid] = ([Image.open(p).convert("RGBA") for p in paths], origin)
    with pipe_mod.FramePipe(ffmpeg_path, W, H, fps, out_path, crf) as fpw:
        for frame in range(compiled.duration_frames):
            if cancel_event is not None and cancel_event.is_set():
                fpw.close(cancel_event)   # raises PipeCancelled
                return
            img = Image.new("RGB", (W, H), bg_rgb)
            states = evaluate(compiled, laid, frame)
            pipe_mod.draw_scene_frame(img, compiled, laid, states, doc.theme)
            # plates on top (text/label/callout/image), alpha by state
            for lo in laid.objects:
                spec = lo.spec
                if spec.id not in loaded:
                    continue
                st = states.get(spec.id)
                if st is None or st.gone or st.alpha <= 0 or not st.visible:
                    continue
                imgs, origin = loaded[spec.id]
                evs = [c for c in compiled.events
                       if c.event.target == spec.id
                       and c.event.action == sc.TYPE_ON]
                if evs and len(imgs) > 1:
                    p = min(1.0, (frame - evs[0].start_frame)
                            / max(1, evs[0].duration_frames))
                    idx = min(len(imgs) - 1,
                              int(p * (len(imgs) - 1) + 0.0001)
                              if evs[0].duration_frames > 0 else len(imgs) - 1)
                    plate = imgs[idx]
                else:
                    plate = imgs[-1]
                if st.alpha < 1.0:
                    plate = _apply_alpha(plate, st.alpha)
                # origin + slide offset: text plates are full-canvas (origin
                # 0,0), callout/image plates live at their box position
                pos = (origin[0] + int(st.dx), origin[1] + int(st.dy))
                img.paste(plate, pos, plate)
            fpw.write(img)
        fpw.close(cancel_event)


def _paint(img, theme, compiled, laid, loaded, frame):
    """One frame of a scene onto img: strokes and shapes, then the text plates."""
    from .evaluator import evaluate
    states = evaluate(compiled, laid, frame)
    pipe_mod.draw_scene_frame(img, compiled, laid, states, theme)
    for lo in laid.objects:
        spec = lo.spec
        if spec.id not in loaded:
            continue
        st = states.get(spec.id)
        if st is None or st.gone or st.alpha <= 0 or not st.visible:
            continue
        imgs, origin = loaded[spec.id]
        evs = [c for c in compiled.events
               if c.event.target == spec.id and c.event.action == sc.TYPE_ON]
        if evs and len(imgs) > 1:
            p = min(1.0, (frame - evs[0].start_frame) / max(1, evs[0].duration_frames))
            idx = (min(len(imgs) - 1, int(p * (len(imgs) - 1) + 0.0001))
                   if evs[0].duration_frames > 0 else len(imgs) - 1)
            plate = imgs[idx]
        else:
            plate = imgs[-1]
        if st.alpha < 1.0:
            plate = _apply_alpha(plate, st.alpha)
        img.paste(plate, (origin[0] + int(st.dx), origin[1] + int(st.dy)), plate)


def _load_plates(doc, scene, laid, canvas, plate_dir, project_folder):
    from PIL import Image
    plate_map = plates_mod.render_scene_plates(laid, canvas, plate_dir, doc.theme, project_folder)
    return {oid: ([Image.open(p).convert("RGBA") for p in paths], origin)
            for oid, (paths, kind, origin) in plate_map.items()}


def _base_image(doc, theme, scenes, base_id, canvas, plate_dir, project_folder, bg_rgb, _seen=None):
    """The finished drawing of an earlier scene (and of the scene it started from) at `canvas`."""
    from PIL import Image
    seen = set(_seen or ())
    b = scenes.get(base_id)
    if b is None or base_id in seen:
        raise ExplainerError([f"base scene {base_id!r} is not available."])
    seen.add(base_id)
    if getattr(b.spec, "remotion", None):
        raise ExplainerError([f"base scene {base_id!r} is a Remotion scene; it cannot be a base."])
    if b.spec.base:
        img = _base_image(doc, theme, scenes, b.spec.base, canvas, plate_dir, project_folder, bg_rgb, seen)
    else:
        img = Image.new("RGB", canvas, bg_rgb)
    laid = layout_scene(b, theme, canvas, project_folder=project_folder)
    b._project_folder = project_folder
    loaded = _load_plates(doc, b, laid, canvas, plate_dir, project_folder)
    _paint(img, theme, b, laid, loaded, max(0, b.duration_frames - 1))
    return img


def _render_pipe_camera(doc, compiled, out_path, ffmpeg_path, fps, crf, cancel_event,
                        canvas, project_folder):
    """Scenes with a camera and/or a base scene: drawn on a (super-sampled) world image, then cropped."""
    import dataclasses
    from PIL import Image
    W, H = canvas
    cam = compiled.spec.camera
    k = cam_mod.supersample(cam)
    Wk, Hk = W * k, H * k
    zoom_end = cam_mod._view(cam.get("to") or cam_mod.OVERVIEW)["zoom"] if cam else 1.0
    theme = dataclasses.replace(doc.theme, stroke_width=max(1, int(round(doc.theme.stroke_width / zoom_end))))
    bg_rgb = tuple(int(doc.theme.background.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    plate_dir = os.path.join(cache_dir(_project_of(out_path)), "plates")
    scenes = getattr(compiled, "_scenes", None) or {}
    laid = layout_scene(compiled, theme, (Wk, Hk), project_folder=project_folder)
    compiled._project_folder = project_folder
    loaded = _load_plates(doc, compiled, laid, (Wk, Hk), plate_dir, project_folder)
    base = None
    if compiled.spec.base:
        base = _base_image(doc, theme, scenes, compiled.spec.base, (Wk, Hk), plate_dir, project_folder, bg_rgb)
    with pipe_mod.FramePipe(ffmpeg_path, W, H, fps, out_path, crf) as fpw:
        for frame in range(compiled.duration_frames):
            if cancel_event is not None and cancel_event.is_set():
                fpw.close(cancel_event)
                return
            img = base.copy() if base is not None else Image.new("RGB", (Wk, Hk), bg_rgb)
            _paint(img, theme, compiled, laid, loaded, frame)
            if cam:
                img = cam_mod.crop(img, cam_mod.view_at(cam, frame, fps), (W, H))
            elif k != 1:
                img = img.resize((W, H), Image.LANCZOS)
            fpw.write(img)
        fpw.close(cancel_event)


def _apply_alpha(plate: Image.Image, alpha: float) -> Image.Image:
    img = plate.copy()
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    a = img.getchannel("A").point(lambda v: int(v * alpha))
    img.putalpha(a)
    return img


def _project_of(out_path: str) -> str:
    """out/explainer/<file>.mp4 -> project folder (two levels up from explainer)."""
    explainer_dir = os.path.dirname(os.path.abspath(out_path))
    return os.path.dirname(os.path.dirname(explainer_dir))


def _secs(frames: int, fps: float) -> str:
    s = f"{frames / fps:.6f}".rstrip("0").rstrip(".")
    return s or "0"
