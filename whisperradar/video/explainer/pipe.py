"""Frame-pipe backend: deterministic per-frame rendering piped to ffmpeg.

Used ONLY for effects the filtergraph cannot express (true arc-length path
tracing). Bounded buffering, drained stderr, clean cancellation - the rules
from the handoff's rendering-architecture section.
"""
from __future__ import annotations

import subprocess
import threading
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter

from . import schema as sc
from .timing import CompiledScene
from .evaluator import formatter
from .strokes import Stroke
from . import strokes_library


class PipeCancelled(Exception):
    pass


class FramePipe:
    """Stream raw RGB frames into an ffmpeg process (rawvideo -> h264)."""

    def __init__(self, ffmpeg_path: str, width: int, height: int, fps: float,
                 output_path: str, crf: int = 20):
        args = [
            ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{width}x{height}", "-r", _f(fps), "-i", "pipe:0",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-bf", "0", "-pix_fmt", "yuv420p", output_path]
        self.width, self.height = width, height
        self._proc: Optional[subprocess.Popen] = None
        self._args = args
        self._stderr_tail: list = []

    def __enter__(self):
        self._proc = subprocess.Popen(
            self._args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE)
        self._drain = threading.Thread(target=self._pump_stderr, daemon=True)
        self._drain.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def _pump_stderr(self):
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                self._stderr_tail.append(line)
                if len(self._stderr_tail) > 40:
                    self._stderr_tail.pop(0)
        except (OSError, ValueError):
            pass

    def write(self, img: Image.Image):
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("FramePipe is not open.")
        if img.mode != "RGB":
            img = img.convert("RGB")
        if img.size != (self.width, self.height):
            img = img.resize((self.width, self.height), Image.BILINEAR)
        try:
            proc.stdin.write(img.tobytes())
        except (OSError, ValueError) as e:
            raise RuntimeError(
                "ffmpeg closed the frame pipe: " + self.error_tail()) from e

    def close(self, cancel_event: Optional[threading.Event] = None):
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        while True:
            try:
                proc.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if cancel_event is not None and cancel_event.is_set():
                    proc.kill()
                    proc.wait()
                    self._proc = None
                    raise PipeCancelled("Frame render cancelled.")
        self._drain.join(timeout=5)
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg frame-pipe encode failed (exit {proc.returncode}): "
                + self.error_tail())
        self._proc = None

    def error_tail(self) -> str:
        return "\n".join(self._stderr_tail[-10:]) or "(no stderr)"


