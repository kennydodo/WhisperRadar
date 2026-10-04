"""Local image upscaling: Real-ESRGAN (ncnn-vulkan) on GPU, Pillow Lanczos on CPU."""

import json
import os
import re
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from config import (
    BASE_DIR,
    DEFAULT_RESOLUTION,
    RESOLUTION_BY_LEVEL,
    RESOLUTION_PRESETS,
    UPSCALER_EXE,
)

# Tile size for the ncnn engine. Smaller tiles = less VRAM; large tiles on
# small GPUs produce corrupted output (garbage from other images overlaid).
DEFAULT_TILE = 256

DEFAULT_EXE = BASE_DIR / "tools" / "realesrgan" / "realesrgan-ncnn-vulkan.exe"
_TOOLS_DIR = BASE_DIR / "tools" / "realesrgan"
_CACHE_FILE = _TOOLS_DIR / "device_cache.json"
_PROBE_PNG = _TOOLS_DIR / "_probe.png"
_PROBE_OUT = _TOOLS_DIR / "_probe_out.png"
_NOT_INSTALLED = (
    "Upscaler engine not installed. Extract realesrgan-ncnn-vulkan to "
    "backend/tools/realesrgan/ (or set UPSCALER_EXE in .env)."
)

_PROBE_SIZE = 64

# The batch route handles several /generate requests at once, and each one
# upscales its own image. Running several engines on one small GPU at the same
# time makes them crash or emit garbage, and concurrent first-run probes share
# one probe file, so every probe "fails" and the machine gets cached as
# CPU-only. One engine process at a time.
_ENGINE_LOCK = threading.RLock()


