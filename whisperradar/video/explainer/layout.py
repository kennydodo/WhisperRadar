"""Layout compiler: theme + normalized boxes -> pixel geometry, wrapped text.

Everything the render backends need per scene is resolved here ONCE, so the
evaluator and both backends stay pure geometry/state code.

Font lookup order (first hit wins):
1. the path as given (absolute, or already relative to cwd);
2. the repo-level `fonts/` folder (drop Google Fonts etc. here);
3. project-relative (resolved against the production folder);
4. C:/Windows/Fonts (system fonts by bare filename).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from PIL import ImageFont

from . import schema as sc
from .validate import ExplainerError

_REPO_FONTS_DIR = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "fonts"))

_SYSTEM_FONT_DIRS = [
    "C:/Windows/Fonts",
    "C:/Windows/System32/fonts",
]


def find_font(path: str, project_folder: str = "") -> str:
    """Resolve a font reference; '' when it cannot be found (caller errors)."""
    if not path:
        return ""
    cand = path.replace("\\", "/")
    if os.path.isfile(cand):
        return os.path.abspath(cand)
    # repo-level fonts folder (portable: just drop the .ttf in)
    repo = os.path.join(_REPO_FONTS_DIR, os.path.basename(cand))
    if os.path.isfile(repo):
        return repo
    # project-relative (per-production font files)
    if project_folder and not os.path.isabs(cand) and ":" not in cand \
            and not cand.startswith(".."):
        proj = os.path.join(project_folder, cand)
        if os.path.isfile(proj):
            return proj
    for d in _SYSTEM_FONT_DIRS:
        sys_font = os.path.join(d, os.path.basename(cand))
        if os.path.isfile(sys_font):
            return sys_font
    return ""

_FONT_DIRS = _SYSTEM_FONT_DIRS  # kept for any external references

_STYLE_ROLE = {
    "heading": "foreground",
    "title": "foreground",
    "label": "foreground",
    "body": "foreground",
    "muted": "muted",
    "stat": "accent",
    "accent": "accent",
    "positive": "positive",
    "negative": "negative",
    "default": "foreground",
}


@dataclass
class TextLayout:
    text: str
    lines: list                          # wrapped lines
    font_path: str
    font_size: int
    color: str
    role: str
    line_height: int
    anchor: tuple                        # (x, y) px of the first line's top-left
    max_width: int


@dataclass
class LaidOutObject:
    spec: sc.ObjectSpec
    box_px: tuple                        # (x, y, w, h)
    text: TextLayout | None = None       # text/label/callout
    error: str = ""


@dataclass
class LaidOutScene:
    spec: sc.SceneSpec
    canvas: tuple                        # (W, H)
    objects: list = field(default_factory=list)   # LaidOutObject


def _find_font(path: str, where: str, errors: list) -> str:
    """Legacy helper kept for compatibility; prefer find_font()."""
    resolved = find_font(path)
    if not resolved:
        errors.append(f"{where}: font file not found: {path}")
    return resolved


def _role_color(spec: sc.ObjectSpec, theme: sc.ThemeSpec) -> str:
    role = _STYLE_ROLE.get(spec.style, "foreground")
    return getattr(theme, role if role != "foreground" else "foreground")


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list:
    lines = []
    for hard in text.split("\n"):
        words = hard.split(" ")
        cur = ""
        for w in words:
            trial = (cur + " " + w).strip()
            if cur and font.getlength(trial) > max_width:
                lines.append(cur)
                cur = w
            else:
                cur = trial
        lines.append(cur)
    return lines or [""]


def layout_text(text, box_px, style, theme, canvas_h, scale, where, errors,
                project_folder: str = "") -> TextLayout:
    x, y, w, h = box_px
    size_name = {"heading": "heading_size", "body": "body_size",
                 "label": "label_size", "stat": "heading_size"}.get(style, "body_size")
    size = int(round(getattr(theme, size_name) * scale))
    font_name = {"heading": theme.heading_font, "stat": theme.mono_font}.get(
        style, theme.body_font if style in ("body", "muted") else theme.heading_font)
    font_path = find_font(font_name, project_folder)
    if not font_path:
        errors.append(f"{where}: font file not found: {font_name} "
                      "(checked the path as given, the repo fonts/ folder, "
                      "the project folder, and C:/Windows/Fonts).")
        return TextLayout(str(text), [str(text)], "", size, "#FF00FF", "foreground",
                          size + 6, (x, y), int(w))
    try:
        font = ImageFont.truetype(font_path, size)
    except OSError as e:
        errors.append(f"{where}: font {font_path} failed to load ({e}).")
        return TextLayout(str(text), [str(text)], "", size, "#FF00FF", "foreground",
                          size + 6, (x, y), int(w))
    lines = _wrap(str(text), font, int(w))
    line_h = size + max(6, size // 5)
    total_h = line_h * len(lines)
    if total_h > h + 2:
        errors.append(f"{where}: text needs {total_h}px but the box is {int(h)}px tall "
                      "(shorten the text or grow the box).")
    color = _STYLE_ROLE.get(style, "foreground")
    color = theme.foreground if color == "foreground" else getattr(theme, color)
    return TextLayout(str(text), lines, font_path, size, color,
                      "accent" if style == "stat" else "foreground",
                      line_h, (x, y), int(w))


def layout_scene(scene, theme: sc.ThemeSpec, canvas: tuple,
                 fps_scale: float = 1.0, project_folder: str = "") -> LaidOutScene:
    """scene: SceneSpec (spec) or CompiledScene (uses .spec)."""
    if hasattr(scene, "spec"):
        scene = scene.spec
    errors = []
    W, H = canvas
    out = LaidOutScene(scene, canvas)
    scale = H / 1080.0
    for spec in scene.objects:
        x, y, w, h = spec.box
        lo = LaidOutObject(spec, (int(round(x * W)), int(round(y * H)),
                                  int(round(w * W)), int(round(h * H))))
        if spec.type in (sc.TEXT, sc.LABEL, sc.CALLOUT):
            spec_text = spec.text.upper() if spec.type == sc.LABEL else spec.text
            lo.text = layout_text(spec_text, lo.box_px, spec.style, theme, H,
                                  scale, f"Scene {scene.id}, object {spec.id}",
                                  errors, project_folder)
            if spec.color:                      # named chalk colour overrides the style
                lo.text.color = sc.resolve_color(spec.color, theme) or lo.text.color
        out.objects.append(lo)
    if errors:
        raise ExplainerError(errors)
    _fit_shapes(out, theme, project_folder)
    return out


def _fit_shapes(out, theme, project_folder: str) -> None:
    """An underline or highlight with fit_to is as wide as the text it belongs to is actually
    written (the font decides that, not the character count)."""
    from . import pipe
    by_id = {lo.spec.id: lo for lo in out.objects}
    W, H = out.canvas
    for lo in out.objects:
        spec = lo.spec
        if spec.type != sc.SHAPE or not spec.fit_to:
            continue
        tgt = by_id.get(spec.fit_to)
        if tgt is None or tgt.spec.type != sc.HANDWRITING or tgt.spec.font == "strokes":
            continue
        font = find_font(tgt.spec.font or theme.hand_font, project_folder)
        if not font:
            continue
        weight = theme.stroke_width / 5.0 * pipe.WEIGHT_FACTOR.get(getattr(theme, "text_weight", ""), 0.0)
        tw = pipe.text_width_px(font, tgt.spec.text, tgt.box_px, weight,
                                pipe.TRACKING_EM.get(getattr(theme, "letter_spacing", ""), 0.04))
        tx, _ty, _tw, _th = tgt.box_px
        x, y, w, h = lo.box_px
        pad = int(round(0.012 * W))
        if spec.kind == "highlight":
            nx, nw = tx - pad, tw + 2 * pad
        else:
            nx, nw = tx, tw + pad
        nw = max(8, min(nw, W - nx - int(0.03 * W)))
        lo.box_px = (nx, y, nw, h)


def layout_doc(doc, compiled_scenes, project_folder: str = "") -> list:
    """ExplainerDoc + CompiledScenes -> LaidOutScenes (same order)."""
    return [layout_scene(cs.spec, doc.theme, doc.canvas,
                         project_folder=project_folder)
            for cs in compiled_scenes]
