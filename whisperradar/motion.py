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


# ----------------------------------------------------- shotlist integration --
# The animated opening is planned in the SHOTLIST (not the planning brief): the
# writer gets WRITER_NOTE for the opening cues, every shot there carries an
# "ai_motion" line, a free check enforces it, and the judge also grades whether
# the image will animate well.

def _first_cue(cues_value):
    """First cue number of a shot's "cues" ("3-9", "3", 3, [3, 4])."""
    if isinstance(cues_value, (list, tuple)) and cues_value:
        return _first_cue(cues_value[0])
    text = str(cues_value or "").strip()
    head = text.replace("–", "-").split("-")[0].strip()
    return int(head) if head.isdigit() else None


def _last_cue(cues_value):
    if isinstance(cues_value, (list, tuple)) and cues_value:
        return _last_cue(cues_value[-1])
    text = str(cues_value or "").strip().replace("–", "-")
    tail = text.split("-")[-1].strip()
    return int(tail) if tail.isdigit() else None


# The opening's stills are animated into 16:9 video clips, so only the two
# zoom codes are allowed there: pans (PL/PR/PV) and tilts (PU/PD) need a
# canvas wider or taller than the video frame, and ST is not a motion at all.
OPENING_MOTIONS = ("ZI", "ZO")


def shot_motion_code(shot: dict) -> str:
    """A shot's motion code: its `motion` field, else the suffix of its asset
    file name (S01_01_SCN_ZI.png -> ZI)."""
    code = str(shot.get("motion") or "").strip().upper()
    if code:
        return code
    stem = str(shot.get("asset") or "").rsplit(".", 1)[0]
    return stem.rsplit("_", 1)[-1].upper() if "_" in stem else ""


def window_last_cue(cues: list[dict], seconds: float, to_seconds) -> int:
    """Number of the last cue that STARTS before `seconds` (0 when none).
    `cues` are {"index", "start", "end"}; `to_seconds` converts a timestamp."""
    last = 0
    for cue in cues:
        if to_seconds(cue.get("start")) < float(seconds):
            last = int(cue["index"])
        else:
            break
    return last


def writer_note(seconds: int, last_cue: int) -> str:
    """The shotlist writer's extra instructions for the animated opening."""
    return (
        "\n\nANIMATED OPENING (strict rules for cues 1-%d ONLY, about the first "
        "%d seconds of the video - the rest of the plan follows the rules "
        "above unchanged): every image in this part is turned into an AI "
        "image-to-video clip of about 8 seconds, so plan each one to ANIMATE "
        "well:\n"
        "- MOTION CODE: every shot in this part uses ZI (slow push in) or ZO "
        "(slow pull out) ONLY - in `motion` and in the asset file name. Never "
        "PL, PR, PU, PD, PV or ST here: a pan or tilt needs a wider or taller "
        "image than the 16:9 video frame, and the clip is animated from a "
        "normal 16:9 image. Alternate ZI and ZO. The rest of the plan keeps "
        "the usual motion codes.\n"
        "- One clear subject (or one small group) in a simple, open scene with "
        "visible depth - foreground, midground, background - and room around "
        "the subject for the camera to move.\n"
        "- A moment that implies motion: a pose that is about to change, wind, "
        "water, smoke, light, a crowd, a vehicle, a gesture. Not a frozen "
        "diagram.\n"
        "- NO text, numbers, charts, screens, maps, split panels, collages, "
        "reveal shots or infographics in this part: they cannot be animated "
        "and the model garbles them.\n"
        "- Keep each shot's hold to 8 seconds or less (4-8 is ideal): a clip "
        "is 8 seconds long; a longer hold is slowed down or frozen.\n"
        "- Give EVERY shot in this part an \"ai_motion\" field: one short "
        "sentence saying what moves and how the camera moves (for example "
        "\"slow push in; her hair and the curtains drift in the breeze\" or "
        "\"slow pull out; cars pass along the street\"). Describe "
        "movement only - never repeat the scene description, and keep the "
        "camera move gentle (no cuts, no sudden zoom, no new subjects "
        "appearing).\n"
        "- Shots after cue %d do NOT get \"ai_motion\"."
        % (last_cue, int(seconds), last_cue))


FIX_RULE = (
    " Shots in the animated opening (the first cues the checker names) must "
    "use ZI or ZO as the motion code (no pans, tilts or static), "
    "keep a one-sentence \"ai_motion\" field (what moves, how the camera "
    "moves) and an image that animates well: one clear subject, depth, no "
    "text, charts or collages, hold of 8 seconds or less.")

