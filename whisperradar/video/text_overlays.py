"""Text the renderer draws itself (shotlist `overlays`), so images can be made
WITHOUT text: the shotlist says what to write and where, the channel's own font
and style are applied here.

`resolve` writes one full-frame transparent PNG per overlay (out/text/); the
render step (render_plan.build_mux_arguments) overlays each PNG on the video
during its frames, with a fade or slide."""
from __future__ import annotations

import glob
import hashlib
import os
import sys
from typing import List, Tuple

from . import models as m

# Style table. size = % of the frame height; colours are RGB(A) tuples. A shotlist
# names a style, the channel decides how it looks (later: per-channel overrides).
STYLES = {
    # spoken-text style captions at the bottom
    "caption": {"size": 5.2, "color": (255, 255, 255), "outline": 0.10, "shadow": True,
                "box": None, "bold": True, "max_width": 0.80},
    # a big number or short claim
    "stat": {"size": 11.0, "color": (255, 214, 10), "outline": 0.08, "shadow": True,
             "box": None, "bold": True, "max_width": 0.80},
    # a title card line
    "title": {"size": 9.0, "color": (255, 255, 255), "outline": 0.08, "shadow": True,
              "box": None, "bold": True, "max_width": 0.80},
    # a small tag next to something in the picture
    "label": {"size": 4.4, "color": (20, 20, 20), "outline": 0.0, "shadow": False,
              "box": (255, 255, 255, 235), "bold": True, "max_width": 0.40},
    # name / source strip
    "lower-third": {"size": 4.6, "color": (255, 255, 255), "outline": 0.0, "shadow": False,
                    "box": (0, 0, 0, 170), "bold": True, "max_width": 0.60},
}
DEFAULT_STYLE = "caption"

_WINDOWS_FONTS = ("arialbd.ttf", "segoeuib.ttf", "impact.ttf", "arial.ttf", "segoeui.ttf")
_LINUX_FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/lato/Lato-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")


def style_of(name: str) -> dict:
    return STYLES.get((name or "").strip().lower(), STYLES[DEFAULT_STYLE])


def find_font(options, folder: str) -> str:
    """The font file to use: the configured one, else the first font in the fonts
    folder, else a system font. "" = Pillow's built-in font (last resort)."""
    t = options.text
    candidates: List[str] = []
    if t.font:
        candidates.append(t.font if os.path.isabs(t.font) else os.path.join(folder, t.font))
        if t.fonts_dir:
            fd = t.fonts_dir if os.path.isabs(t.fonts_dir) else os.path.join(folder, t.fonts_dir)
            candidates.append(os.path.join(fd, t.font))
    for c in candidates:
        if os.path.isfile(c):
            return c
    if t.fonts_dir:
        fd = t.fonts_dir if os.path.isabs(t.fonts_dir) else os.path.join(folder, t.fonts_dir)
        for pattern in ("*.ttf", "*.otf"):
            found = sorted(glob.glob(os.path.join(fd, pattern)))
            if found:
                return found[0]
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    for name in _WINDOWS_FONTS:
        p = os.path.join(windir, "Fonts", name)
        if os.path.isfile(p):
            return p
    for p in _LINUX_FONTS:
        if os.path.isfile(p):
            return p
    return ""


def _load_font(path: str, size: int):
    from PIL import ImageFont
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                       # Pillow < 10.1
        return ImageFont.load_default()


