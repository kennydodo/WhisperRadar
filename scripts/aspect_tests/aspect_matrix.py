"""Single source of truth for the aspect-ratio tests.

The 2026-09 aspect work has the same motion-code -> ratio decision in three
places; this module restates them so the seed/verify scripts and the direct
Renderly test all assert against one table:

  motion code   the last "_" token of "<name>_<MOTION>.png"

  Renderly API  ImgToVideo.Cli export-batch writes out\\image-batch.json with
                an "aspect" per file. Only PL/PR are ever sent to the API
                (21:9); see RENDERLY_API_MOTIONS + renderly_engine_aspect().
  engine split  the renderly engine splits one batch across its two halves:
                PL/PR -> the paid API (21:9); everything else (ST/ZI/ZO,
                PU/PD, PV) -> the Flow Driver (16:9, or 1:1 for PU/PD). A
                quota wall on the PL/PR slice falls those through to Flow too.
  FlowBatch     whisperradar/studio.py FLOWBATCH_ASPECT_BY_MOTION: PU/PD = 1:1,
                PV = 16:9, every other motion the job default 16:9.
  Flow Driver   Renderly extension-v2/flow.js MOTION_ASPECT, clamped to
                FLOW_SUPPORTED_ASPECTS (Flow has no 21:9, so PL/PR/PV -> 16:9).

Import this from anything under scripts\\aspect_tests. No dependencies.
"""

MOTIONS = ["ST", "ZI", "ZO", "PL", "PR", "PU", "PD", "PV"]

# --- the three tables -------------------------------------------------------
# The paid API is only ever asked for the wide pans; every other motion code is
# served by the free Flow Driver on the same renderly engine.
RENDERLY_API_MOTIONS = ("PL", "PR")

RENDERLY_API_ASPECT = {
    "ST": "16:9", "ZI": "16:9", "ZO": "16:9",
    "PL": "21:9", "PR": "21:9",
    "PU": "16:9", "PD": "16:9",
    "PV": "21:9",
}

FLOWBATCH_ASPECT_BY_MOTION = {"PU": "1:1", "PD": "1:1", "PV": "16:9"}

FLOW_DRIVER_MOTION_ASPECT = {
    "PL": "21:9", "PR": "21:9", "PV": "21:9",
    "PU": "1:1", "PD": "1:1",
}
FLOW_SUPPORTED_ASPECTS = frozenset({"16:9", "4:3", "1:1", "3:4", "9:16"})

DEFAULT_ASPECT = "16:9"

# What Renderly's backend can be asked for directly (backend/config.py).
ALL_RENDERLY_ASPECTS = ("1:1", "16:9", "9:16", "4:3", "3:4", "21:9")

# Ratio pairs are only trustworthy to a couple of percent: providers round
# canvas sizes, and 21:9 vs 2.96:1 differ by ~2% by design.
RATIO_TOLERANCE = 0.04


def motion_of(file_name: str) -> str:
    """The motion code of a shotlist image name: last '_' token, no extension."""
    stem = str(file_name)
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    return stem.rsplit("_", 1)[-1]


def renderly_api_aspect(motion: str) -> str:
    return RENDERLY_API_ASPECT.get(motion, DEFAULT_ASPECT)


def flowbatch_aspect(motion: str) -> str:
    return FLOWBATCH_ASPECT_BY_MOTION.get(motion, DEFAULT_ASPECT)


def flow_driver_aspect(motion: str) -> str:
    ideal = FLOW_DRIVER_MOTION_ASPECT.get(motion, DEFAULT_ASPECT)
    return ideal if ideal in FLOW_SUPPORTED_ASPECTS else DEFAULT_ASPECT


def renderly_engine_aspect(motion: str) -> str:
    """Expected shape for a shot on engine=renderly/mode=api, where one batch is
    split across the engine's two halves: PL/PR via the paid API, the rest via
    the Flow Driver."""
    if motion in RENDERLY_API_MOTIONS:
        return renderly_api_aspect(motion)
    return flow_driver_aspect(motion)


def ratio_value(aspect: str) -> float:
    width, height = aspect.split(":")
    return int(width) / int(height)


def nearest_aspect(width: int, height: int) -> tuple[str, float]:
    """The supported ratio closest to the actual pixel shape, and its error."""
    actual = width / height
    best = min(ALL_RENDERLY_ASPECTS,
               key=lambda a: abs(ratio_value(a) - actual))
    return best, abs(ratio_value(best) - actual)


def ratios_match(aspect: str, width: int, height: int,
                 tolerance: float = RATIO_TOLERANCE) -> bool:
    return abs(ratio_value(aspect) - (width / height)) <= tolerance
