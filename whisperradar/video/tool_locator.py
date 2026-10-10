"""Port of ToolLocator.cs: find ffmpeg/ffprobe binaries."""
from __future__ import annotations

import os
from typing import Iterator, List


def _is_rooted(p: str) -> bool:
    if os.path.isabs(p):
        return True
    # Windows drive paths (C:\x) are "rooted" for the C# locator on any OS.
    return len(p) >= 3 and p[1] == ":" and p[2] in "\\/" and p[0].isalpha()


def _executable_names(configured_value: str) -> List[str]:
    """C# always appends .exe; on non-Windows hosts also try the bare name."""
    if configured_value.lower().endswith(".exe"):
        names = [configured_value]
    else:
        names = [configured_value + ".exe"]
    if os.name != "nt":
        names.append(configured_value[:-4] if configured_value.lower().endswith(".exe") else configured_value)
    return names


def _path_directories() -> Iterator[str]:
    seen = set()
    raw = os.environ.get("PATH", "")
    for d in raw.split(os.pathsep):
        d = d.strip()
        if d and d.lower() not in seen:
            seen.add(d.lower())
            yield d
    for d in _fallback_directories():
        if d.lower() not in seen:
            seen.add(d.lower())
            yield d


def _fallback_directories() -> Iterator[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    yield here
    yield os.path.join(here, "ffmpeg")
    yield os.path.join(here, "tools", "ffmpeg")
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        yield os.path.join(local, "Microsoft", "WinGet", "Links")
        packages = os.path.join(local, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(packages):
            for root, _dirs, files in os.walk(packages):
                for f in files:
                    if f.lower().endswith(".exe"):
                        yield root
                        break
    if os.name == "nt":
        yield "C:\\ProgramData\\chocolatey\\bin"
        yield "C:\\ffmpeg\\bin"
        tools = "C:\\Tools"
        if os.path.isdir(tools):
            try:
                subs = [os.path.join(tools, s) for s in os.listdir(tools)]
            except OSError:
                subs = []
            for sub in subs:
                if os.path.isdir(sub):
                    yield sub
                    b = os.path.join(sub, "bin")
                    if os.path.isdir(b):
                        yield b


def resolve(tool_name: str, configured: str = "") -> str:
    value = tool_name if not configured or not configured.strip() else configured.strip()
    if _is_rooted(value):
        return value
    for directory in _path_directories():
        for name in _executable_names(value):
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    return value


def resolve_all_candidates(tool_name: str, configured: str = "") -> List[str]:
    value = tool_name if not configured or not configured.strip() else configured.strip()
    results: List[str] = []
    seen = set()

    def add(p: str) -> None:
        k = p.lower()
        if k not in seen:
            seen.add(k)
            results.append(p)

    if _is_rooted(value) and os.path.isfile(value):
        add(value)
    if not _is_rooted(value):
        for directory in _path_directories():
            for name in _executable_names(value):
                candidate = os.path.join(directory, name)
                if os.path.isfile(candidate):
                    add(candidate)
    return results
