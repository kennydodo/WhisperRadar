"""Port of ImgToVideo.Core.Analysis.ProjectLoader: reads a project folder into a ProjectInventory."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from . import manifest as mf
from . import models as m
from . import overrides as ov
from . import parsing
from . import imaging
from .options import NamingOptions, ProjectOptions

AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".aac", ".flac")


@dataclass
class ProjectInventory:
    project_folder: str = ""
    audio_file_path: Optional[str] = None
    srt_file_path: Optional[str] = None
    all_images: list = field(default_factory=list)
    scene_groups: list = field(default_factory=list)
    subtitles: list = field(default_factory=list)
    scene_map_windows: Optional[list] = None
    manifest: Optional[mf.VisualManifest] = None
    overrides: ov.ProjectOverrides = field(default_factory=ov.ProjectOverrides)
    issues: list = field(default_factory=list)


def _issue(sev, code, msg):
    return m.ValidationIssue(sev, code, msg)


def _files(directory: str) -> list:
    """Directory.EnumerateFiles (files only), natural-sorted by file name."""
    out = []
    with os.scandir(directory) as it:
        for e in it:
            if e.is_file():
                out.append(e.path)
    out.sort(key=lambda p: parsing.natural_key(os.path.basename(p)))
    return out


def _ext(path: str) -> str:
    return os.path.splitext(path)[1]


def _allowed_extensions(naming: NamingOptions) -> set:
    return {(e if e.startswith(".") else "." + e).lower() for e in naming.image_extensions}


def load(project_folder: str, options: Optional[ProjectOptions] = None) -> ProjectInventory:
    options = options or ProjectOptions()
    issues: list = []
    audio = _find_audio(project_folder, issues)
    srt = _find_srt(project_folder, issues)
    subtitles = _parse_subtitles(srt, issues)
    overrides = _load_overrides(project_folder, issues)
    manifest = _load_manifest_for_planner(project_folder, options, subtitles, issues)
    if manifest is None:
        all_images, groups = _load_images(project_folder, options.naming, overrides, issues)
    else:
        all_images, groups = _load_manifest_images(manifest, project_folder, options.naming, issues)
    scene_map = _load_scene_map(project_folder, issues)
    return ProjectInventory(project_folder, audio, srt, all_images, groups, subtitles,
                            scene_map, manifest, overrides, issues)


# ------------------------------------------------------------------ manifest / shotlist
def _load_visual_manifest(project_folder, issues):
    try:
        return mf.load(project_folder)
    except OSError as e:
        issues.append(_issue(m.ERROR, "MANIFEST_INVALID",
                             f"visual_manifest.json could not be read: {e}"))
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        issues.append(_issue(m.ERROR, "MANIFEST_INVALID",
                             f"visual_manifest.json could not be loaded: {e}"))
    return None


def _load_manifest_for_planner(project_folder, options, subtitles, issues):
    from . import shotlist
    shot_list_path = os.path.join(project_folder, shotlist.FILE_NAME)
    if not os.path.isfile(shot_list_path):
        return _load_visual_manifest(project_folder, issues)

    if options.planner.lower() != "v1":
        try:
            with open(shot_list_path, "r", encoding="utf-8-sig", errors="replace") as f:
                text = f.read()
            document = shotlist.parse(text, issues, options.shotlist.master_prompt_max_chars,
                                      options.shotlist.prompt_max_chars)
        except OSError as e:
            issues.append(_issue(m.ERROR, "SHOTLIST_INVALID",
                                 f"shotlist.json could not be read: {e}"))
            return _load_visual_manifest(project_folder, issues)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            issues.append(_issue(m.ERROR, "SHOTLIST_INVALID",
                                 f"shotlist.json could not be loaded: {e}"))
            return _load_visual_manifest(project_folder, issues)

        manifest = shotlist.expand(document, subtitles,
                                   _enumerate_image_file_names(project_folder, options.naming),
                                   options.transitions, issues)
        if manifest is None:
            return _load_visual_manifest(project_folder, issues)

        if mf.exists(project_folder):
            issues.append(_issue(
                m.INFO, "SHOTLIST_PRECEDENCE",
                "shotlist.json takes precedence — visual_manifest.json was regenerated from it "
                "(edit the shotlist, not the manifest)."))
        try:
            mf.save(manifest, os.path.join(project_folder, mf.FILE_NAME))
        except OSError as e:
            issues.append(_issue(m.WARNING, "SHOTLIST_SAVE_FAILED",
                                 f"The expanded visual_manifest.json could not be written: {e}"))
        return manifest

    issues.append(_issue(
        m.INFO, "SHOTLIST_IGNORED",
        'shotlist.json found but "planner" is "v1" — set "planner": "v2" in '
        "imgtovideo.json to plan from the shot list."))
    return _load_visual_manifest(project_folder, issues)


def _enumerate_image_file_names(project_folder, naming) -> list:
    images_dir = os.path.join(project_folder, "images")
    if not os.path.isdir(images_dir):
        return []
    allowed = _allowed_extensions(naming)
    return [os.path.basename(f) for f in _files(images_dir) if _ext(f).lower() in allowed]


# ------------------------------------------------------------------ images
def _warn_on_risky_png_format(file: str, issues: list) -> None:
    if _ext(file).lower() != ".png":
        return
    try:
        with open(file, "rb") as f:
            header = f.read(26)
    except OSError:
        return
    if len(header) < 26 or header[0] != 0x89 or header[1] != 0x50 or header[2] != 0x4E or header[3] != 0x47:
        return
    bit_depth = header[24]
    color_type = header[25]
    if bit_depth == 8 and color_type in (2, 6):
        return
    color_name = {0: "grayscale", 2: "RGB", 3: "palette", 4: "grayscale+alpha",
                  6: "RGBA"}.get(color_type, "unknown")
    issues.append(_issue(
        m.WARNING, "PNG_FORMAT_RISKY",
        f'"{os.path.basename(file)}" is a {bit_depth}-bit {color_name} PNG — Premiere can render such files '
        "as black frames. Re-export it as an 8-bit RGB or RGBA PNG."))


def _parse_scene_number(stem: str) -> int:
    mt = re.match(r"^S(\d+)", stem, re.IGNORECASE)
    return int(mt.group(1)) if mt else 0


def _read_size(file: str, issues: list):
    size = imaging.try_read(file)
    if size is None:
        issues.append(_issue(m.WARNING, "IMAGE_UNREADABLE",
                             f'"{os.path.basename(file)}" has unreadable image dimensions.'))
        return 0, 0
    return size


def _load_manifest_images(manifest, project_folder, naming, issues):
    images_dir = os.path.join(project_folder, "images")
    if not os.path.isdir(images_dir):
        issues.append(_issue(m.ERROR, "IMAGES_MISSING",
                             'No "images" folder found in the project folder.'))
        return [], []
    allowed = _allowed_extensions(naming)
    images: list = []
    parser = parsing.ImageFilenameParser(naming)
    for file in _files(images_dir):
        if _ext(file).lower() not in allowed:
            continue
        width, height = _read_size(file, issues)
        _warn_on_risky_png_format(file, issues)
        stem = os.path.splitext(os.path.basename(file))[0]
        parsed = parser.try_parse(stem)
        if parsed is None:
            parsed = m.ParsedImageName(stem, _parse_scene_number(stem), 0, None, False)
        images.append(m.ImageInfo(file, parsed, width, height))

    by_scene: dict = {}
    first_key: dict = {}
    for a in manifest.assets or []:
        if a.scene_id is None:
            continue
        k = a.scene_id.lower()
        if k not in by_scene:
            by_scene[k] = []
            first_key[k] = a.scene_id
        by_scene[k].append(a)
    groups = []
    for index, (k, assets) in enumerate(by_scene.items()):
        key = first_key[k]
        files = {(a.file or "").lower() for a in assets}
        scene_images = [i for i in images if os.path.basename(i.file_path).lower() in files]
        number = _parse_scene_number(key)
        groups.append(m.SceneImageGroup(number if number > 0 else index + 1, key, tuple(scene_images)))
    groups.sort(key=lambda g: g.scene_number)
    return images, groups


def _load_images(project_folder, naming, overrides, issues):
    images_dir = os.path.join(project_folder, "images")
    if not os.path.isdir(images_dir):
        issues.append(_issue(m.ERROR, "IMAGES_MISSING",
                             'No "images" folder found in the project folder.'))
        return [], []
    allowed = _allowed_extensions(naming)
    parser = parsing.ImageFilenameParser(naming)
    all_files = _files(images_dir)
    if not all_files:
        issues.append(_issue(m.ERROR, "IMAGES_EMPTY",
                             'The "images" folder is empty — it contains no files at all.'))
        return [], []

    files = [f for f in all_files if _ext(f).lower() in allowed]
    if not files:
        present = sorted({_ext(f).lower() for f in all_files if _ext(f)})
        issues.append(_issue(
            m.WARNING, "IMAGES_EXTENSION",
            f'Found {len(all_files)} files in "images" but none has an allowed extension '
            f"({', '.join(naming.image_extensions)}). Extensions present: "
            f"{', '.join(present)}. "
            "Add them in Settings > File naming > Extensions."))

    max_shown = 5
    shown = 0
    for file in files:
        parsed = parser.try_parse(file)
        if parsed is None:
            shown += 1
            if shown <= max_shown:
                issues.append(_issue(
                    m.WARNING, "IMAGE_UNRECOGNIZED",
                    f'"{os.path.basename(file)}" does not match the naming pattern '
                    "(expected S{scene}_{index}[_CODE].png, e.g. S08_02_PR.png) and was ignored."))
        elif parsed.has_unknown_suffix:
            issues.append(_issue(
                m.WARNING, "IMAGE_UNKNOWN_CODE",
                f'"{os.path.basename(file)}" has an unknown suffix; motion will be auto-selected.'))
    if shown > max_shown:
        issues.append(_issue(
            m.WARNING, "IMAGE_UNRECOGNIZED_MORE",
            f"…and {shown - max_shown} more files were ignored for the same reason."))

    images: list = []
    seen: set = set()
    for file in files:
        parsed = parser.try_parse(file)
        if parsed is None:
            continue
        stem_key = os.path.splitext(os.path.basename(file))[0].lower()
        if stem_key in seen:
            issues.append(_issue(
                m.WARNING, "IMAGE_DUPLICATE_STEM",
                f'"{os.path.basename(file)}" duplicates an earlier stem; only the first file is used.'))
            continue
        seen.add(stem_key)
        width, height = _read_size(file, issues)
        _warn_on_risky_png_format(file, issues)
        images.append(m.ImageInfo(file, parsed, width, height))

    for image in list(images):
        co = overrides.for_clip(image.file_path)
        if co is not None and co.exclude:
            issues.append(_issue(
                m.INFO, "IMAGE_EXCLUDED",
                f'"{os.path.basename(image.file_path)}" is excluded by overrides and was skipped.'))
            images.remove(image)

    if not images:
        issues.append(_issue(
            m.ERROR, "IMAGES_NONE",
            'No correctly named images were found in the "images" folder. '
            "Names must look like S01_01.png or S08_02_SCN_PR.png "
            "(pattern: S{scene}_{index}[_TYPE][_CODE].png)."))

    order_lookup: dict = {}
    for c in overrides.clips:
        if c.order is not None:
            order_lookup.setdefault(c.file.lower(), c.order)
    big = 2 ** 31 - 1
    images.sort(key=lambda i: order_lookup.get(i.file_path.lower(), big))

    by_scene: dict = {}
    for i in images:
        by_scene.setdefault(i.name.scene_number, []).append(i)
    groups = [m.SceneImageGroup(n, naming.scene_id(n), tuple(by_scene[n])) for n in sorted(by_scene)]
    return images, groups


# ------------------------------------------------------------------ overrides / scene map
def _resolve_project_path(project_folder: str, file: str) -> str:
    if os.path.isabs(file):
        return file
    return os.path.normpath(os.path.abspath(os.path.join(project_folder, file)))


def _load_overrides(project_folder, issues):
    try:
        overrides = ov.load_or_default(os.path.join(project_folder, ov.FILE_NAME))
    except (ValueError, TypeError, AttributeError) as e:
        issues.append(_issue(m.ERROR, "OVERRIDES_INVALID",
                             f"overrides.json could not be loaded: {e}"))
        return ov.ProjectOverrides()
    for clip in overrides.clips:
        clip.file = _resolve_project_path(project_folder, clip.file)
    for cut in overrides.cuts:
        cut.before_file = _resolve_project_path(project_folder, cut.before_file)
    return overrides


def _load_scene_map(project_folder, issues):
    path = os.path.join(project_folder, "scenes.json")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        windows, scene_issues = parsing.parse_scene_map(f.read())
    issues.extend(scene_issues)
    return windows


# ------------------------------------------------------------------ audio / srt
def _find_audio(project_folder, issues) -> Optional[str]:
    audio_dir = os.path.join(project_folder, "audio")
    for directory in (audio_dir, project_folder):
        if not os.path.isdir(directory):
            continue
        candidates = [f for f in _files(directory) if _ext(f).lower() in AUDIO_EXTENSIONS]
        if not candidates:
            continue
        if len(candidates) > 1:
            issues.append(_issue(
                m.INFO, "AUDIO_AMBIGUOUS",
                f'Multiple audio files found in "{os.path.basename(directory)}"; '
                f'using "{os.path.basename(candidates[0])}".'))
        return candidates[0]
    issues.append(_issue(
        m.ERROR, "AUDIO_MISSING",
        'No audio file (.mp3, .wav, …) found in the "audio" folder or the project root.'))
    return None


def _find_srt(project_folder, issues) -> Optional[str]:
    if os.path.isdir(project_folder):
        candidates = [f for f in _files(project_folder) if _ext(f).lower() == ".srt"]
        if len(candidates) > 1:
            issues.append(_issue(
                m.INFO, "SRT_AMBIGUOUS",
                f'Multiple subtitle files found; using "{os.path.basename(candidates[0])}".'))
        if candidates:
            return candidates[0]
    issues.append(_issue(m.ERROR, "SRT_MISSING",
                         "No subtitle file (.srt) found in the project folder."))
    return None


def _parse_subtitles(srt_path, issues) -> list:
    if srt_path is None:
        return []
    name = os.path.basename(srt_path)
    try:
        with open(srt_path, "r", encoding="utf-8-sig", errors="replace") as f:
            blocks = parsing.parse_srt(f.read())
        if not blocks:
            issues.append(_issue(m.ERROR, "SRT_EMPTY", f'"{name}" contains no subtitle blocks.'))
        return blocks
    except OSError as e:
        issues.append(_issue(m.ERROR, "SRT_UNREADABLE", f'"{name}" could not be read: {e}'))
        return []
    except ValueError as e:
        issues.append(_issue(m.ERROR, "SRT_UNPARSABLE", f'"{name}" could not be parsed: {e}'))
        return []
