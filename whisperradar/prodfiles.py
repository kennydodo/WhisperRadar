"""Where the generated files of a production live.

A production folder used to hold 20-30 loose files. The files the renderer
reads (audio, subtitles.srt, shotlist.json, images/, refs/, imgtovideo.json,
visual_manifest.json, motion_gate.json, out/) stay in the root; everything
else is grouped:

  flow/       the Flow job files and their reports (rebuilt from the shotlist)
  motion/     the animated-opening job and report
  packaging/  titles, description, thumbnails, the packaging plan and kit
  _work/      chat links and the raw chat replies (safe to delete)

`path()` is the single lookup: the new place when the file is there, the old
(root) place when only that exists, the new place otherwise. So a production
that has not been migrated keeps working, and new files of a migrated one
land in the new folders. `migrate()` moves an old production over.
"""
from __future__ import annotations

import filecmp
import json
import os
import re
import shutil
from pathlib import Path

GROUPS = {
    "flow": ("flowbatch.json", "flow_prepare.json", "flowbatch_refs.json",
             "flowbatch_recover.json", "refs_generated.json", "refs_project.json",
             "refs_titles.json"),
    "motion": ("motion_job.json", "motion_report.json"),
    "packaging": ("packaging_plan.json", "publish_kit.json", "title.txt",
                  "titles.txt", "description.txt", "thumbnails"),
    "_work": ("webchat_chats.json", "webchat_debug"),
}
FOLDER = {name: group for group, names in GROUPS.items() for name in names}
OLD_DIR = "_work/old"          # leftovers go here instead of being deleted

_JUNK = (re.compile(r"^out\d+$"), re.compile(r".*\.bak$"),
         re.compile(r"^shotlist_baseline\.json$"), re.compile(r"^elements_v\d.*\.md$"))


def rel(name: str) -> str:
    """The new location of `name`, relative to the production folder."""
    group = FOLDER.get(name)
    return f"{group}/{name}" if group else name


def path(pdir, name: str) -> Path:
    """Where to read (or write) `name`: the new place, else the old one if
    only that exists, else the new place."""
    pdir = Path(pdir)
    new = pdir / rel(name)
    if new.exists():
        return new
    old = pdir / name
    return old if old.exists() else new


def wpath(pdir, name: str) -> Path:
    """Like path(), and the folder it sits in exists, ready for writing."""
    p = path(pdir, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def relative(pdir, name: str) -> str:
    """`name` as a forward-slash path relative to the production folder (for
    values stored in JSON and for /studio/file/<pid>/... URLs)."""
    return path(pdir, name).relative_to(Path(pdir)).as_posix()


# ---- migration ------------------------------------------------------------

def _pending(pdir: Path) -> list[str]:
    return [n for n in FOLDER if (pdir / n).exists() and not (pdir / rel(n)).exists()]


def _junk(pdir: Path) -> list[Path]:
    found = [p for p in pdir.iterdir() if any(rx.match(p.name) for rx in _JUNK)]
    refs = pdir / "refs"
    if refs.is_dir():
        found += [p for p in refs.iterdir() if p.name.lower() == "thumbs.db"]
    return found


def _duplicate_pairs(pdir: Path):
    """(original, copy) of files stored twice: images vs flow_images and the
    audio copy the renderer step used to make."""
    a, b = pdir / "flow_images", pdir / "images"
    if a.is_dir() and b.is_dir():
        for f in b.iterdir():
            o = a / f.name
            if f.is_file() and o.is_file():
                yield o, f
    audio_dir = pdir / "audio"
    if audio_dir.is_dir():
        for f in audio_dir.glob("narration.*"):
            o = pdir / ("audio" + f.suffix)
            if o.is_file():
                yield o, f


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def needs_migration(pdir) -> bool:
    pdir = Path(pdir)
    if not pdir.is_dir():
        return False
    return bool(_pending(pdir) or _junk(pdir)
                or any(not _same_file(o, c) and _identical(o, c)
                       for o, c in _duplicate_pairs(pdir)))


def _identical(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def link_or_copy(src: Path, dst: Path) -> None:
    """Make `dst` the same file as `src` without a second copy on disk when the
    drive allows (a hard link); copy otherwise."""
    dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _fix_json_paths(pdir: Path, report: list[str]) -> None:
    """Thumbnail paths stored in the kit and in thumbs.json."""
    for f in (pdir / "packaging" / "thumbnails" / "thumbs.json",
              pdir / "packaging" / "publish_kit.json"):
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8")
        new = text.replace('"thumbnails/', '"packaging/thumbnails/')
        new = re.sub(r'(?<!packaging)\\\\thumbnails\\\\', r'\\\\packaging\\\\thumbnails\\\\', new)
        new = re.sub(r'(?<!packaging)/thumbnails/', '/packaging/thumbnails/', new)
        if new != text:
            f.write_text(new, encoding="utf-8")
            report.append(f"updated thumbnail paths in {f.name}")


def migrate(pdir) -> list[str]:
    """Move one production to the grouped layout. Nothing is deleted: leftovers
    go to _work/old, duplicate copies become links to the original. Returns
    what was done, one line per change (empty = already tidy)."""
    pdir = Path(pdir)
    done: list[str] = []
    for name in _pending(pdir):
        dst = pdir / rel(name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(pdir / name, dst)
        done.append(f"{name} -> {rel(name)}")
    _fix_json_paths(pdir, done)
    for p in _junk(pdir):
        dst = pdir / OLD_DIR / p.relative_to(pdir).as_posix().replace("/", "__")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            continue
        os.replace(p, dst)
        done.append(f"{p.relative_to(pdir).as_posix()} -> {OLD_DIR}/")
    saved = linked = 0
    for orig, copy in _duplicate_pairs(pdir):
        if _same_file(orig, copy):
            continue
        try:
            if orig.stat().st_size != copy.stat().st_size or not filecmp.cmp(orig, copy, shallow=False):
                continue
            size = copy.stat().st_size
            link_or_copy(orig, copy)
            if _same_file(orig, copy):
                linked += 1
                saved += size
        except OSError:
            continue
    if linked:
        done.append(f"{linked} duplicate file(s) now share one copy on disk "
                    f"(about {saved / 1e6:.0f} MB saved)")
    return done
