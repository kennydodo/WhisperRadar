"""Port of SoundEffectLibrary.cs, SoundEffectResolver.cs and SoundCatalog.cs."""
from __future__ import annotations

import math
import os
from typing import Callable, Dict, List, Optional, Tuple

from . import models as m
from . import netfmt
from .ffmpeg_runner import FfmpegRunner
from .ffprobe import ffprobe_duration_seconds
from .options import DEFAULT_POP_ID, POP_VARIANTS, is_pop_variant

POP_ALIAS = "pop"
PROJECT_FOLDER_NAME = "sfx"
LONG_SOUND_SECONDS = 3.0
EXTENSIONS = (".wav", ".mp3", ".m4a", ".ogg", ".flac")


def _ae(expr: str, d: str) -> str:
    return f"aevalsrc='{expr}':d={d}:s=48000"


# name -> (lavfi source, audio filter); insertion order preserved
BUILT_IN: Dict[str, Tuple[str, str]] = {
    "pop_classic": (_ae("0.8*sin(2*PI*(1100*t-3000*t*t))*exp(-16*t)", "0.3"), "volume=-1.0dB"),
    "pop_bubble": (_ae("0.8*sin(2*PI*(350*t+3500*t*t))*exp(-45*t)", "0.15"), "afade=t=in:d=0.002,volume=-0.2dB"),
    "pop_boop": (_ae("0.7*sin(2*PI*(480*t-400*t*t))*exp(-22*t)", "0.25"), "afade=t=in:d=0.004,lowpass=f=2500,volume=1.0dB"),
    "pop_tap": (_ae("0.8*sin(2*PI*700*t)*exp(-60*t)+0.35*sin(2*PI*1750*t)*exp(-90*t)+0.3*(random(0)*2-1)*exp(-400*t)", "0.15"), "lowpass=f=5000,volume=-2.9dB"),
    "pop_marimba": (_ae("0.6*sin(2*PI*784*t)*exp(-14*t)+0.25*sin(2*PI*3136*t)*exp(-40*t)", "0.4"), "afade=t=in:d=0.002,volume=-0.8dB"),
    "pop_cork": (_ae("0.8*sin(2*PI*(900*t-5500*t*t))*exp(-50*t)+0.3*(random(0)*2-1)*exp(-250*t)", "0.15"), "lowpass=f=4500,afade=t=in:d=0.001,volume=-1.0dB"),
    "pop_blip": (_ae("0.8*sin(2*PI*1000*t)*exp(-70*t)", "0.1"), "afade=t=in:d=0.001,volume=-0.3dB"),
    "pop_ping": (_ae("0.55*sin(2*PI*1760*t)*exp(-18*t)+0.25*sin(2*PI*2637*t)*exp(-25*t)", "0.5"), "afade=t=in:d=0.002,volume=-0.3dB"),
    "pop_drop": (_ae("0.8*sin(2*PI*(1400*t-2800*t*t))*exp(-30*t)", "0.2"), "afade=t=in:d=0.002,lowpass=f=4000,volume=-0.4dB"),
    "pop_thump": (_ae("0.9*sin(2*PI*(220*t-500*t*t))*exp(-18*t)", "0.25"), "afade=t=in:d=0.003,volume=-1.5dB"),
    "pop_snap": (_ae("(random(0)*2-1)*exp(-180*t)+0.4*sin(2*PI*1300*t)*exp(-120*t)", "0.1"), "highpass=f=1800,lowpass=f=9000,volume=0.1dB"),
    "pop_pluck": (_ae("0.55*sin(2*PI*440*t)*exp(-20*t)+0.28*sin(2*PI*880*t)*exp(-26*t)+0.18*sin(2*PI*1320*t)*exp(-32*t)", "0.3"), "afade=t=in:d=0.002,volume=-0.6dB"),
    "pop_sparkle": (_ae("0.6*sin(2*PI*1568*t)*exp(-40*t)+if(gt(t,0.06),0.6*sin(2*PI*2093*t)*exp(-40*(t-0.06)),0)", "0.3"), "afade=t=in:d=0.001,volume=1.1dB"),
    "ding": (_ae("0.5*sin(2*PI*1568*t)*exp(-5*t)+0.25*sin(2*PI*2349*t)*exp(-7*t)", "1"), "anull"),
    "click": (_ae("(random(0)*2-1)*exp(-120*t)", "0.1"), "lowpass=f=6000,highpass=f=800,volume=1.5"),
    "tick": (_ae("0.7*sin(2*PI*2200*t)*exp(-45*t)", "0.12"), "anull"),
    "whoosh": ("anoisesrc=d=0.7:c=pink:r=48000:a=1.0",
               "highpass=f=300,lowpass=f=5000,afade=t=in:d=0.3,afade=t=out:st=0.3:d=0.4,volume=8dB"),
    "swipe": ("anoisesrc=d=0.45:c=white:r=48000:a=1.0",
              "highpass=f=1500,lowpass=f=7000,afade=t=in:d=0.12,afade=t=out:st=0.15:d=0.3,volume=-2dB"),
}
_BUILT_IN_LOWER = {k.lower(): v for k, v in BUILT_IN.items()}