def _wrap(draw, text: str, font, max_width: int, stroke: int) -> List[str]:
    lines: List[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        current = ""
        for word in words:
            trial = (current + " " + word).strip()
            if current and draw.textlength(trial, font=font) + 2 * stroke > max_width:
                lines.append(current)
                current = word
            else:
                current = trial
        lines.append(current)
    return [ln for ln in lines if ln != ""] or [""]


def _anchor(position: str, width: int, height: int, box_w: int, box_h: int,
            margin: int) -> Tuple[int, int]:
    vert, _, horiz = position.partition("-")
    if position == "center":
        vert, horiz = "middle", "center"
    elif horiz == "":
        horiz = "center"
    if horiz == "left":
        x = margin
    elif horiz == "right":
        x = width - margin - box_w
    else:
        x = (width - box_w) // 2
    if vert == "top":
        y = margin
    elif vert == "bottom":
        y = height - margin - box_h
    else:
        y = (height - box_h) // 2
    return int(x), int(y)


def render_png(overlay: m.TextOverlay, width: int, height: int, font_path: str,
               margin_percent: float, out_path: str) -> None:
    """One full-frame transparent PNG with the overlay's text drawn at its position."""
    from PIL import Image, ImageDraw
    st = style_of(overlay.style)
    size = max(8, int(round(height * st["size"] / 100.0)))
    font = _load_font(font_path, size)
    stroke = int(round(size * st["outline"]))
    margin = int(round(min(width, height) * margin_percent / 100.0))
    pad = int(round(size * 0.35)) if st["box"] else 0
    max_w = int(width * st["max_width"]) - 2 * pad
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    lines = _wrap(draw, overlay.text, font, max_w, stroke)
    spacing = int(round(size * 0.18))
    metrics = [draw.textbbox((0, 0), ln or " ", font=font, stroke_width=stroke) for ln in lines]
    line_h = [b[3] - b[1] for b in metrics]
    line_w = [b[2] - b[0] for b in metrics]
    text_w = max(line_w)
    text_h = sum(line_h) + spacing * (len(lines) - 1)
    box_w, box_h = text_w + 2 * pad, text_h + 2 * pad
    x0, y0 = _anchor(overlay.position, width, height, box_w, box_h, margin)
    horiz = overlay.position.rpartition("-")[2] if "-" in overlay.position else "center"
    if st["box"]:
        draw.rounded_rectangle((x0, y0, x0 + box_w, y0 + box_h), radius=int(pad * 0.6),
                               fill=st["box"])
    y = y0 + pad
    for ln, b, w, h in zip(lines, metrics, line_w, line_h):
        if horiz == "left":
            x = x0 + pad
        elif horiz == "right":
            x = x0 + box_w - pad - w
        else:
            x = x0 + (box_w - w) // 2
        tx, ty = x - b[0], y - b[1]
        if st["shadow"]:
            off = max(2, int(size * 0.05))
            draw.text((tx + off, ty + off), ln, font=font, fill=(0, 0, 0, 150),
                      stroke_width=stroke, stroke_fill=(0, 0, 0, 150))
        draw.text((tx, ty), ln, font=font, fill=st["color"] + (255,),
                  stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
        y += h + spacing
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    canvas.save(out_path, "PNG")


def resolve(timeline: m.Timeline, folder: str, options) -> List[m.ValidationIssue]:
    """Draw the PNG of every overlay (re-used when the text, style, position, font and
    size are unchanged). Fills `png_path`. Returns warnings (an unknown style, the
    fallback font); an overlay that cannot be drawn is dropped with a warning."""
    issues: List[m.ValidationIssue] = []
    if not timeline.overlays:
        return issues
    font = find_font(options, folder)
    if not font:
        issues.append(m.ValidationIssue(
            m.WARNING, "TEXT_FONT_FALLBACK",
            "No font file found for the text overlays - using the built-in font. Set "
            "text.font or text.fonts_dir in imgtovideo.json."))
    width, height = timeline.resolution.width, timeline.resolution.height
    out_dir = os.path.join(folder, "out", "text")
    kept: List[m.TextOverlay] = []
    for ov in timeline.overlays:
        if (ov.style or "").strip().lower() not in STYLES:
            issues.append(m.ValidationIssue(
                m.WARNING, "TEXT_STYLE_UNKNOWN",
                f'Text overlay style "{ov.style}" is unknown (known: {", ".join(STYLES)}) - '
                f'using "{DEFAULT_STYLE}".'))
        key = "|".join([ov.text, ov.style, ov.position, str(width), str(height), font,
                        str(options.text.safe_margin_percent), repr(style_of(ov.style))])
        name = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".png"
        path = os.path.join(out_dir, name)
        try:
            if not os.path.isfile(path):
                render_png(ov, width, height, font, options.text.safe_margin_percent, path)
        except Exception as exc:                      # noqa: BLE001 - drop the text, keep the video
            issues.append(m.ValidationIssue(
                m.WARNING, "TEXT_RENDER_FAILED",
                f'Could not draw the text "{ov.text[:40]}": {exc}. It is left out.'))
            continue
        ov.png_path = path
        kept.append(ov)
    timeline.overlays = kept
    return issues