def _f(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s or "0"


# ---- deterministic path geometry -------------------------------------------------

def path_length(points) -> float:
    total = 0.0
    for i in range(1, len(points)):
        dx = points[i][0] - points[i - 1][0]
        dy = points[i][1] - points[i - 1][1]
        total += (dx * dx + dy * dy) ** 0.5
    return total


def partial_path(points, progress: float):
    """Points along the polyline for arc-length fraction progress (0..1)."""
    if progress >= 1.0:
        return list(points)
    total = path_length(points)
    if total <= 0:
        return points[:1]
    target = total * max(0.0, min(1.0, progress))
    out = [points[0]]
    walked = 0.0
    for i in range(1, len(points)):
        seg_x = points[i][0] - points[i - 1][0]
        seg_y = points[i][1] - points[i - 1][1]
        seg = (seg_x * seg_x + seg_y * seg_y) ** 0.5
        if walked + seg >= target:
            t = 0.0 if seg == 0 else (target - walked) / seg
            out.append((points[i - 1][0] + seg_x * t,
                        points[i - 1][1] + seg_y * t))
            return out
        walked += seg
        out.append(points[i])
    return out


def draw_scene_frame(img: Image.Image, scene: CompiledScene, laid_out,
                     states: dict, theme, canvas_px: tuple = None) -> None:
    """Paint one frame: background + plates/paths/strokes per state. Draw
    calls are ordered by object list order (deterministic)."""
    draw = ImageDraw.Draw(img, "RGBA")
    lw = theme.stroke_width
    W, H = canvas_px or img.size
    for lo in laid_out.objects:
        st = states.get(lo.spec.id)
        if st is None or st.gone or (not st.visible and st.alpha <= 0):
            continue
        spec = lo.spec
        x, y, w, h = lo.box_px
        alpha = int(round(255 * max(0.0, min(1.0, st.alpha))))
        if spec.type in (sc.STROKE_ASSET, sc.HANDWRITING, sc.SHAPE):
            _draw_stroke_object(img, draw, scene, lo, st, theme, alpha, (W, H))
        elif spec.type == sc.PATH:
            if st.progress <= 0:
                continue
            color = _role_color(spec.style, theme)
            pts = _absolute_path(spec, img, st.progress)
            draw.line(pts, fill=_with_alpha(color, alpha), width=lw,
                      joint="curve")
            if spec.arrow and st.progress >= 1.0:
                _arrow_head(draw, pts, _with_alpha(color, alpha), lw)
        elif spec.type == sc.BAR:
            if st.progress <= 0:
                continue
            color = _role_color(spec.style, theme)
            pad = max(2, lw)
            draw.rectangle([x, y, x + w, y + h],
                           outline=_with_alpha(theme.muted, alpha), width=max(1, lw // 2))
            fill_w = int((w - 2 * pad) * _clamp01(st.progress) * spec.fraction)
            if fill_w > 0:
                draw.rectangle(
                    [x + pad, y + pad, x + pad + fill_w, y + h - pad],
                    fill=_with_alpha(color, alpha))
        elif spec.type == sc.GRID:
            _draw_grid(draw, spec, lo, st, theme, alpha,
                       _role_color(spec.style, theme))
        elif spec.type == sc.BLOCKS:
            _draw_blocks(draw, spec, lo, st, theme, alpha)
        elif spec.type == sc.COUNTER:
            if not st.visible:
                continue
            text = formatter(spec.format, st.value, spec.decimals)
            color = _role_color(spec.style, theme)
            _draw_counter(draw, lo, text, theme, alpha, color, st.emphasized)
        # TEXT/LABEL/CALLOUT/IMAGE are composited by the caller (plate overlay)


def _absolute_path(spec, img, progress: float):
    scaled = [(px * img.width, py * img.height) for px, py in (spec.points or [])]
    return [(int(round(a)), int(round(b))) for a, b in partial_path(scaled, progress)]


# ---- whiteboard stroke objects ------------------------------------------------------

def _scene_project(scene):
    return getattr(scene, "_project_folder", "") or ""


def _draw_stroke_object(img, draw, scene, lo, st, theme, alpha, canvas_px):
    """stroke_asset / handwriting: ordered strokes drawn by arc length,
    erase reverses them, pen nib shows while drawing (doc 16.3/16.5/16.7)."""
    spec = lo.spec
    W, H = canvas_px
    if st.dim > 0:
        alpha = int(round(alpha * (1.0 - st.dim)))
    if spec.type == sc.SHAPE:
        strokes = _shape_strokes(spec, lo, (W, H), theme, float(getattr(st, "scale", 1.0)))
        alpha = int(round(alpha * spec.opacity))
    elif spec.type == sc.HANDWRITING and _hand_font(spec, theme, scene):
        _write_on(img, draw, spec, lo, st, theme, alpha, _hand_font(spec, theme, scene))
        return
    elif spec.type == sc.HANDWRITING:
        if getattr(spec, "fallback_typed", False) and getattr(
                spec, "_missing_glyphs", None):
            return   # typed fallback renders as a label plate, not strokes
        strokes = _handwriting_strokes(spec, lo, (W, H))
    else:
        try:
            asset = strokes_library.load(spec.asset, _scene_project(scene))
        except ValueError as e:
            raise RuntimeError(f"object {spec.id}: {e}") from None
        x, y, w, h = lo.box_px
        scale = min(w / asset.viewBox[0], h / asset.viewBox[1])
        strokes = _scale_strokes(asset.strokes, scale, x, y)

    erase = float(st.erase_progress)
    draw_progress = float(st.progress) * (1.0 - erase)
    if draw_progress <= 0 and st.alpha <= 0:
        return

    total = sum(s.length for s in strokes) or 1.0
    lw_scale = max(1.0, W / 1920.0)
    if spec.type == sc.SHAPE:
        _shape_extras(img, draw, spec, strokes, theme, st, draw_progress,
                      lw_scale, alpha)
    ink = theme.foreground
    budget = total * _clamp01(draw_progress)
    spent = 0.0
    tip = None
    colours = {_stroke_color(st_, spec, theme) for st_ in strokes}
    # fading / dimmed / translucent ink is drawn flat, so stroke caps and joints never stack up
    translucent = ((spec.type == sc.SHAPE and spec.opacity < 1.0) or alpha < 250) \
        and len(colours) == 1 and not (spec.type == sc.STROKE_ASSET and spec.fill_after_draw)
    if translucent:                      # one flat mask, so overlaps never double-blend
        tmask = Image.new("RGBA", img.size, (0, 0, 0, 0))
        real_draw, draw = draw, ImageDraw.Draw(tmask)
        mask_alpha, alpha = alpha, 255
    for s in strokes:
        if spent >= budget:
            break
        sl = s.length or 0.01
        frac = min(1.0, (budget - spent) / sl)
        pts = [(int(round(a)), int(round(b))) for a, b in s.partial(frac)]
        color = _stroke_color(s, spec, theme)
        width = max(2, int(round(s.width * lw_scale)))
        if len(pts) >= 2:
            draw.line(pts, fill=_with_alpha(color, alpha), width=width,
                      joint="curve")
            r = max(1, width // 2)   # round caps/joins (16.3)
            for cap in (() if translucent and spec.type == sc.SHAPE and spec.opacity < 1.0
                        else (pts[0], pts[-1])):
                draw.ellipse([cap[0] - r, cap[1] - r, cap[0] + r, cap[1] + r],
                             fill=_with_alpha(color, alpha))
        if frac < 1.0:
            tip = pts[-1]
        spent += sl * frac
    if translucent:
        draw = real_draw
        tmask = tmask.getchannel("A").point(lambda v: v * mask_alpha // 255)
        img.paste(_hex_rgb(_stroke_color(strokes[0], spec, theme)), (0, 0), tmask)
    if (spec.type == sc.STROKE_ASSET and spec.fill_after_draw
            and draw_progress >= 1.0):
        _fill_stroke_silhouette(draw, strokes, theme, alpha)
    if st.drawing and tip and erase == 0.0 and spec.opacity >= 1.0:
        r = max(2, int(round(4 * lw_scale)))
        draw.ellipse([tip[0] - r, tip[1] - r, tip[0] + r, tip[1] + r],
                     fill=_with_alpha(theme.muted, alpha))


def _hand_font(spec, theme, scene):
    """Resolved .ttf path a handwriting object writes with, or "" for the
    built-in pen-stroke glyphs."""
    from .layout import find_font
    name = "" if spec.font == "strokes" else (spec.font or theme.hand_font)
    return find_font(name, _scene_project(scene)) if name else ""


_WRITE_CACHE: dict = {}


# how much of the old chalk thickening each text weight keeps, and the extra space
# between letters (a fraction of the font size) for each spacing choice
WEIGHT_FACTOR = {"": 0.0, "thin": 0.0, "normal": 0.5, "bold": 1.0}
TRACKING_EM = {"": 0.04, "normal": 0.04, "tight": 0.0, "wide": 0.09}


def _tracked_width(font, ln, track):
    return sum(font.getlength(ch) for ch in ln) + track * max(0, len(ln) - 1)


def text_width_px(font_path, text, box_px, weight, tracking=0.04) -> int:
    """Width in pixels of the widest line as _write_layer will write it in this box."""
    return int(_fit_text(font_path, text, box_px, weight, tracking)[5])


def _fit_text(font_path, text, box_px, weight, tracking):
    """(font, size, stroke, track, lines, widest) - the size the text is written at: as tall as the box
    allows, shrunk until the widest line fits the box width."""
    from PIL import ImageFont
    x, y, w, h = box_px
    lines = text.split("\n")
    probe = ImageFont.truetype(font_path, 100).getmetrics()
    ratio = max(1.2, (probe[0] + probe[1]) / 100.0)      # tall fonts need taller lines
    size = max(8, int(h / len(lines) / ratio))
    while True:
        font = ImageFont.truetype(font_path, size)
        sw = max(0, int(round(size * weight / 36.0)))
        track = size * tracking
        widths = [_tracked_width(font, ln, track) + 2 * sw for ln in lines]
        if max(widths) <= w or size <= 8:
            break
        size = int(size * 0.92)
    return font, size, sw, track, lines, max(widths)


def _write_layer(font_path, text, box_px, canvas_px, weight, tracking=0.04):
    """Greyscale mask of the whole text plus each line's x-extent: built once
    per (font, text, box) and reused for every frame of the write-on."""
    from PIL import ImageFont
    key = (font_path, text, box_px, canvas_px, weight, tracking)
    hit = _WRITE_CACHE.get(key)
    if hit is not None:
        return hit
    x, y, w, h = box_px
    font, size, sw, track, lines, _widest = _fit_text(font_path, text, box_px, weight, tracking)
    widths = [_tracked_width(font, ln, track) + 2 * sw for ln in lines]
    ascent, descent = font.getmetrics()
    lh = max(size * 1.2, ascent + descent + 2 * sw)   # tails of g/y/p must stay inside the line
    mask = Image.new("L", canvas_px, 0)
    md = ImageDraw.Draw(mask)
    extents = []
    for i, ln in enumerate(lines):
        top = y + i * lh
        cx = x + sw
        for ch in ln:                          # letter by letter so the spacing is ours
            md.text((cx, top), ch, font=font, fill=255, stroke_width=sw, stroke_fill=255)
            cx += font.getlength(ch) + track
        extents.append((x, x + widths[i], int(top) - sw, int(top + lh) + sw))
    if len(_WRITE_CACHE) > 64:
        _WRITE_CACHE.clear()
    _WRITE_CACHE[key] = (mask, extents)
    return _WRITE_CACHE[key]


def _write_on(img, draw, spec, lo, st, theme, alpha, font_path):
    """Real-font handwriting: the text is revealed left to right, line by
    line, with a pen nib at the writing edge (and rubbed out in reverse)."""
    W, H = img.size
    mask, extents = _write_layer(
        font_path, spec.text, lo.box_px, (W, H),
        theme.stroke_width / 5.0 * WEIGHT_FACTOR.get(getattr(theme, "text_weight", ""), 0.0),
        TRACKING_EM.get(getattr(theme, "letter_spacing", ""), 0.04))
    frac = _clamp01(float(st.progress) * (1.0 - float(st.erase_progress)))
    if frac <= 0 and st.alpha <= 0:
        return
    total = sum(e[1] - e[0] for e in extents) or 1.0
    budget = total * frac
    reveal = Image.new("L", (W, H), 0)
    rd = ImageDraw.Draw(reveal)
    tip = None
    for x0, x1, y0, y1 in extents:
        take = min(x1 - x0, budget)
        if take <= 0:
            break
        rd.rectangle([x0 - 2, y0, x0 + take, y1], fill=255)
        if take < x1 - x0:
            tip = (int(x0 + take), int((y0 + y1) / 2 + (y1 - y0) * 0.15))
        budget -= take
    from PIL import ImageChops
    vis = ImageChops.multiply(mask, reveal).point(lambda v: v * alpha // 255)
    color = _stroke_color(Stroke(points=[(0, 0), (1, 0)]), spec, theme)
    img.paste(_hex_rgb(color), (0, 0), vis)
    if st.drawing and tip and st.erase_progress == 0.0:
        r = max(2, int(round(4 * max(1.0, W / 1920.0))))
        draw.ellipse([tip[0] - r, tip[1] - r, tip[0] + r, tip[1] + r],
                     fill=_with_alpha(theme.muted, alpha))


def _shape_strokes(spec, lo, canvas_px, theme, scale=1.0):
    from . import shapes
    W, H = canvas_px
    pts = [(px * W, py * H) for px, py in (spec.points or [])]
    box = lo.box_px
    if scale != 1.0:                       # stretch: the left edge stays, the width follows
        box = (box[0], box[1], max(2, box[2] * scale), box[3])
    return shapes.shape_strokes(spec, box, pts, theme.stroke_width,
                                straight=getattr(theme, "line_style", "") == "straight")


def _shape_extras(img, draw, spec, strokes, theme, st, progress, lw_scale, alpha):
    """Card fill (drawn under the outline) and the glow of an emphasised shape."""
    color = sc.resolve_color(spec.color, theme) if spec.color else theme.foreground
    if spec.kind == "card" and spec.filled and strokes and progress > 0:
        poly = [(int(round(a)), int(round(b))) for a, b in strokes[0].points]
        bg = _hex_rgb(theme.background)
        inner = tuple(int(c * 0.45) for c in bg)
        a = int(alpha * 0.9 * _clamp01(progress * 2.5))
        draw.polygon(poly, fill=inner + (a,))
    if st.emphasized and progress > 0:
        mask = Image.new("L", img.size, 0)
        md = ImageDraw.Draw(mask)
        for s in strokes:
            pts = [(int(round(a)), int(round(b))) for a, b in s.points]
            if len(pts) >= 2:
                md.line(pts, fill=255, width=int(round(theme.stroke_width * lw_scale * 3)),
                        joint="curve")
        mask = mask.filter(ImageFilter.GaussianBlur(10 * lw_scale))
        mask = mask.point(lambda v: int(v * 0.85 * alpha / 255))
        img.paste(_hex_rgb(color), (0, 0), mask)


def _stroke_color(s: Stroke, spec, theme):
    if getattr(spec, "color", ""):
        resolved = sc.resolve_color(spec.color, theme)
        if resolved:
            return resolved
    style = spec.style if spec.style not in ("body", "heading", "label") else ""
    if style:
        return _role_color(style, theme)
    if s.color:
        return s.color
    return theme.foreground


def _scale_strokes(strokes, scale, ox, oy):
    out = []
    for s in strokes:
        pts = [(ox + px * scale, oy + py * scale) for px, py in s.points]
        out.append(Stroke(points=pts, width=s.width * scale, color=s.color,
                          fill=s.fill, weight=s.weight,
                          pause_after=s.pause_after))
    return out


def _handwriting_strokes(spec, lo, canvas_px):
    """Handwriting strokes in canvas space, wrapped to the object box.
    Marker width comes from the glyph scale (6.5 * scale), never from the
    wrap width."""
    from . import glyphs
    x, y, w, h = lo.box_px
    lines = spec.text.split("\n")
    # start from a scale that fits the box height per line, then shrink until
    # every line fits the box width
    scale = (h / max(1, len(lines))) / (120.0 * 1.15)
    for _ in range(24):
        too_wide = any(
            glyphs.text_strokes(line, (x, y), scale)[1][0] > w
            for line in lines)
        if not too_wide:
            break
        scale *= 0.82
    strokes = []
    y_cursor = y
    for line in lines:
        line_strokes, _adv = glyphs.text_strokes(line, (x, y_cursor), scale)
        strokes.extend(line_strokes)
        y_cursor += 120 * scale * 0.72
    return strokes


def _fill_stroke_silhouette(draw, strokes, theme, alpha):
    pts = []
    for s in strokes:
        pts.extend(s.points)
    if len(pts) >= 3:
        poly = [(int(round(a)), int(round(b))) for a, b in pts]
        rgb = _hex_rgb(theme.accent)
        draw.polygon(poly, fill=(rgb[0], rgb[1], rgb[2],
                                 int(40 * alpha / 255)))


def _draw_grid(draw, spec, lo, st, theme, alpha, color):
    x, y, w, h = lo.box_px
    cols, rows = spec.columns, spec.rows
    cw, ch = w / cols, h / rows
    shown = st.visible_count if st.visible_count < cols * rows else cols * rows
    for r in range(rows):
        for c in range(cols):
            if r * cols + c >= shown:
                break
            draw.rectangle(
                [x + c * cw, y + r * ch, x + (c + 1) * cw - 2, y + (r + 1) * ch - 2],
                outline=_with_alpha(color, alpha), width=max(1, theme.stroke_width // 2))


def _draw_blocks(draw, spec, lo, st, theme, alpha):
    x, y, w, h = lo.box_px
    cols, rows = spec.columns, spec.rows
    cw, ch = w / cols, h / rows
    for i in range(min(st.visible_count, spec.count)):
        r, c = divmod(i, cols)
        draw.rectangle(
            [x + c * cw, y + r * ch, x + (c + 1) * cw - 3, y + (r + 1) * ch - 3],
            fill=_with_alpha(theme.accent, alpha))


def _draw_counter(draw, lo, text, theme, alpha, color, emphasized):
    from PIL import ImageFont
    from .layout import find_font
    x, y, w, h = lo.box_px
    size = lo.text.font_size if lo.text else 64
    font_path = find_font(theme.mono_font)
    font = ImageFont.truetype(font_path, size) if font_path else \
        ImageFont.load_default()
    if emphasized:
        draw.rectangle([x - 8, y - 4, x + w + 8, y + h + 4],
                       outline=_with_alpha(theme.accent, alpha),
                       width=max(2, theme.stroke_width))
    draw.text((x, y), text, font=font, fill=_with_alpha(color, alpha))


def _arrow_head(draw, pts, color, lw):
    if len(pts) < 2:
        return
    (x0, y0), (x1, y1) = pts[-2], pts[-1]
    import math
    ang = math.atan2(y1 - y0, x1 - x0)
    size = max(10, lw * 4)
    for spread in (2.6, -2.6):
        draw.line([(x1, y1),
                   (x1 - size * math.cos(ang + spread / 2),
                    y1 - size * math.sin(ang + spread / 2))],
                  fill=color, width=max(2, lw))


def _role_color(style, theme):
    from .layout import _STYLE_ROLE
    role = _STYLE_ROLE.get(style, "foreground")
    return theme.foreground if role == "foreground" else getattr(theme, role)


def _with_alpha(color: str, alpha: int):
    rgb = _hex_rgb(color)
    return (rgb[0], rgb[1], rgb[2], alpha)


def _hex_rgb(color: str):
    c = color.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return tuple(int(c[k:k + 2], 16) for k in (0, 2, 4))


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))
