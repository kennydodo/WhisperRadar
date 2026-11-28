"""The reference catalog: which images exist, what each one shows, and which of
them THIS story uses.

A channel keeps one big library of reference images (CH_ characters, BG_
places, OBJ_ objects). A production should only hand the writers the ones the
story needs. Three small JSON files make that work, and none of them lives in
a production's refs\\ folder (the Flow code registers every file in there as
an image):

  refs.json          the LIBRARY index, beside the images in the channel's
                     reference folder: {"refs": {"OBJ_LAPTOP": {"file", "kind",
                     "description", "aliases", "always"}}}. Written by `scan`,
                     edited by hand. A re-scan only adds entries.
  refs_catalog.json  the production's copy of it, in the production folder.
                     Its existence is what turns the filter on: a production
                     without it keeps using every image, as before.
  refs_active.json   the references this story uses: {"auto", "added",
                     "removed", "text_hash"}. active = (auto + always + added)
                     minus removed. A changed script only ADDS suggestions to
                     auto, it never drops a reference.

Matching is deliberately plain words, never a model call:
  CH_   active when the character's name (first word of the name) or an alias
        appears in the script or the subtitles. Characters are per story.
  OBJ_/BG_/other  active when an alias appears or every word of the name does
        (OBJ_COIN_JAR needs "coin" and "jar"). Objects and places are reusable.
  always: true  active in every story (a presenter, a style anchor). Files that do
        not follow the CH_/BG_/OBJ_ naming default to always, so a channel's presenter
        is never dropped just because the narration does not say its name. A
        CH_ presenter needs "always": true set by hand.
Anything the production already uses (shotlist registry, storyboard maps) stays
active, so switching the filter on never breaks a plan.

A missing or broken JSON file never stops a run: the name still works, the
description is derived from the file name, and a warning is returned.

    python -m whisperradar.refcatalog <folder>      write/refresh <folder>\\refs.json
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
LIBRARY_INDEX = "refs.json"
CATALOG = "refs_catalog.json"
ACTIVE = "refs_active.json"
KINDS = ("CH", "BG", "OBJ")
VERSION = 1


# ---- names -------------------------------------------------------------------------------

def kind_of(stem: str) -> str:
    head = str(stem).split("_", 1)[0].upper()
    return head if head in KINDS and "_" in str(stem) else "other"


def name_tokens(stem: str) -> list[str]:
    """The words of a reference name without its kind prefix and without
    pure numbers: OBJ_COIN_JAR_02 -> ['coin', 'jar']."""
    s = str(stem)
    if kind_of(s) != "other":
        s = s.split("_", 1)[1]
    words = [w.lower() for w in re.findall(r"[^\W_]+", s)]
    return [w for w in words if not w.isdigit()]


def derived_description(stem: str) -> str:
    return " ".join(name_tokens(stem)) or str(stem)


def images_in(folder) -> dict[str, str]:
    """{stem: file name} of the image files directly in `folder`."""
    out: dict[str, str] = {}
    folder = Path(folder)
    if not folder.is_dir():
        return out
    for f in sorted(folder.iterdir()):
        if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
            out.setdefault(f.stem, f.name)
    return out


# ---- reading and writing -----------------------------------------------------------------

def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), ""
    except FileNotFoundError:
        return None, ""
    except (OSError, ValueError) as exc:
        return None, f"{Path(path).name} could not be read ({exc})"


def _write_json(path: Path, data) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _entries(raw) -> dict[str, dict]:
    """The {stem: entry} dict out of a catalog file, whatever shape it has."""
    if not isinstance(raw, dict):
        return {}
    body = raw.get("refs") if isinstance(raw.get("refs"), dict) else raw
    out = {}
    for k, v in body.items():
        if k in ("version", "refs") or not isinstance(v, dict):
            continue
        out[str(k)] = v
    return out


def _clean_entry(stem: str, e: dict | None, file: str = "") -> dict:
    e = e if isinstance(e, dict) else {}
    desc = str(e.get("description") or "").strip()
    aliases = e.get("aliases")
    aliases = [str(a).strip() for a in aliases if str(a).strip()] if isinstance(aliases, list) else []
    return {"file": str(e.get("file") or file or ""),
            "kind": str(e.get("kind") or kind_of(stem)),
            "description": desc or derived_description(stem),
            "aliases": aliases,
            # a file that does not follow the CH_/BG_/OBJ_ naming is the channel's own (a
            # presenter, a style anchor): it stays in every story unless it is switched off
            "always": bool(e.get("always", kind_of(stem) == "other")),
            "auto": bool(e.get("auto", not desc))}


# ---- the library index -------------------------------------------------------------------

def scan(folder) -> dict:
    """Create or refresh <folder>\\refs.json. New images get an entry (kind from
    the prefix, description from the name); existing entries are never touched.
    Returns {"added": [...], "missing": [...], "total": n, "warnings": [...]}."""
    folder = Path(folder)
    path = folder / LIBRARY_INDEX
    raw, warn = _read_json(path)
    warnings = [warn] if warn else []
    if warn:
        return {"added": [], "missing": [], "total": 0, "warnings": warnings
                + ["refs.json was left as it is - fix or delete it, then scan again"]}
    entries = _entries(raw)
    images = images_in(folder)
    added = [s for s in images if s not in entries]
    for s in added:
        entries[s] = _clean_entry(s, None, images[s])
    missing = sorted(s for s in entries if s not in images)
    if added or raw is None:
        _write_json(path, {"version": VERSION, "refs": dict(sorted(entries.items()))})
    return {"added": added, "missing": missing, "total": len(entries), "warnings": warnings}


# ---- the production's copy ---------------------------------------------------------------

def catalog_path(pdir) -> Path:
    return Path(pdir) / CATALOG


def has_catalog(pdir) -> bool:
    return catalog_path(pdir).is_file()


def build_catalog(pdir, library=None) -> dict:
    """Write the production's refs_catalog.json for the images now in its refs\\
    folder: entries come from the library index when it has them, otherwise
    they are derived from the names. Turns the filter ON for this production.
    Keeps entries (and their edits) already in an existing catalog."""
    pdir = Path(pdir)
    warnings: list[str] = []
    lib: dict[str, dict] = {}
    if library:
        raw, warn = _read_json(Path(library) / LIBRARY_INDEX)
        if warn:
            warnings.append(warn)
        lib = _entries(raw)
    raw, warn = _read_json(catalog_path(pdir))
    if warn:
        warnings.append(warn)
    current = _entries(raw)
    images = images_in(pdir / "refs")
    out = dict(current)
    for stem, file in images.items():
        if stem in current:
            continue
        out[stem] = _clean_entry(stem, lib.get(stem), file)
    for stem in list(out):
        out[stem] = _clean_entry(stem, out[stem], images.get(stem, ""))
    _write_json(catalog_path(pdir), {"version": VERSION, "refs": dict(sorted(out.items()))})
    return {"entries": len(out), "warnings": warnings}


def catalog(pdir) -> dict[str, dict]:
    raw, _ = _read_json(catalog_path(pdir))
    return {s: _clean_entry(s, e) for s, e in _entries(raw).items()}


# ---- the story ---------------------------------------------------------------------------

def story_text(pdir) -> str:
    """The script plus the subtitle text: what the references are matched against."""
    parts = []
    for name in ("script.md", "subtitles.srt"):
        try:
            parts.append((Path(pdir) / name).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(parts)


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.lower())


def _has(joined: str, phrase: str) -> bool:
    """`phrase` as whole words in the joined text, tolerating a plural."""
    p = " ".join(_words(phrase))
    if not p:
        return False
    return any(f" {c} " in joined for c in (p, p + "s", p + "es", p[:-1] if p.endswith("s") else p))


def matches(stem: str, entry: dict, text: str) -> bool:
    joined = " " + " ".join(_words(text)) + " "
    if any(_has(joined, a) for a in entry.get("aliases") or []):
        return True
    toks = name_tokens(stem)
    if not toks:
        return False
    if kind_of(stem) == "CH":
        return _has(joined, toks[0])          # the person's name; later words are the variant
    return all(_has(joined, t) for t in toks)


def used_stems(pdir) -> set[str]:
    """References the production's plans already use (shotlist registry and
    images, storyboard maps) - these stay active so a plan never loses one."""
    pdir = Path(pdir)
    out: set[str] = set()
    data, _ = _read_json(pdir / "shotlist.json")
    if isinstance(data, dict):
        if isinstance(data.get("refs"), dict):
            out.update(str(k) for k in data["refs"])
        for im in data.get("images") or []:
            if isinstance(im, dict):
                for r in im.get("refs") or []:
                    name = r.get("name") if isinstance(r, dict) else r
                    if name:
                        out.add(Path(str(name)).stem)
    sb, _ = _read_json(pdir / "storyboard.json")
    if isinstance(sb, dict):
        comp = sb.get("compiled_refs")
        if isinstance(comp, dict):
            for v in comp.values():
                if isinstance(v, list):
                    out.update(Path(str(x)).stem for x in v if x)
    return out


# ---- the active list ---------------------------------------------------------------------

def _state(pdir) -> dict | None:
    raw, _ = _read_json(Path(pdir) / ACTIVE)
    if not isinstance(raw, dict):
        return None
    lst = lambda k: [str(x) for x in raw.get(k) or []]
    return {"text_hash": str(raw.get("text_hash") or ""), "auto": lst("auto"),
            "added": lst("added"), "removed": lst("removed")}


def _save_state(pdir, st: dict) -> None:
    _write_json(Path(pdir) / ACTIVE, {"version": VERSION, **{
        k: (sorted(set(v)) if isinstance(v, list) else v) for k, v in st.items()}})


def _suggest(pdir, cat: dict, text: str) -> list[str]:
    return sorted({s for s, e in cat.items() if matches(s, e, text)} | used_stems(pdir))


def active_stems(pdir) -> set[str] | None:
    """The stems this story uses, or None when no filter applies (no catalog,
    or no story text yet and nothing saved). The first call that has story text
    computes and saves the list; a changed script only adds suggestions."""
    if not has_catalog(pdir):
        return None
    cat = catalog(pdir)
    st = _state(pdir)
    text = story_text(pdir)
    if st is None:
        if not text.strip():
            return None
        st = {"text_hash": hashlib.sha1(text.encode("utf-8")).hexdigest(),
              "auto": _suggest(pdir, cat, text), "added": [], "removed": []}
        _save_state(pdir, st)
    elif text.strip():
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()
        if h != st["text_hash"]:
            st["auto"] = sorted(set(st["auto"]) | set(_suggest(pdir, cat, text)))
            st["text_hash"] = h
            _save_state(pdir, st)
    always = {s for s, e in cat.items() if e["always"]}
    return (set(st["auto"]) | always | set(st["added"])) - set(st["removed"])


def filter_names(pdir, names: list[str]) -> list[str]:
    """`names` (file names in refs\\) limited to this story's active references."""
    try:
        active = active_stems(pdir)
    except (OSError, ValueError):
        return list(names)
    if active is None:
        return list(names)
    return [n for n in names if Path(n).stem in active]


