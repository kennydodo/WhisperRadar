"""Helpers for the ImgToVideo golden tests: build a throw-away project folder from a fixture case."""
import json
import os
import shutil
import struct
import tempfile
import zlib

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "video")


def png_header(width, height, bit_depth=8, color_type=2):
    """PNG signature + IHDR chunk (enough for imaging.try_read and the PNG format check)."""
    ihdr = struct.pack(">iiBBBBB", width, height, bit_depth, color_type, 0, 0, 0)
    crc = zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", crc)


def write_image(folder, name, width, height, **kw):
    os.makedirs(os.path.join(folder, "images"), exist_ok=True)
    path = os.path.join(folder, "images", name)
    with open(path, "wb") as f:
        f.write(png_header(width, height, **kw))
    return path


def write_audio(folder, name="narration.mp3"):
    os.makedirs(os.path.join(folder, "audio"), exist_ok=True)
    path = os.path.join(folder, "audio", name)
    with open(path, "wb") as f:
        f.write(b"\0" * 16)
    return path


def case_dir(case):
    return os.path.join(FIXTURES, case)


def load_json(case, name):
    with open(os.path.join(case_dir(case), name), encoding="utf-8") as f:
        return json.load(f)


def make_project(case, parent, folder_name=None):
    """Create <parent>/<folder_name or case>/ from the fixture inputs; returns the folder."""
    folder = os.path.join(parent, folder_name or case)
    os.makedirs(folder)
    for name in ("shotlist.json", "imgtovideo.json", "visual_manifest.json", "subtitles.srt"):
        src = os.path.join(case_dir(case), name)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(folder, name))
    for name, (w, h) in load_json(case, "images.json").items():
        write_image(folder, name, w, h)
    write_audio(folder)
    return folder


def base_name(path):
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def normalize_paths(node):
    """Replace every path-like 'file'/'file_path'/'source_path'/'sources' value by its file name."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in ("file", "file_path", "source_path") and isinstance(v, str):
                out[k] = base_name(v)
            elif k == "sources" and isinstance(v, list):
                out[k] = [base_name(x) for x in v]
            elif k == "audio_file" and isinstance(v, str):
                out[k] = base_name(v)
            else:
                out[k] = normalize_paths(v)
        return out
    if isinstance(node, list):
        return [normalize_paths(x) for x in node]
    return node


def first_difference(a, b, path="$"):
    """None when equal (numbers compared by value), else a description of the first difference."""
    if isinstance(a, bool) or isinstance(b, bool):
        return None if a == b else f"{path}: {a!r} != {b!r}"
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return None if abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b)) else f"{path}: {a!r} != {b!r}"
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                return f"{path}.{k}: missing in actual (expected {b[k]!r})"
            if k not in b:
                return f"{path}.{k}: unexpected in actual ({a[k]!r})"
            d = first_difference(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{path}: length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_difference(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    return None if a == b else f"{path}: {a!r} != {b!r}"


# ---------------------------------------------------------------- TempProject (TestSupport.cs)
SAMPLE_SRT = """1
00:00:00,000 --> 00:00:05,000
First line one.

2
00:00:05,200 --> 00:00:09,500
First line two.

3
00:00:10,200 --> 00:00:15,000
Second line one.

4
00:00:15,200 --> 00:00:19,000
Second line two.
"""


class TempProject:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="imgtovideo-tests-")
        self.path = self._tmp.name
        os.makedirs(os.path.join(self.path, "images"))

    def write_image(self, name, width, height):
        return write_image(self.path, name, width, height)

    def write_file(self, relative_path, content):
        full = os.path.join(self.path, relative_path.replace("\\", os.sep))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return full

    def write_audio(self):
        self.write_file(os.path.join("audio", "narration.mp3"), "fake audio bytes - not probed by unit tests")

    def write_srt(self):
        self.write_file("narration.srt", SAMPLE_SRT)

    def cleanup(self):
        self._tmp.cleanup()