def _suppress_crash_dialogs() -> None:
    """Stop Windows popping "Application Error" when the engine crashes.

    realesrgan-ncnn-vulkan dereferences null when a Vulkan device/driver is
    missing. Without this, Windows shows a modal dialog that nobody clicks, the
    subprocess never exits and the whole batch hangs. The error mode is
    inherited by child processes.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except Exception:
        pass


_suppress_crash_dialogs()
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _write_probe_png(path: Path) -> None:
    """A deterministic gradient, generated rather than shipped.

    A 10x10 source gives _content_ok too little detail, so a perfect GPU result
    is rejected as "not matching" and the upscaler silently falls back to CPU.
    """
    from PIL import Image

    image = Image.new("RGB", (_PROBE_SIZE, _PROBE_SIZE))
    image.putdata(
        [
            ((x * 4) % 256, (y * 4) % 256, ((x + y) * 2) % 256)
            for y in range(_PROBE_SIZE)
            for x in range(_PROBE_SIZE)
        ]
    )
    image.save(path, "PNG")

_RECACHE_AFTER_SECONDS = 3600


def exe_path() -> Path:
    override = UPSCALER_EXE.strip()
    return Path(override) if override else DEFAULT_EXE


def is_available() -> bool:
    return exe_path().exists()


def info() -> dict:
    cache = _load_cache()
    return {
        "available": is_available(),
        "exe": str(exe_path()),
        "device": cache.get("name", "auto-detect on first use"),
        "device_kind": cache.get("kind", "auto"),
    }


def png_dimensions(path: Path) -> tuple[int, int]:
    with open(path, "rb") as f:
        header = f.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError("Not a PNG file")
    width, height = struct.unpack(">II", header[16:24])
    return width, height


def _image_dimensions(path: Path) -> tuple[int, int]:
    try:
        return png_dimensions(path)
    except Exception:
        from PIL import Image

        with Image.open(path) as img:
            return img.size


def classify_size(width: int, height: int) -> str:
    """Name the resolution tier an image already sits at (by its short side)."""
    short_side = min(width, height)
    if short_side >= 1900:
        return "4K"
    if short_side >= 1260:
        return "2K"
    if short_side >= 1000:
        return "HD"
    return "1K"


def target_dimensions(tier: str, width: int, height: int) -> tuple[int, int]:
    """Exact output size for a tier, keeping the source aspect ratio.

    The presets are 16:9, so a source that already matches that ratio (or its
    portrait flip) snaps to the preset exactly; every other ratio is scaled so
    its short side matches the preset's.
    """
    preset_w, preset_h = RESOLUTION_PRESETS[tier]
    ratio = width / height
    if abs(ratio - preset_w / preset_h) <= 0.03 * (preset_w / preset_h):
        return preset_w, preset_h
    if abs(ratio - preset_h / preset_w) <= 0.03 * (preset_h / preset_w):
        return preset_h, preset_w
    scale = min(preset_w, preset_h) / min(width, height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def level_to_tier(level) -> str | None:
    """Map the saved upscale_level setting (0 = off) to a tier name."""
    try:
        return RESOLUTION_BY_LEVEL.get(int(level))
    except (TypeError, ValueError):
        return None


# Tier names used before the rename to ImgToVideo's vocabulary.
_LEGACY_TIER_NAMES = {"1K": "HD"}


def resolve_tier(tier: str | None, scale: int | None = None) -> str:
    """Normalize an API request to a tier name (HD / 2K / 4K).

    `scale` is the legacy 2x/4x request; it maps onto the nearest tier so older
    callers (ImgToVideo.ImageGen, WhisperRadar, the driver) keep working.
    0 / "off" is rejected with a clear error instead of silently falling back to
    the default tier: a caller that does not want an upscale must not call the
    upscale endpoint at all (WhisperRadar's upscale tier 0 means off).
    """
    if tier in RESOLUTION_PRESETS:
        return tier
    if isinstance(tier, str) and tier in _LEGACY_TIER_NAMES:
        return _LEGACY_TIER_NAMES[tier]
    try:
        scale_value = int(scale or 0)
    except (TypeError, ValueError):
        scale_value = 0
    if scale_value <= 0:
        raise ValueError(
            "Upscale scale must be 2 or 4 (0 = off: skip the upscale call)."
        )
    return {1: "HD", 2: "2K", 3: "2K", 4: "4K"}.get(scale_value, DEFAULT_RESOLUTION)


def _load_cache() -> dict:
    try:
        return json.loads(_CACHE_FILE.read_text())
    except Exception:
        return {}


def _save_cache(data: dict) -> None:
    try:
        _TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(data))
    except Exception:
        pass


def _usable_icd_files(tools_dir: Path | None = None) -> list[str]:
    """ICD files shipped with the engine whose driver exists on THIS machine.

    nv-vk64.json / igvk64.json carry absolute DriverStore paths, so a checkout
    copied from another machine names drivers that are not installed. Forcing
    those into VK_DRIVER_FILES makes vkCreateInstance fail (-9) and every probe
    look like a dead GPU, so unusable entries are skipped instead of trusted.
    """
    directory = tools_dir or _TOOLS_DIR
    usable: list[str] = []
    for path in sorted(directory.glob("*.json")):
        if path.name == "device_cache.json":
            continue
        try:
            library = json.loads(path.read_text(encoding="utf-8"))["ICD"]["library_path"]
        except Exception:
            continue
        if isinstance(library, str) and Path(library).exists():
            usable.append(str(path))
    return usable


def _icd_env() -> dict:
    """Point the Vulkan loader at locally written ICD files (NVIDIA + Intel)."""
    files = _usable_icd_files()
    if not files:
        return {}
    value = ";".join(files)
    return {"VK_DRIVER_FILES": value, "VK_ICD_FILENAMES": value}


def _tile_size() -> int:
    override = os.getenv("UPSCALER_TILE", "").strip()
    if override.isdigit() and int(override) > 0:
        return int(override)
    return DEFAULT_TILE


def _flatten_to_rgb(src: Path, work_dir: Path) -> Path:
    """Re-encode as plain RGB PNG - the engine corrupts alpha-channel inputs."""
    from PIL import Image

    out = work_dir / f"rgb_{src.stem}_{int(time.time())}.png"
    with Image.open(src) as img:
        img.convert("RGB").save(out, "PNG")
    return out


def _run(exe: Path, src: Path, dst: Path, gpu: int | None):
    """Run the engine at its native 4x (always-clean path on this setup)."""
    cmd = [
        str(exe),
        "-i",
        str(src),
        "-o",
        str(dst),
        "-n",
        "realesrgan-x4plus",
        "-s",
        "4",
        "-t",
        str(_tile_size()),
        "-f",
        "png",
    ]
    if gpu is not None:
        cmd += ["-g", str(gpu)]
    env = os.environ.copy()
    env.update(_icd_env())
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=1800,
        env=env,
        creationflags=_CREATE_NO_WINDOW,
    )


def _device_name(result, gpu: int | None) -> str:
    text = f"{result.stdout}\n{result.stderr}"
    if gpu is not None:
        matches = re.findall(rf"\[{gpu}\s+([^\]]+)\]", text)
        if matches:
            return matches[0].strip()
    matches = re.findall(r"\[\d+\s+([^\]]+)\]", text)
    return matches[-1].strip() if matches else "Vulkan device"


def _content_ok(src: Path, out_path: Path, factor: int, threshold: float = 60.0) -> bool:
    """Reject engine output whose downscaled content doesn't match the source.

    Corrupt GPU tiles (VRAM overruns, driver fence errors) produce output that
    looks nothing like the input - detect that by mean absolute deviation.
    """
    from PIL import Image

    try:
        source = Image.open(src).convert("RGB")
        output = Image.open(out_path).convert("RGB")
        if output.width < source.width or output.height < source.height:
            return False
        matched = output.resize(source.size, Image.LANCZOS)
        a = list(source.resize((64, 64)).getdata())
        b = list(matched.resize((64, 64)).getdata())
        flat_a = [c for p in a for c in p]
        flat_b = [c for p in b for c in p]
        mad = sum(abs(x - y) for x, y in zip(flat_a, flat_b)) / len(flat_a)
        _ = factor
        return mad <= threshold
    except Exception:
        return False


def _probe_devices(exe: Path) -> dict[int, str]:
    """Probe Vulkan device indices 0..5; return {index: device_name} for working ones."""
    _write_probe_png(_PROBE_PNG)
    devices: dict[int, str] = {}
    misses = 0
    for idx in range(6):
        # Device indices are contiguous: after two dead ones in a row there is
        # nothing further to find, and each dead index is an engine crash.
        if misses >= 2:
            break
        if _PROBE_OUT.exists():
            _PROBE_OUT.unlink()
        try:
            result = _run(exe, _PROBE_PNG, _PROBE_OUT, idx)
            if _PROBE_OUT.exists() and _content_ok(_PROBE_PNG, _PROBE_OUT, 4):
                devices[idx] = _device_name(result, idx)
                misses = 0
            else:
                misses += 1
        except Exception:
            misses += 1
            continue
    if _PROBE_OUT.exists():
        _PROBE_OUT.unlink()
    return devices


def _pick_gpu(exe: Path) -> int | None:
    """Pick a GPU index (NVIDIA first). Returns None when no Vulkan GPU works."""
    devices = _probe_devices(exe)
    if not devices:
        _save_cache({"device": "cpu", "name": "Pillow Lanczos (CPU)", "kind": "cpu", "ts": time.time()})
        return None
    nvidia = next((i for i, n in devices.items() if "nvidia" in n.lower()), None)
    chosen = nvidia if nvidia is not None else min(devices)
    kind = "nvidia" if nvidia is not None else "vulkan"
    _save_cache({"device": chosen, "name": devices[chosen], "kind": kind, "ts": time.time()})
    return chosen


def _cached_gpu(exe: Path) -> int | None:
    cache = _load_cache()
    if not cache:
        return _pick_gpu(exe)
    device = cache.get("device")
    if device is None or isinstance(device, str):
        # Cached as CPU (or unknown) - re-probe occasionally in case a GPU appeared
        if time.time() - cache.get("ts", 0) > _RECACHE_AFTER_SECONDS:
            return _pick_gpu(exe)
        return None
    return int(device)


def _resize(src: Path, dst: Path, size: tuple[int, int]) -> tuple[int, int]:
    from PIL import Image

    with Image.open(src) as img:
        result = img.convert("RGB").resize(size, Image.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        result.save(dst, "PNG")
    return png_dimensions(dst)


def upscale(src: Path, dst: Path, tier: str) -> tuple[int, int]:
    """Upscale src -> dst (PNG) to a resolution tier: HD (1920x1080),
    2K (2560x1440) or 4K (3840x2160) at 16:9, scaled to the source ratio.

    NVIDIA GPU preferred, then any Vulkan GPU, then CPU. The engine always runs
    at its native 4x (the -s 2 path produces corrupt tiles on this setup); the
    result is then Lanczos-resized to the exact target size.
    """
    exe = exe_path()
    if not exe.exists():
        raise RuntimeError(_NOT_INSTALLED)
    if tier not in RESOLUTION_PRESETS:
        raise ValueError(f"Unknown resolution tier '{tier}' - use HD, 2K or 4K")

    width, height = _image_dimensions(src)
    target_w, target_h = target_dimensions(tier, width, height)
    dst.parent.mkdir(parents=True, exist_ok=True)

    # Already at or above the target - a plain Lanczos resize is enough.
    if width >= target_w and height >= target_h:
        return _resize(src, dst, (target_w, target_h))

    with _ENGINE_LOCK, tempfile.TemporaryDirectory(prefix="renderly_upscale_") as tmp:
        flat = _flatten_to_rgb(src, Path(tmp))
        out_tmp = Path(tmp) / "engine_out.png"

        gpu = _cached_gpu(exe)
        if gpu is None:
            gpu = _pick_gpu(exe)

        candidates: list[int | None] = []
        if gpu is not None:
            candidates.append(gpu)
        candidates.append(None)  # engine's own auto device choice as second chance

        for candidate in candidates:
            try:
                result = _run(exe, flat, out_tmp, candidate)
                if not out_tmp.exists():
                    raise RuntimeError("engine produced no output")
                if not _content_ok(flat, out_tmp, 4):
                    # Garbage tiles (VRAM overrun etc.) - reject this device
                    if candidate is not None:
                        try:
                            _CACHE_FILE.unlink()
                        except Exception:
                            pass
                    continue

                dims = _resize(out_tmp, dst, (target_w, target_h))
                if candidate is not None:
                    cache = _load_cache()
                    if cache.get("device") != candidate:
                        _save_cache(
                            {
                                "device": candidate,
                                "name": _device_name(result, candidate),
                                "kind": cache.get("kind", "vulkan"),
                                "ts": time.time(),
                            }
                        )
                return dims
            except ValueError:
                raise
            except Exception:
                pass
            if candidate is not None:
                try:
                    _CACHE_FILE.unlink()
                except Exception:
                    pass

    # No working GPU - CPU fallback
    dims = _resize(src, dst, (target_w, target_h))
    _save_cache({"device": "cpu", "name": "Pillow Lanczos (CPU)", "kind": "cpu", "ts": time.time()})
    return dims


def engine_label(tier: str) -> str:
    cache = _load_cache()
    if cache.get("kind") == "cpu":
        return f"lanczos (cpu, {tier})"
    return f"realesrgan-x4 ({cache.get('name', 'GPU')}, {tier})"
