"""Pillow plate rendering: text/label/callout images with alpha.

Plates are rendered ONCE per scene; the filtergraph backend animates them
(fade/slide/emphasis), the frame-pipe backend pastes them per frame.
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

from . import schema as sc
from .layout import LaidOutScene, TextLayout


def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _draw_text_into(img: Image.Image, tl: TextLayout, color=None) -> None:
    draw = ImageDraw.Draw(img)
    font = _load_font(tl.font_path, tl.font_size)
    color = color or tl.color
    y = tl.anchor[1]
    for line in tl.lines:
        draw.text((tl.anchor[0], y), line, font=font, fill=color)
        y += tl.line_height


def text_plate(tl: TextLayout, canvas: tuple) -> Image.Image:
    img = Image.new("RGBA", canvas, (0, 0, 0, 0))
    _draw_text_into(img, tl)
    return img


def type_on_plates(tl: TextLayout, canvas: tuple, step_chars: int = 2):
    """One plate per prefix (typewriter). Honest type-on: real prefixes, not a
    wipe. Long text falls back to a single plate (fade in)."""
    text = "".join(tl.lines) if len(tl.lines) == 1 else tl.text
    if len(text) > 60:
        return [text_plate(tl, canvas)]
    plates = []
    font = _load_font(tl.font_path, tl.font_size)
    for n in range(1, len(text) + 1, max(1, step_chars)):
        img = Image.new("RGBA", canvas, (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.text((tl.anchor[0], tl.anchor[1]), text[:n], font=font, fill=tl.color)
        plates.append(img)
    plates.append(text_plate(tl, canvas))
    return plates


def callout_plate(tl: TextLayout, box_px, theme, number=None,
                  outline_color=None) -> Image.Image:
    """Numbered card: outline box + short text (reference-style callout)."""
    img = Image.new("RGBA", box_px[2:] and canvas_size(box_px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    w, h = img.size
    oc = outline_color or theme.foreground
    lw = max(2, theme.stroke_width)
    draw.rectangle([lw // 2, lw // 2, w - lw - lw // 2, h - lw - lw // 2],
                   outline=oc, width=lw)
    if tl.font_path:
        font = _load_font(tl.font_path, tl.font_size)
    else:
        font = ImageFont.load_default()
    pad = 14 + lw
    y = pad
    if number is not None:
        draw.text((pad, y), f"{number}", font=font, fill=oc)
        y += tl.line_height
    for line in tl.lines:
        draw.text((pad, y), line, font=font, fill=tl.color)
        y += tl.line_height
    return img


def canvas_size(box_px):
    return (box_px[2], box_px[3])


def image_plate(asset_abs_path: str, box_px, fit: str = "contain") -> Image.Image:
    """RGBA plate of an image asset scaled into its box. Missing files were
    rejected by validation, so a load failure here is a hard error."""
    try:
        src = Image.open(asset_abs_path)
    except OSError as e:
        raise RuntimeError(f"image asset failed to load: {asset_abs_path} ({e})") from None
    src = src.convert("RGBA")
    bw, bh = int(box_px[2]), int(box_px[3])
    if bw <= 0 or bh <= 0:
        raise RuntimeError(f"image asset {asset_abs_path} has an empty box.")
    if fit == "stretch":
        out = src.resize((bw, bh), Image.LANCZOS)
    else:
        ratio = min(bw / src.width, bh / src.height)
        nw, nh = max(1, int(src.width * ratio)), max(1, int(src.height * ratio))
        out = src.resize((nw, nh), Image.LANCZOS)
    plate = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    plate.paste(out, ((bw - out.width) // 2, (bh - out.height) // 2), out)
    return plate


def accent_copy(plate: Image.Image, color: str) -> Image.Image:
    """Recolor the plate's text pixels for emphasis windows."""
    img = plate.copy()
    px = img.load()
    w, h = img.size
    rgb = _hex_rgb(color)
    for j in range(h):
        for i in range(w):
            r, g, b, a = px[i, j]
            if a:
                px[i, j] = (rgb[0], rgb[1], rgb[2], a)
    return img


def _hex_rgb(color: str):
    c = color.lstrip("#")
    return tuple(int(c[k:k + 2], 16) for k in (0, 2, 4))


def crop_to_content(plates: list) -> tuple:
    """Crop full-canvas plates to the box that holds all their pixels.

    A text plate used to be as big as the whole frame (8 MB at 1080p) with a
    few words in it, and a typed-on label has one per two characters: ffmpeg
    held every one of them in memory. All plates of one object are cropped to
    the SAME box (the union), so they still line up frame to frame.
    Returns (cropped plates, (x, y) of the box on the canvas)."""
    box = None
    for p in plates:
        b = p.getbbox()
        if b:
            box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]),
                                         max(box[2], b[2]), max(box[3], b[3]))
    if box is None:                      # nothing drawn: keep a 1px plate
        return [p.crop((0, 0, 1, 1)) for p in plates], (0, 0)
    return [p.crop(box) for p in plates], (box[0], box[1])


def render_scene_plates(laid: LaidOutScene, canvas: tuple, out_dir: str,
                        theme, project_folder: str = "") -> dict:
    """-> {object_id: (paths, kind, origin)}.

    origin = where the plate must be composited: (0, 0) for full-canvas text
    plates (text already at its absolute anchor), or the box position for
    box-sized plates (callout, image). Getting this wrong double-offsets the
    plate."""
    os.makedirs(out_dir, exist_ok=True)
    result = {}
    for lo in laid.objects:
        spec = lo.spec
        if spec.type in (sc.TEXT, sc.LABEL) and lo.text is not None:
            if spec.type == sc.LABEL:
                plates = type_on_plates(lo.text, canvas, step_chars=2)
                kind = "typeon"
            else:
                plates = [text_plate(lo.text, canvas)]
                kind = "single"
            plates, origin = crop_to_content(plates)
            paths = []
            for i, p in enumerate(plates):
                path = os.path.join(out_dir, f"{spec.id}_{i:03d}.png")
                p.save(path)
                paths.append(path)
            result[spec.id] = (paths, kind, origin)
        elif spec.type == sc.CALLOUT and lo.text is not None:
            img = callout_plate(lo.text, lo.box_px, theme,
                                number=None, outline_color=theme.foreground)
            path = os.path.join(out_dir, f"{spec.id}.png")
            img.save(path)
            result[spec.id] = ([path], "single", (lo.box_px[0], lo.box_px[1]))
        elif spec.type == sc.IMAGE and spec.asset:
            if not project_folder:
                raise RuntimeError(
                    f"object {spec.id}: image assets need the project folder "
                    "to resolve the asset path.")
            img = image_plate(os.path.join(project_folder, spec.asset),
                              lo.box_px, spec.fit)
            path = os.path.join(out_dir, f"{spec.id}.png")
            img.save(path)
            result[spec.id] = ([path], "single", (lo.box_px[0], lo.box_px[1]))
    return result