def built_in_names() -> List[str]:
    non_pop = [k for k in BUILT_IN if not k.lower().startswith("pop_")]
    pops = [k for k in BUILT_IN if k.lower().startswith("pop_")]
    return non_pop + [POP_ALIAS] + pops


def resolve_alias(name: str, default_pop: Optional[str]) -> str:
    if name.lower() != POP_ALIAS:
        return name
    return default_pop if is_pop_variant(default_pop) else DEFAULT_POP_ID


def find_project_file(project_dir: str, name: str) -> Optional[str]:
    folder = os.path.join(project_dir, PROJECT_FOLDER_NAME)
    if not os.path.isdir(folder):
        return None
    direct = os.path.join(folder, name)
    if os.path.splitext(name)[1] and os.path.isfile(direct):
        return direct
    for ext in EXTENSIONS:
        cand = os.path.join(folder, name + ext)
        if os.path.isfile(cand):
            return cand
    return None


def is_built_in(name: str) -> bool:
    return name.lower() == POP_ALIAS or name.lower() in _BUILT_IN_LOWER


def build_arguments(name: str, output_path: str, default_pop: Optional[str] = None) -> Optional[List[str]]:
    recipe = _BUILT_IN_LOWER.get(resolve_alias(name, default_pop).lower())
    if recipe is None:
        return None
    return ["-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", recipe[0],
            "-af", recipe[1], "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", output_path]


def ensure_built_in(project_dir: str, name: str, runner: FfmpegRunner,
                    default_pop: Optional[str] = None, cancel_event=None) -> Optional[str]:
    name = resolve_alias(name, default_pop)
    if name.lower() not in _BUILT_IN_LOWER:
        return None
    path = os.path.join(project_dir, "out", "sfx", name.lower() + ".wav")
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    result = runner.run(build_arguments(name, path), cancel_event=cancel_event)
    return path if result.success and os.path.isfile(path) else None


def resolve(timeline: m.Timeline, project_dir: str, runner: FfmpegRunner,
            ffprobe_path: str = "ffprobe", default_pop: Optional[str] = None,
            probe: Optional[Callable[[str], float]] = None, cancel_event=None) -> List[m.ValidationIssue]:
    """Fill sound file_path/duration_frames; drop unfound sounds. Returns issues."""
    issues: List[m.ValidationIssue] = []
    if not timeline.sounds:
        return issues
    if probe is None:
        def probe(p: str) -> float:
            return ffprobe_duration_seconds(p, ffprobe_path, cancel_event)
    files: Dict[str, Optional[Tuple[str, int]]] = {}
    kept = []
    for sound in timeline.sounds:
        key = sound.name.lower()
        if key not in files:
            files[key] = _resolve_one(sound.name, timeline.fps, project_dir, runner, probe,
                                      default_pop, issues, cancel_event)
        found = files[key]
        if found is None:
            continue
        sound.file_path, sound.duration_frames = found
        kept.append(sound)
    timeline.sounds[:] = kept
    return issues


def _resolve_one(name, fps, project_dir, runner, probe, default_pop, issues, cancel_event):
    path = (find_project_file(project_dir, name)
            or ensure_built_in(project_dir, name, runner, default_pop, cancel_event))
    if path is None:
        issues.append(m.ValidationIssue(
            m.WARNING, "SFX_UNKNOWN",
            f'Sound "{name}" was not found: put {name}.wav (or .mp3) in the project\'s sfx folder, or use one of '
            f'the built-in sounds ({", ".join(built_in_names())}). The sound was skipped.'))
        return None
    try:
        seconds = probe(path)
    except (RuntimeError, OSError) as e:
        issues.append(m.ValidationIssue(
            m.WARNING, "SFX_UNREADABLE",
            f'Sound "{name}" ({os.path.basename(path)}) could not be read: {e} The sound was skipped.'))
        return None
    if seconds > LONG_SOUND_SECONDS:
        issues.append(m.ValidationIssue(
            m.INFO, "SFX_LONG",
            f'Sound "{name}" is {netfmt.optional(seconds, 1)} s long; sound effects are meant to be about a second.'))
    return (path, max(1, int(math.ceil(seconds * fps))))
