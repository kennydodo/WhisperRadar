"""Animated opening: the first N seconds of a video become AI image-to-video clips.

Per channel only (a channel that does not switch it on is untouched):
  animate_enabled  0/1   (NULL = off)
  animate_seconds  30-180 (NULL = DEFAULT_SECONDS)
  animate_model    one of MODELS (NULL = DEFAULT_MODEL)

Flow's Veo clips are a fixed 8 seconds at 720p, while a shot lasts 4-10 s, so
`fit` decides how a clip fills its shot:
  shot <= CLIP_SECONDS          -> use the first `shot` seconds of the clip
  CLIP_SECONDS < shot <= MAX_STRETCH_SECONDS
                                -> play the whole clip slowed to the shot length
                                   (never below MIN_SPEED)
  shot > MAX_STRETCH_SECONDS    -> play the clip at MIN_SPEED, then hold its
                                   last frame for the rest (slow zoom on the hold)
"""
from __future__ import annotations

MODELS = ["Veo 3.1 - Lite", "Veo 3.1 - Fast"]
DEFAULT_MODEL = MODELS[0]
MIN_SECONDS = 30
MAX_SECONDS = 180
DEFAULT_SECONDS = 60

CLIP_SECONDS = 8.0
MIN_SPEED = 0.8                       # slowest playback that still looks natural
MAX_STRETCH_SECONDS = CLIP_SECONDS / MIN_SPEED   # 10.0


def clamp_seconds(value) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return DEFAULT_SECONDS
    return max(MIN_SECONDS, min(MAX_SECONDS, n))


def effective(own) -> dict:
    """The channel's animated-opening settings, with defaults filled in."""
    def get(key):
        try:
            return own[key]
        except (IndexError, KeyError, TypeError):
            return None

    model = get("animate_model")
    return {
        "animate_enabled": bool(get("animate_enabled")),
        "animate_seconds": clamp_seconds(get("animate_seconds")
                                         if get("animate_seconds") not in (None, "")
                                         else DEFAULT_SECONDS),
        "animate_model": model if model in MODELS else DEFAULT_MODEL,
    }


def window_shots(shots: list[dict], seconds: float) -> list[dict]:
    """The shots that make up the animated opening.

    `shots` need `start` and `end` (seconds, in order). A shot is included when
    it starts before `seconds`; the last one is kept whole, so the window ends on
    a shot boundary at or just after the requested length."""
    out = []
    for shot in shots:
        if float(shot["start"]) < float(seconds):
            out.append(shot)
        else:
            break
    return out


def fit(shot_seconds: float) -> dict:
    """How a fixed-length clip fills a shot of `shot_seconds`.

    Returns {"play": seconds of clip used, "speed": playback speed,
             "hold": seconds of last-frame hold after the clip}."""
    shot = max(0.1, float(shot_seconds))
    if shot <= CLIP_SECONDS:
        return {"play": round(shot, 3), "speed": 1.0, "hold": 0.0}
    if shot <= MAX_STRETCH_SECONDS + 1e-9:
        return {"play": CLIP_SECONDS, "speed": round(CLIP_SECONDS / shot, 4), "hold": 0.0}
    played = CLIP_SECONDS / MIN_SPEED
    return {"play": CLIP_SECONDS, "speed": MIN_SPEED,
            "hold": round(shot - played, 3)}