def notes(pdir, names: list[str] | None = None) -> dict[str, str]:
    """{stem: 'kind - description'} for the prompt listing, for `names` when
    given. Empty when the production has no catalog."""
    if not has_catalog(pdir):
        return {}
    cat = catalog(pdir)
    stems = [Path(n).stem for n in names] if names is not None else list(cat)
    out = {}
    for s in stems:
        e = cat.get(s) or _clean_entry(s, None)
        out[s] = f"{e['description']}"
    return out


def set_manual(pdir, add=(), remove=()) -> dict:
    """The user's checkboxes: stems to force on / off. Kept across script changes."""
    st = _state(pdir) or {"text_hash": "", "auto": [], "added": [], "removed": []}
    added, removed = set(st["added"]), set(st["removed"])
    for s in add:
        added.add(s)
        removed.discard(s)
    for s in remove:
        removed.add(s)
        added.discard(s)
    st["added"], st["removed"] = sorted(added), sorted(removed)
    _save_state(pdir, st)
    return st


def overview(pdir) -> list[dict]:
    """One row per catalog entry for the Studio box."""
    cat = catalog(pdir)
    active = active_stems(pdir)
    used = used_stems(pdir)
    images = images_in(Path(pdir) / "refs")
    rows = []
    for s, e in sorted(cat.items()):
        rows.append({"stem": s, "file": images.get(s) or e["file"], "kind": e["kind"],
                     "description": e["description"], "always": e["always"],
                     "on_disk": s in images, "used": s in used,
                     "active": True if active is None else s in active})
    return rows


if __name__ == "__main__":      # python -m whisperradar.refcatalog <folder>
    import sys
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    r = scan(sys.argv[1])
    print(f"{r['total']} entries, added {len(r['added'])}"
          + (f", images missing for {', '.join(r['missing'])}" if r["missing"] else ""))
    for w in r["warnings"]:
        print("warning:", w)