JUDGE_RULES = (
    "\n\nANIMATION CHECK: shots marked \"ANIMATED SHOT\" will be turned into an "
    "8-second AI image-to-video clip, using the planned motion shown. For "
    "those shots ALSO judge whether the image will ANIMATE well: it needs one "
    "clear subject (or small group) with space and depth around it, and a "
    "scene where the planned motion is plausible. It will NOT animate well if "
    "it relies on readable text, numbers, charts, maps or screens, is a "
    "collage or split panel, is crowded and flat with no depth, or the "
    "planned motion contradicts the scene. A shot that fails this is weak: "
    "give verdict \"weak\" and put an item starting with \"animatable:\" "
    "(plus the reason) in \"missing\". The usual completeness rules still "
    "apply to these shots.")


def window_check(data: dict, cues: list[dict], seconds: float, to_seconds,
                 cue_range=None) -> tuple[list[str], list[str], set[str]]:
    """(faults, warnings, assets in the window) for an animated opening.

    Free checks, no LLM: every shot that starts inside the window needs a
    non-empty "ai_motion"; a hold over the clip length is only a warning
    (the renderer slows it or holds the last frame)."""
    shots = [s for s in (data.get("shots") or []) if isinstance(s, dict)]
    by_index = {int(c["index"]): c for c in cues}
    limit = window_last_cue(cues, seconds, to_seconds)
    faults: list[str] = []
    warnings: list[str] = []
    assets: set[str] = set()
    missing: list[str] = []
    wrong: list[tuple[str, str]] = []
    for s in shots:
        first, last = _first_cue(s.get("cues")), _last_cue(s.get("cues"))
        if first is None or first > limit:
            continue
        asset = str(s.get("asset") or "")
        assets.add(asset)
        code = shot_motion_code(s)
        if code not in OPENING_MOTIONS:
            wrong.append((asset, code or "none"))
        if not str(s.get("ai_motion") or "").strip():
            missing.append(asset)
        a, b = by_index.get(first), by_index.get(last if last is not None else first)
        if a and b:
            hold = to_seconds(b.get("end")) - to_seconds(a.get("start"))
            if hold > MAX_STRETCH_SECONDS + 1e-6:
                warnings.append(
                    f"animated opening: {asset} holds {hold:.1f}s - longer than "
                    f"{MAX_STRETCH_SECONDS:g}s, so its clip will be slowed and "
                    f"then frozen (split it to keep it moving)")
    if wrong:
        shown = ", ".join(f"{a} ({c})" for a, c in wrong[:10]) \
            + ("..." if len(wrong) > 10 else "")
        faults.append(
            f"{len(wrong)} shot(s) in the animated opening (cues 1-{limit}) "
            f"do not use ZI or ZO: {shown}. The opening is animated from "
            f"16:9 images, so only ZI/ZO are allowed there - no PL, PR, PU, "
            f"PD, PV or ST; change the motion code and the asset file name")
    if missing:
        shown = ", ".join(missing[:10]) + ("..." if len(missing) > 10 else "")
        faults.append(
            f"{len(missing)} shot(s) in the animated opening (cues 1-{limit}) "
            f"have no \"ai_motion\" line: {shown}. Every shot there needs one "
            f"short sentence saying what moves and how the camera moves")
    return faults, warnings, assets


# ------------------------------------------------------------ motion stage --
# The MOTION stage (after IMAGES) turns each opening shot's still into one clip.

CLIPS_DIR = "clips"


def _find_image(images_dir, asset: str):
    """The rendered image for a shotlist asset (extension may differ)."""
    from pathlib import Path
    stem = Path(asset).stem.lower()
    try:
        for p in Path(images_dir).iterdir():
            if p.is_file() and p.stem.lower() == stem:
                return p
    except OSError:
        pass
    return None


def window_clips(data: dict, cues: list[dict], seconds: float, to_seconds,
                 pdir) -> list[dict]:
    """The clips the opening needs: [{asset, image, prompt, out}] in shot order.

    A shot qualifies when it starts inside the window and has an "ai_motion"
    line. `image` is None while the still is not rendered yet."""
    from pathlib import Path
    pdir = Path(pdir)
    _faults, _warnings, assets = window_check(data, cues, seconds, to_seconds)
    out, seen = [], set()
    for s in data.get("shots") or []:
        if not isinstance(s, dict):
            continue
        asset = str(s.get("asset") or "")
        prompt = str(s.get("ai_motion") or "").strip()
        if not asset or asset not in assets or not prompt \
                or asset.lower() in seen:
            continue
        seen.add(asset.lower())
        img = _find_image(pdir / "images", asset)
        out.append({"asset": asset, "image": str(img) if img else None,
                    "prompt": prompt,
                    "out": str(pdir / CLIPS_DIR / (Path(asset).stem + ".mp4"))})
    return out


def missing_clips(clips: list[dict]) -> list[dict]:
    """Clips still to generate (no mp4 on disk yet)."""
    from pathlib import Path
    return [c for c in clips if not Path(c["out"]).is_file()
            or Path(c["out"]).stat().st_size < 10_000]
