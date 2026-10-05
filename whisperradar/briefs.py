"""Per-channel shotlist-planning profiles (motion policy + presentation).

The manifest-authoring brief (``brief_template.md``) is a template. Everything
in it that depends on HOW a channel's videos are paced and staged is a named
slot - the motion table, the ST / motion-share / hold rules, the canvas sizes
and the checklist lines - and a channel picks a *motion profile* that fills
those slots. A free-text *presentation* block (who is on screen and when:
a host who opens and closes the video, example characters in the middle, a
narrator throughout, no narrator at all...) fills the ``{{PRESENTATION}}``
slot, which renders to nothing when it is empty.

The same profile drives the review gates (``studio.shotlist_pacing``), so the
prompt and the checks can never disagree about what a channel allows.

The "standard" profile is the wording and limits the brief has always had: a
channel with nothing set plans exactly as before.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

TEMPLATE_PATH = Path(__file__).with_name("brief_template.md")

# ---- the standard limits (what the review gates have always enforced) ------
# ST is only for ~1-2-cue shots (~2-4s) and ~10% of shots; no motion code above
# ~40% (PL/PR tighter, at ~10% - they default too easily); a static hold of
# 15s+ is never acceptable.
ST_MAX_HOLD_SECONDS = 5.0
ST_MAX_SHARE = 0.10
MOTION_MAX_SHARE = 0.40
MOTION_MAX_SHARE_OVERRIDES = {"PL": 0.10, "PR": 0.10}
STATIC_LONG_HOLD_SECONDS = 15.0
# On a channel with a minimum hold, more than this share of shots (the last
# one is exempt - it ends where the narration ends) holding under the minimum
# is a fault: the plan is chopping the narration up instead of grouping ideas.
MIN_HOLD_MAX_SHORT_SHARE = 0.35

# ---- the motion MIX (all profiles) -------------------------------------------
# The share limits above are CAPS, and a plan that never uses a capped code
# (no ST, no PU/PD, nothing but ZI) satisfies every one of them. So the mix is
# also a target: a code with a stated share is used at least MIX_FLOOR_RATIO of
# it, and when both zoom codes are allowed ZI takes ZOOM_SPLIT of the ZI+ZO
# shots (the other zoom takes the rest). Small plans are exempt: a handful of
# shots cannot show a mix.
ZOOM_SPLIT = (0.40, 0.60)
MIX_FLOOR_RATIO = 0.5
MIX_MIN_SHOTS = 20
MIX_MIN_ZOOM_SHOTS = 10

# ---- slot texts: the STANDARD preset, extracted verbatim from the brief ----
# (tests/test_brief_profiles.py proves the default render equals the
# original brief, so these cannot drift from it unnoticed)
STANDARD_SLOTS: dict[str, str] = {
    "HOLD_RULE": (
        "A hold that runs long (roughly 15 seconds or more) is acceptable only when it carries motion — a long hold on a static frame is never acceptable (Section 7)."),
    "MOTION_SECTION": """\
### Motion is assigned with composition, not after it

ST (static) is the exception, not the default. Every other shot carries motion chosen while designing the image — the motion code determines the canvas and the subject placement, so it can never be stamped on afterward. A shot composed as a left/right comparison has no horizontal room to pan; applying PR to it crops a panel off. Motion and composition must agree.

| Visual | Motion | Why |
|---|---|---|
| INF diagram | ZI | push into the mechanism |
| INF wide comparison | ZO | pull back to reveal the full span |
| CMP left/right split | PR / PL | the pan performs the comparison — reveal A, then B |
| CMP top/bottom split | PU / PD | same logic, vertical |
| SCN character beat | ZI | pulls the viewer inward |
| SCN environment | PL / PR | establishes space |
| CU detail | ZO | short hold, pulls out to context |
| HYB scene + graphics | ZI | scene first, graphics revealed by the push |
| PROC stages | PR | pan through the sequence |
| OVR wide concept | PV | panoramic reveal across the whole idea |

Motion must vary. A video where every shot is ZI has the same problem as one where every shot is ST — motion stops reading as expressive and becomes wallpaper. No single motion code above ~40% of shots. PL and PR specifically are capped tighter, at 10% each: they are the easiest motions to reach for by default, and a plan that leans on them flattens into a side-scroll instead of varying with the composition table above.""",
    "MOTION_FIELD_RULE": (
        "- `motion`: ST | ZI | ZO | PL | PR | PU | PD | PV — **must match the motion code in the asset's own filename** (the image was composed for that motion and overscan). Deviate only with a deliberate reason. **ST is rare and special:** use it only on very short holds — roughly 1–2 cues (~2–4 seconds) — and cap it at ~10% of shots, reserving it for compositions that cannot tolerate overscan (tight symmetric close-ups, exact diagrams). Every shot that holds longer must carry motion. Assign motion with composition per the table in Section 5 and vary the codes — no single motion code above ~40% of shots, and PL/PR each capped at 10% of shots (they default too easily; lean on ZI/ZO/PU/PD/PV for the rest of the variety)."),
    "CANVAS_SPEC": (
        "ST/ZI/ZO: 2304x1296 · PL/PR: 2880x1296 (subject left third for PR, right third for PL) · PU/PD: 2304x2160 · PV: 3840x1296"),
    "CHECK_HOLD": (
        "long holds are acceptable only where the idea keeps developing and the shot carries motion — never on ST."),
    "CHECK_MOTION": (
        "ST appears only on ~1–2-cue shots, capped at ~10% of shots, and only where the composition cannot tolerate overscan. Every other shot carries motion that matches its composition and canvas (a PR shot has the subject in the left third on a wide canvas; a ZI shot is centered with overscan; etc.). No single motion code exceeds ~40% of shots. Every motion code in `shots[].motion` matches the motion code in that shot's own filename. Transitions only CROSSFADE/DIP/DIP_WHITE, sparingly, never on the last shot."),
    "EX_A": "PR",
    "EX_B": "ST",
    "EX_C": "ZI",
}


def _num(value: float) -> str:
    return f"{value:g}"


@dataclass(frozen=True)
class MotionProfile:
    """How a channel's shots are paced and what motion they may use.

    The slot texts and the gate limits live together so they cannot drift:
    ``slots`` overrides STANDARD_SLOTS for the prompt, the other fields are
    what ``studio.shotlist_pacing`` enforces. ``None`` means "no such limit"."""
    key: str
    label: str
    description: str
    # motion codes the plan may use; None = any
    allowed: tuple[str, ...] | None = None
    st_max_share: float | None = ST_MAX_SHARE
    st_max_hold: float | None = ST_MAX_HOLD_SECONDS
    static_long_hold: float | None = STATIC_LONG_HOLD_SECONDS
    code_max_share: float | None = MOTION_MAX_SHARE
    code_share_overrides: dict = field(
        default_factory=lambda: dict(MOTION_MAX_SHARE_OVERRIDES))
    # ((codes...), share): those codes TOGETHER may take at most this share
    group_caps: tuple = ()
    # None = inherit (the channel / global shotlist_max_hold_seconds)
    min_hold: float | None = None
    max_hold: float | None = None
    slots: dict = field(default_factory=dict)
    # shot-type caps + host-in-frame shares (see normalize_types); None = none
    types: dict | None = None
    # the channel lets the plan use reveal shots (see reveal_block)
    reveal: bool = False
    # level 2: reveals are REQUIRED wherever the narration lists 2-4 items
    reveal_strict: bool = False

    def slot_values(self) -> dict[str, str]:
        values = dict(STANDARD_SLOTS)
        values.update(self.slots)
        mn = _num(self.min_hold) if self.min_hold else ""
        mx = _num(self.max_hold) if self.max_hold else ""
        out = {k: v.replace("[[MIN]]", mn).replace("[[MAX]]", mx)
               for k, v in values.items()}
        mix = mix_text(self)
        if mix and "MOTION_SECTION" in out:
            out["MOTION_SECTION"] = out["MOTION_SECTION"].rstrip() + "\n\n" + mix
            out["CHECK_MOTION"] = out["CHECK_MOTION"].rstrip() + " " + mix
        kinds = types_text(self.types)
        if kinds and "MOTION_SECTION" in out:
            out["MOTION_SECTION"] = (
                adapt_motion_table(out["MOTION_SECTION"], self.types,
                                   self.allowed).rstrip() + "\n\n" + kinds)
            out["CHECK_MOTION"] = out["CHECK_MOTION"].rstrip() + " " + kinds
        return out


def _allows(profile, code: str) -> bool:
    return profile.allowed is None or code in profile.allowed


def mix_targets(profile: "MotionProfile") -> list:
    """[((codes...), share)]: ST and the grouped codes (PU/PD...) with a stated
    share cap, which the plan must also actually use (about that share, never
    below half of it). The per-code overrides (PL/PR kept low because they
    default too easily, and rendered through the paid API) stay pure caps."""
    out: list = []
    if _allows(profile, "ST") and profile.st_max_share:
        out.append((("ST",), profile.st_max_share))
    for codes, cap in getattr(profile, "group_caps", ()):
        usable = tuple(c for c in codes if _allows(profile, c))
        if usable and cap:
            out.append((usable, cap))
    return out


def zoom_balanced(profile: "MotionProfile") -> bool:
    """True when both zoom codes are allowed, so their split is enforced."""
    return _allows(profile, "ZI") and _allows(profile, "ZO")


def mix_text(profile: "MotionProfile") -> str:
    """The brief's "aim for this mix" paragraph (empty when a profile has
    nothing to mix, e.g. a static-only channel)."""
    parts = [f"{'/'.join(codes)} about {share:.0%}"
             + (" (short holds only)" if codes == ("ST",) else
                " together" if len(codes) > 1 else "")
             for codes, share in mix_targets(profile)]
    if zoom_balanced(profile):
        lo, hi = ZOOM_SPLIT
        parts.append(f"ZI and ZO take all the remaining shots, split roughly "
                     f"evenly - ZI is between {lo:.0%} and {hi:.0%} of the "
                     f"ZI+ZO shots and ZO the rest")
    if not parts:
        return ""
    return ("Aim for this mix, not merely to stay under the caps: "
            + "; ".join(parts) + ". A plan with one code on nearly every "
            "shot, or that never uses a code listed here, fails review - "
            "choose each shot's motion while designing the image so the mix "
            "comes out right.")


STANDARD = MotionProfile(
    key="standard",
    label="Standard - all motions, short holds",
    description="The brief's own policy: every motion code, ST only on short "
                "holds (~10% of shots), no code above ~40% (PL/PR ~10%).")

STATIC = MotionProfile(
    key="static",
    label="Static only - no camera motion",
    description="Every shot is ST. No push, pull or pan; transitions only if "
                "the plan uses them sparingly.",
    allowed=("ST",), st_max_share=None, st_max_hold=None,
    static_long_hold=None, code_max_share=None, code_share_overrides={},
    slots={
        "HOLD_RULE": (
            "Every shot on this channel is a still frame (no camera motion), "
            "so a long hold has nothing but the image itself to carry it: "
            "hold an image only as long as the narration keeps developing "
            "the same idea, and never past the maximum hold given in the "
            "pacing math below."),
        "MOTION_SECTION": """\
### Motion — none: every shot is static (ST)

This channel uses NO camera motion. Every shot's `motion` is `ST` and every filename ends in `_ST.png`. Never use ZI, ZO, PL, PR, PU, PD or PV — a plan that uses any of them fails review. The rules elsewhere that cap ST (its share of shots, "ST only on short holds") and that ask for varied motion codes do NOT apply on this channel.

Because nothing moves, each image must work as a complete, finished frame on the standard 2304x1296 canvas: no overscan, no space reserved for a push or pan, no subject parked in a side third. The picture does all the work, so vary the camera angle, distance, subject placement, focal object, scale and composition from image to image instead of relying on movement.""",
        "MOTION_FIELD_RULE": (
            "- `motion`: always `ST`, and it must match the `_ST` code in the "
            "asset's own filename. This channel has no camera motion — never "
            "write any other motion code."),
        "CANVAS_SPEC": "ST: 2304x1296",
        "CHECK_HOLD": (
            "long holds are acceptable only where the idea keeps developing "
            "and never past the maximum hold — every shot is a static ST "
            "frame on this channel."),
        "CHECK_MOTION": (
            "Every shot is ST: `motion` is `ST` on every shot and every "
            "filename ends in `_ST.png` — no ZI/ZO/PL/PR/PU/PD/PV anywhere, "
            "and no ST share or ST hold limits apply on this channel. "
            "Transitions only CROSSFADE/DIP/DIP_WHITE, sparingly, never on "
            "the last shot."),
        "EX_A": "ST", "EX_B": "ST", "EX_C": "ST",
    })

LONG_HOLDS = MotionProfile(
    key="long_holds",
    label="Long holds 10-30s - with motion",
    description="Slow pacing: a shot normally holds 10-30s and carries "
                "motion for the whole hold. The 12s ceiling of the global "
                "setting does not apply.",
    min_hold=10.0, max_hold=30.0,
    slots={
        "HOLD_RULE": (
            "This channel is slow-paced: a shot normally holds between "
            "[[MIN]] and [[MAX]] seconds (hard maximum [[MAX]]s), so one "
            "image carries a whole developing idea. Group every cue that "
            "develops the same idea into ONE shot; a new concrete detail "
            "inside the same idea stays on the same image, and you split "
            "only where the idea itself changes (a pivot, a new subject, a "
            "new example). This replaces the 'each new concrete detail "
            "deserves its own visual' guidance above for this channel. "
            "Every hold of [[MIN]]s or more must carry motion — a long hold "
            "on a static frame is never acceptable (Section 7)."),
        "CHECK_HOLD": (
            "long holds ([[MIN]]-[[MAX]]s is normal on this channel) are "
            "acceptable only where the idea keeps developing and the shot "
            "carries motion — never on ST; no shot exceeds [[MAX]]s."),
    })

# ---- more presets -----------------------------------------------------------
# Rows of the standard composition table, minus the codes a preset does not use.
_FLOW_SAFE_TABLE = """\
| Visual | Motion | Why |
|---|---|---|
| INF diagram | ZI | push into the mechanism |
| INF wide comparison | ZO | pull back to reveal the full span |
| CMP left/right split | ZI | no horizontal pan on this channel: compose the pair as one centered frame and push in |
| CMP top/bottom split | PU / PD | the tilt performs the comparison - reveal A, then B |
| SCN character beat | ZI | pulls the viewer inward |
| SCN environment | ZO | pulls back to establish the space |
| CU detail | ZO | short hold, pulls out to context |
| HYB scene + graphics | ZI | scene first, graphics revealed by the push |
| PROC stages | PD | stack the stages top to bottom and tilt down through them |
| OVR wide concept | ZO | pull back to reveal the whole idea |"""

FLOW_SAFE = MotionProfile(
    key="flow_safe",
    label="Flow-safe - zooms and vertical tilts only",
    description="ST, ZI, ZO, PU, PD. No horizontal pans or panoramas: Google "
                "Flow cannot render the wide canvases PL/PR/PV need, so "
                "they would only render as push-ins.",
    allowed=("ST", "ZI", "ZO", "PU", "PD"),
    code_share_overrides={},
    slots={
        "MOTION_SECTION": f"""\
### Motion is assigned with composition, not after it

This channel uses only these motion codes: ST, ZI, ZO, PU and PD. Never use PL, PR or PV - the image generator cannot render the extra-wide canvas a horizontal pan or a panorama needs, and a plan that uses them fails review. Ignore every rule elsewhere in this brief that asks for PL, PR or PV.

ST (static) is the exception, not the default. Every other shot carries motion chosen while designing the image - the motion code determines the canvas and the subject placement, so it can never be stamped on afterward. Motion and composition must agree.

{_FLOW_SAFE_TABLE}

Motion must vary. A video where every shot is ZI has the same problem as one where every shot is ST - motion stops reading as expressive and becomes wallpaper. No single motion code above ~40% of shots.""",
        "MOTION_FIELD_RULE": (
            "- `motion`: ST | ZI | ZO | PU | PD — **must match the motion code in the asset's own filename**. Never write PL, PR or PV on this channel. **ST is rare and special:** use it only on very short holds — roughly 1–2 cues (~2–4 seconds) — and cap it at ~10% of shots, reserving it for compositions that cannot tolerate overscan. Every shot that holds longer must carry motion. Vary the codes — no single motion code above ~40% of shots."),
        "CANVAS_SPEC": "ST/ZI/ZO: 2304x1296 · PU/PD: 2304x2160",
        "CHECK_MOTION": (
            "Only ST, ZI, ZO, PU and PD are used - no PL, PR or PV anywhere. ST appears only on ~1–2-cue shots, capped at ~10% of shots. Every other shot carries motion that matches its composition and canvas. No single motion code exceeds ~40% of shots. Every motion code in `shots[].motion` matches the motion code in that shot's own filename. Transitions only CROSSFADE/DIP/DIP_WHITE, sparingly, never on the last shot."),
        "EX_A": "PD", "EX_B": "ST", "EX_C": "ZI",
    })

ZOOMS_ONLY = MotionProfile(
    key="zooms_only",
    label="Zooms only - push in and pull out",
    description="ST, ZI, ZO. No pans or tilts at all: the calmest look, "
                "for channels where side-scrolling feels wrong.",
    allowed=("ST", "ZI", "ZO"),
    code_max_share=0.65, code_share_overrides={},
    slots={
        "MOTION_SECTION": """\
### Motion — zooms only

This channel uses only three motion codes: ZI (slow push in), ZO (slow pull out) and ST (static). Never use PL, PR, PU, PD or PV - there are no pans or tilts here, and a plan that uses them fails review. Ignore every rule elsewhere in this brief that asks for them.

ST is the exception: only on very short holds (~1-2 cues) and ~10% of shots at most. Every other shot carries a zoom chosen while designing the image: push IN (ZI) to move toward a detail, a character or a mechanism; pull OUT (ZO) to reveal context, scale or a whole comparison. Because every canvas is the standard 2304x1296 with centered overscan, keep the main subject near the center and leave breathing room around it - nothing parked in a side third.

Alternate the two zooms so the video breathes in and out: neither ZI nor ZO above ~65% of shots, and avoid more than three of the same zoom in a row.""",
        "MOTION_FIELD_RULE": (
            "- `motion`: ST | ZI | ZO — **must match the motion code in the asset's own filename**. This channel has no pans or tilts: never write PL, PR, PU, PD or PV. **ST is rare and special:** only on very short holds — roughly 1–2 cues (~2–4 seconds) — and capped at ~10% of shots. Every other shot is ZI or ZO; neither above ~65% of shots."),
        "CANVAS_SPEC": "ST/ZI/ZO: 2304x1296",
        "CHECK_MOTION": (
            "Only ST, ZI and ZO are used - no PL/PR/PU/PD/PV anywhere. ST appears only on ~1–2-cue shots, capped at ~10% of shots. Neither ZI nor ZO exceeds ~65% of shots. Every motion code in `shots[].motion` matches the motion code in that shot's own filename. Transitions only CROSSFADE/DIP/DIP_WHITE, sparingly, never on the last shot."),
        "EX_A": "ZO", "EX_B": "ST", "EX_C": "ZI",
    })

FAST_PACED = MotionProfile(
    key="fast_paced",
    label="Fast-paced - short holds, all motions",
    description="A shot holds up to 6s (usually 2-5s), ST on up to ~25% of "
                "shots. For high-energy, news or list-style channels.",
    st_max_share=0.25, st_max_hold=4.0, max_hold=6.0,
    slots={
        "HOLD_RULE": (
            "This channel is fast-paced: a shot normally holds 2 to [[MAX]] "
            "seconds (hard maximum [[MAX]]s), so the picture changes "
            "whenever the narration reaches a new concrete detail, example "
            "or beat. Split generously - each new detail deserves its own "
            "visual - but never cut one idea into one-cue fragments just to "
            "change the image. A hold of 4 seconds or more should carry "
            "motion."),
        "MOTION_FIELD_RULE": STANDARD_SLOTS["MOTION_FIELD_RULE"].replace(
            "cap it at ~10% of shots", "cap it at ~25% of shots"),
        "CHECK_HOLD": (
            "no shot exceeds [[MAX]]s; the picture changes with the "
            "narration's beats, and holds of 4s or more carry motion."),
        "CHECK_MOTION": STANDARD_SLOTS["CHECK_MOTION"].replace(
            "capped at ~10% of shots", "capped at ~25% of shots"),
    })

DOCUMENTARY = MotionProfile(
    key="documentary",
    label="Documentary - slow zooms, holds 8-20s",
    description="Mostly slow ZI/ZO, each held 8-20s; pans and tilts "
                "are rare accents (~10% each), ST almost never.",
    st_max_share=0.05, code_max_share=0.55,
    code_share_overrides={c: 0.10 for c in ("PL", "PR", "PU", "PD", "PV")},
    min_hold=8.0, max_hold=20.0,
    slots={
        "HOLD_RULE": LONG_HOLDS.slots["HOLD_RULE"],
        "CHECK_HOLD": LONG_HOLDS.slots["CHECK_HOLD"],
        "MOTION_FIELD_RULE": (
            "- `motion`: ST | ZI | ZO | PL | PR | PU | PD | PV — **must match the motion code in the asset's own filename** (the image was composed for that motion and overscan). **This channel is slow and observational:** ZI and ZO carry most shots (each up to ~55%); every pan or tilt (PL, PR, PU, PD, PV) is a rare accent, ~10% of shots at most per code; ST is almost never used (~5% of shots, only on a ~1–2-cue hold). Every long hold carries a slow motion."),
        "CHECK_MOTION": (
            "ZI and ZO carry most shots (neither above ~55%); each of PL/PR/PU/PD/PV stays at ~10% of shots or less; ST is ~5% of shots at most, only on ~1–2-cue holds. Every other shot carries motion that matches its composition and canvas. Every motion code in `shots[].motion` matches the motion code in that shot's own filename. Transitions only CROSSFADE/DIP/DIP_WHITE, sparingly, never on the last shot."),
    })

MOTION_PRESETS: dict[str, MotionProfile] = {
    p.key: p for p in (STANDARD, STATIC, LONG_HOLDS, FLOW_SAFE, ZOOMS_ONLY,
                       FAST_PACED, DOCUMENTARY)}


def get_profile(key) -> MotionProfile:
    """The channel's motion profile; unknown / empty = standard."""
    return MOTION_PRESETS.get(str(key or "").strip().lower(), STANDARD)


# ---- the Custom profile: a channel types its own motion policy ---------------
CUSTOM_KEY = "custom"
CUSTOM_LABEL = "Custom - your own motion codes and limits"
CUSTOM_DESCRIPTION = ("Tick the motion codes the planner may use, set the "
                      "limits, and add your own motion rules in words.")

MOTION_CODES = ("ST", "ZI", "ZO", "PL", "PR", "PU", "PD", "PV")
# (what the camera does, canvas the image is composed on)
# custom share caps are set per family of codes, for the family TOGETHER (the
# custom spec key and the codes it covers). ZI/ZO have no cap of their own:
# they take whatever the others leave.
CODE_GROUPS: dict[str, tuple[str, ...]] = {
    "pan_max_share": ("PL", "PR"),
    "tilt_max_share": ("PU", "PD", "PV"),
}
ZOOM_CODES = ("ZI", "ZO")
MOTION_CODE_INFO: dict[str, tuple[str, str]] = {
    "ST": ("static - no camera move", "2304x1296"),
    "ZI": ("slow push in", "2304x1296"),
    "ZO": ("slow pull out", "2304x1296"),
    "PL": ("pan left (subject in the right third)", "2880x1296"),
    "PR": ("pan right (subject in the left third)", "2880x1296"),
    "PU": ("tilt up", "2304x2160"),
    "PD": ("tilt down", "2304x2160"),
    "PV": ("panoramic reveal across the whole idea", "3840x1296"),
}


def _frac(value, lo=0.01, hi=1.0):
    """A 0-1 share from a number (or numeric text); None if empty / bad."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v > 1:           # typed as a percentage
        v = v / 100.0
    return round(min(hi, max(lo, v)), 4) if v > 0 else None


def _secs(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return round(min(300.0, max(1.0, v)), 2) if v > 0 else None


def normalize_custom(spec) -> dict | None:
    """A clean custom spec from stored JSON / a dict, or None if unusable.

    Keys: allowed (motion codes, in canonical order), st_max_share (0-1),
    st_max_hold (s), code_max_share (0-1), rules (free text). Empty numbers
    mean "the standard limit"."""
    if isinstance(spec, str):
        import json
        try:
            spec = json.loads(spec)
        except ValueError:
            return None
    if not isinstance(spec, dict):
        return None
    raw = spec.get("allowed")
    raw = [raw] if isinstance(raw, str) else (raw or [])
    allowed = [c for c in MOTION_CODES
               if c in {str(x).strip().upper() for x in raw}]
    if not allowed:
        return None
    return {
        "allowed": allowed,
        "st_max_share": _frac(spec.get("st_max_share")),
        "st_max_hold": _secs(spec.get("st_max_hold")),
        "code_max_share": _frac(spec.get("code_max_share")),
        **{key: _frac(spec.get(key)) for key in CODE_GROUPS},
        "rules": str(spec.get("rules") or "").strip(),
    }


def _caps(spec: dict):
    """(st_share, default per-code cap, per-code overrides, group caps).

    A pan / tilt family cap limits that family's codes TOGETHER; a family left
    empty keeps the standard per-code cap. ZI/ZO are never capped on their own
    - they take whatever share the others leave. cap None = no cap at all."""
    allowed = spec["allowed"]
    moving = [c for c in allowed if c != "ST"]
    st_share = (spec["st_max_share"] or ST_MAX_SHARE) if "ST" in allowed else 0.0
    cap, overrides, groups = spec["code_max_share"], {}, []
    if cap is None and len(moving) >= 4:      # the standard caps, per code
        if not spec.get("pan_max_share"):
            overrides.update({c: v for c, v in MOTION_MAX_SHARE_OVERRIDES.items()
                              if c in moving})
        if not spec.get("tilt_max_share"):
            overrides.update({c: MOTION_MAX_SHARE for c in ("PU", "PD", "PV")
                              if c in moving})
    for key, codes in CODE_GROUPS.items():
        inside = tuple(c for c in codes if c in moving)
        if spec.get(key) and inside:
            groups.append((inside, spec[key]))
    if (overrides or groups) and cap is None:
        cap = 1.0                              # only the family caps apply
    return st_share, cap, overrides, tuple(groups)


def _room(spec: dict) -> float:
    """The most share the caps let the ticked codes cover (>= 1 is fine)."""
    st_share, cap, overrides, groups = _caps(spec)
    moving = [c for c in spec["allowed"] if c != "ST"]
    grouped = {c for codes, _v in groups for c in codes}
    room = st_share + sum(v for _c, v in groups)
    room += sum(overrides.get(c, cap if cap is not None else 1.0)
                for c in moving if c not in grouped)
    return room


def share_text(cap, overrides, groups) -> str:
    """The share limits as one sentence for the prompt ('' = none)."""
    parts = []
    if cap is not None and cap < 1.0:
        parts.append(
            f"No single motion code above ~{cap:.0%} of shots"
            + ("" if not overrides else " (" + ", ".join(
                f"{c} ~{v:.0%}" for c, v in sorted(overrides.items()))
               + " are capped tighter)") + ".")
    elif overrides:
        parts.append("Share caps, per code: " + ", ".join(
            f"{c} ~{v:.0%}" for c, v in sorted(overrides.items())) + ".")
    if groups:
        parts.append("Share caps, codes of a group together: " + ", ".join(
            f"{'/'.join(codes)} at most ~{v:.0%} of shots"
            for codes, v in groups) + ".")
    return " ".join(parts)


def custom_error(spec) -> str | None:
    """Why this custom spec cannot work (None = fine)."""
    spec = normalize_custom(spec)
    if spec is None:
        return "tick at least one motion code"
    moving = [c for c in spec["allowed"] if c != "ST"]
    if "ST" not in spec["allowed"] and not moving:
        return "tick at least one motion code"
    if moving:
        st_share, cap, overrides, groups = _caps(spec)
        if cap is not None:
            zoom = [c for c in ZOOM_CODES if c in moving]
            taken = st_share + sum(v for _c, v in groups)
            if zoom and groups and taken >= 1.0 - 1e-9:
                return ("the other shares already use 100% of the shots - "
                        "nothing is left for " + " / ".join(zoom))
            room = _room(spec)
            if room + 1e-9 < 1.0:
                return (f"the share caps allow at most {room:.0%} of shots "
                        f"across the {len(moving)} moving code(s) - raise a "
                        "cap or tick more codes")
    return None


def _canvas_spec(allowed) -> str:
    groups: dict[str, list[str]] = {}
    for c in allowed:
        groups.setdefault(MOTION_CODE_INFO[c][1], []).append(c)
    return " · ".join(f"{'/'.join(codes)}: {size}"
                      for size, codes in groups.items())


def custom_profile(spec) -> MotionProfile:
    """A MotionProfile built from a channel's own custom spec; an unusable
    spec gives the standard profile."""
    spec = normalize_custom(spec)
    if spec is None or custom_error(spec):
        return STANDARD
    allowed = tuple(spec["allowed"])
    moving = [c for c in allowed if c != "ST"]
    has_st = "ST" in allowed
    if allowed == ("ST",):                    # all-static = the Static preset
        base = replace(STATIC, key=CUSTOM_KEY, label=CUSTOM_LABEL,
                       description=CUSTOM_DESCRIPTION)
        if spec["rules"]:
            slots = dict(base.slots)
            slots["MOTION_SECTION"] += ("\n\nCreator's own rules for this "
                                        "channel (follow them):\n"
                                        + spec["rules"])
            base = replace(base, slots=slots)
        return base
    st_share, cap, overrides, groups = _caps(spec)
    st_hold = (spec["st_max_hold"] or ST_MAX_HOLD_SECONDS) if has_st else None
    not_allowed = [c for c in MOTION_CODES if c not in allowed]

    rows = "\n".join(f"| {c} | {MOTION_CODE_INFO[c][0]} | "
                     f"{MOTION_CODE_INFO[c][1]} |" for c in allowed)
    limits = []
    if has_st:
        limits.append(
            f"ST (static) is the exception: only on very short holds "
            f"(~{_num(st_hold)}s or less, roughly 1-2 cues) and at most "
            f"~{st_share:.0%} of shots.")
    else:
        limits.append("There is no static code on this channel: every shot "
                      "carries motion, and a long hold on a still frame is "
                      "never acceptable.")
    shares = share_text(cap, overrides, groups)
    if shares:
        limits.append(shares)
    if len(moving) > 1:
        limits.append("Vary the codes - one code on every shot reads as "
                      "wallpaper.")
    section = (
        "### Motion — this channel's own rules\n\n"
        f"Allowed motion codes on this channel: {', '.join(allowed)}."
        + (f" Never use {', '.join(not_allowed)} - a plan that uses a code "
           "outside this list fails review, and every rule elsewhere in this "
           "brief that asks for one does NOT apply here." if not_allowed
           else "")
        + "\n\nThe motion code determines the canvas and the subject "
        "placement, so choose it while designing the image, never stamp it on "
        "afterward. Motion and composition must agree.\n\n"
        "| Code | Camera move | Canvas |\n|---|---|---|\n" + rows + "\n\n"
        + " ".join(limits))
    if spec["rules"]:
        section += ("\n\nCreator's own motion rules for this channel (follow "
                    "them; they decide which allowed code suits which "
                    "image):\n" + spec["rules"])

    field_rule = (
        f"- `motion`: {' | '.join(allowed)} — **must match the motion code in "
        "the asset's own filename** (the image was composed for that motion "
        "and overscan)."
        + (f" Never write {', '.join(not_allowed)}." if not_allowed else "")
        + (f" **ST is rare and special:** only on very short holds (~"
           f"{_num(st_hold)}s or less) and at most ~{st_share:.0%} of shots; "
           "every shot that holds longer must carry motion." if has_st else
           " Every shot carries motion.")
        + (" " + shares if shares else ""))
    check_motion = (
        f"Only {', '.join(allowed)} are used"
        + (f" - never {', '.join(not_allowed)}" if not_allowed else "") + ". "
        + (f"ST appears only on short holds, at most ~{st_share:.0%} of "
           "shots. " if has_st else "Every shot carries motion. ")
        + (shares + " " if shares else "")
        + "Every motion code in `shots[].motion` matches the motion code in "
        "that shot's own filename. Transitions only CROSSFADE/DIP/DIP_WHITE, "
        "sparingly, never on the last shot.")
    seq = moving or ["ST"]
    slots = {
        "MOTION_SECTION": section,
        "MOTION_FIELD_RULE": field_rule,
        "CANVAS_SPEC": _canvas_spec(allowed),
        "CHECK_MOTION": check_motion,
        "EX_A": seq[0],
        "EX_B": "ST" if has_st else seq[1 % len(seq)],
        "EX_C": seq[2 % len(seq)] if len(seq) > 2 else seq[-1],
    }
    if not has_st:
        slots["CHECK_HOLD"] = (
            "long holds are acceptable only where the idea keeps developing "
            "- every shot carries motion on this channel.")
        slots["HOLD_RULE"] = (
            "A hold that runs long (roughly 15 seconds or more) is "
            "acceptable only when the idea keeps developing - every shot "
            "carries motion on this channel (Section 7).")
    return MotionProfile(
        key=CUSTOM_KEY, label=CUSTOM_LABEL, description=CUSTOM_DESCRIPTION,
        allowed=None if len(allowed) == len(MOTION_CODES) else allowed,
        st_max_share=st_share if has_st else None,
        st_max_hold=st_hold,
        static_long_hold=STATIC_LONG_HOLD_SECONDS if has_st else None,
        code_max_share=cap, code_share_overrides=overrides,
        group_caps=groups, slots=slots)


_RANGE_HOLD_RULE = (
    "This channel is slow-paced: a shot normally holds between [[MIN]] and "
    "[[MAX]] seconds (hard maximum [[MAX]]s), so one image carries a whole "
    "developing idea. Group every cue that develops the same idea into ONE "
    "shot; a new concrete detail inside the same idea stays on the same "
    "image, and you split only where the idea itself changes (a pivot, a new "
    "subject, a new example). This replaces the 'each new concrete detail "
    "deserves its own visual' guidance above for this channel.")


# ---- shot types: per-type caps and host-in-frame shares -----------------------
# The brief's TYPE codes (the third part of an asset name, S01_04_INF_ZI.png).
TYPE_CODES: dict[str, str] = {
    "SCN": "scene / character / environment",
    "CU": "close-up / detail",
    "INF": "infographic / diagram",
    "CMP": "comparison (A vs B, before/after)",
    "PROC": "process / stages",
    "HYB": "scene + explanatory graphics",
    "OVR": "conceptual overview",
}
# The types that are diagrams / infographics rather than a plain scene: one
# combined cap ("graphics_max") limits how much of the video they take together.
GRAPHIC_CODES = ("INF", "CMP", "PROC", "HYB", "OVR")
# a type's host share counts as met within this many percentage points (plans
# with fewer than TYPE_MIN_SHOTS shots of that type only enforce 0% and 100%)
HOST_TOLERANCE = 15
TYPE_MIN_SHOTS = 4
# plans shorter than this only enforce a 0% cap (rounding makes small plans noisy)
TYPE_CAP_MIN_PLAN = 10


def _pct(value) -> int | None:
    try:
        text = str(value).strip().rstrip("%")
        if text == "":
            return None
        return max(0, min(100, int(round(float(text)))))
    except (TypeError, ValueError):
        return None


def normalize_types(raw) -> dict | None:
    """Clean a shot-type spec: {"max": {CODE: 0-100}, "host_ref": "CH_X",
    "host": {CODE: 0-100}} or None when nothing is set. `max` is the largest
    share of ALL shots a type may take (0 = the type is never used, absent =
    no limit); `host` is the share of THAT type's shots that show the host
    (0 = never, 100 = always, absent = no rule) and only applies when the
    channel names its host's ref in `host_ref`."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else None
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    caps = {}
    for code, v in (raw.get("max") or {}).items():
        code = str(code).upper()
        pct = _pct(v)
        if code in TYPE_CODES and pct is not None:
            caps[code] = pct
    if caps:
        out["max"] = caps
    gmax = _pct(raw.get("graphics_max"))
    if gmax is not None:
        out["graphics_max"] = gmax
    ref = str(raw.get("host_ref") or "").strip()
    host = {}
    if ref:
        for code, v in (raw.get("host") or {}).items():
            code = str(code).upper()
            pct = _pct(v)
            if code in TYPE_CODES and pct is not None:
                host[code] = pct
        out["host_ref"] = ref
    if host:
        out["host"] = host
    elif "host_ref" in out and not caps and "graphics_max" not in out:
        return None   # a host name alone sets no rule
    return out or None


def effective_types(global_max: dict | None, channel_raw,
                    global_graphics: int | None = None) -> dict | None:
    """The caps a production runs with: the global per-type caps, overridden
    per code by the channel's own; host rules come from the channel only."""
    chan = normalize_types(channel_raw) or {}
    caps = dict(global_max or {})
    caps.update(chan.get("max") or {})
    caps = {c: p for c, p in caps.items() if p < 100}   # 100% = no limit
    spec = {}
    if caps:
        spec["max"] = caps
    g = chan.get("graphics_max", global_graphics)
    if g is not None and g < 100:
        spec["graphics_max"] = g
    if chan.get("host_ref") and chan.get("host"):
        spec["host_ref"] = chan["host_ref"]
        spec["host"] = chan["host"]
    return spec or None


# Motion codes whose only table rows belong to a diagram/comparison type: when
# that type is banned the code needs another visual to be reachable at all.
_FALLBACK_ROWS = {
    "PU": ("SCN tall subject (a person head to toe, a tower, a staircase)",
           "PU / PD", "the tilt reveals the height"),
    "PD": ("SCN tall subject (a person head to toe, a tower, a staircase)",
           "PU / PD", "the tilt reveals the height"),
    "PV": ("SCN panoramic landscape or crowd", "PV",
           "panoramic reveal across the whole scene"),
    "PL": ("SCN environment", "PL / PR", "establishes space"),
    "PR": ("SCN environment", "PL / PR", "establishes space"),
}
_ROW_RE = re.compile(r"^\| (SCN|CU|INF|CMP|PROC|HYB|OVR)\b[^|]*\|([^|]*)\|")


def adapt_motion_table(text: str, spec, allowed=None) -> str:
    """Make the brief's visual -> motion table agree with the type spec: rows
    of a banned type (cap 0, or any diagram type when graphics_max is 0) are
    removed, a motion code that thereby loses every row gets a plain-scene row
    instead (so PU/PD, PV, PL/PR stay reachable), and rows of types that may
    share the frame with the host / never do say so."""
    spec = normalize_types(spec)
    if not spec or "|---|" not in text:
        return text
    banned = {c for c, p in (spec.get("max") or {}).items() if p == 0}
    if spec.get("graphics_max") == 0:
        banned |= set(GRAPHIC_CODES)
    host = spec.get("host") or {}
    lines = text.split("\n")
    kept, lost = [], set()
    for line in lines:
        m = _ROW_RE.match(line)
        if m and m.group(1) in banned:
            lost.update(c.strip() for c in m.group(2).split("/"))
            continue
        if m and m.group(1) in host and spec.get("host_ref"):
            note = ("the host may share the frame" if host[m.group(1)]
                    else "never with the host")
            line = line.rstrip().rstrip("|").rstrip() + f" ({note}) |"
        kept.append(line)
    left = set()
    for line in kept:
        m = _ROW_RE.match(line)
        if m:
            left.update(c.strip() for c in m.group(2).split("/"))
    extra = []
    for code in sorted(lost - left):
        row = _FALLBACK_ROWS.get(code)
        if row and (allowed is None or code in allowed):
            text_row = f"| {row[0]} | {row[1]} | {row[2]} |"
            if text_row not in extra:
                extra.append(text_row)
    if extra:
        last = max(i for i, ln in enumerate(kept) if _ROW_RE.match(ln))
        kept[last + 1:last + 1] = extra
    return "\n".join(kept)


def types_text(spec) -> str:
    """The prompt/check text for a type spec ('' when there is none)."""
    spec = normalize_types(spec)
    if not spec:
        return ""
    parts = []
    for code, pct in (spec.get("max") or {}).items():
        if pct == 0:
            parts.append(f"{code} ({TYPE_CODES[code]}) is NEVER used on this "
                         f"channel - not one shot.")
        else:
            parts.append(f"{code} ({TYPE_CODES[code]}) takes at most {pct}% "
                         f"of all shots.")
    if "graphics_max" in spec:
        g = spec["graphics_max"]
        names = "/".join(GRAPHIC_CODES)
        parts.append(f"Diagrams and infographics ({names}) together are "
                     + (f"NEVER used - every shot is a scene or close-up."
                        if g == 0 else f"at most {g}% of all shots."))
    host = spec.get("host") or {}
    ref = spec.get("host_ref")
    for code, pct in host.items():
        if pct == 0:
            parts.append(f"The host ({ref}) NEVER appears in {code} shots.")
        elif pct == 100:
            parts.append(f"The host ({ref}) appears in EVERY {code} shot.")
        else:
            lo, hi = max(0, pct - HOST_TOLERANCE), min(100, pct + HOST_TOLERANCE)
            parts.append(f"The host ({ref}) appears in about {pct}% of the "
                         f"{code} shots ({lo}-{hi}%).")
    if not parts:
        return ""
    tail = (f" A shot shows the host only when its image entry lists "
            f"\"{ref}\" in `refs`, and lists it whenever the host is in the "
            f"frame." if ref and host else "")
    return ("SHOT TYPE TARGETS (checked as hard faults): "
            + " ".join(parts) + tail)


def _resolve_motion(key, min_hold=None, max_hold=None,
                    default_max: float | None = None,
                    custom=None) -> MotionProfile:
    """The channel's effective profile: its motion preset, with the channel's
    own hold range (seconds) laid over it. The range feeds the prompt AND the
    review gates (briefs.pacing_note, studio.shotlist_pacing) so they cannot
    disagree. A minimum needs a maximum: the preset's, else `default_max`
    (the global shotlist_max_hold_seconds). A min >= max is ignored.
    `custom` is the channel's own spec, used when the key is "custom"."""
    base = (custom_profile(custom)
            if str(key or "").strip().lower() == CUSTOM_KEY
            else get_profile(key))
    mn = float(min_hold) if min_hold else base.min_hold
    mx = float(max_hold) if max_hold else base.max_hold
    if mn and not mx and default_max:
        mx = float(default_max)
    if mn and mx and mn >= mx:
        mn = None
    if (mn, mx) == (base.min_hold, base.max_hold):
        return base
    slots = dict(base.slots)
    if mn and "[[MIN]]" not in slots.get("HOLD_RULE", ""):
        motion = base.allowed != ("ST",)
        slots["HOLD_RULE"] = _RANGE_HOLD_RULE + (
            " Every hold of [[MIN]]s or more must carry motion — a long hold "
            "on a static frame is never acceptable (Section 7)."
            if motion else "")
        slots["CHECK_HOLD"] = (
            "long holds ([[MIN]]-[[MAX]]s is normal on this channel) are "
            "acceptable only where the idea keeps developing"
            + (" and the shot carries motion — never on ST" if motion
               else "") + "; no shot exceeds [[MAX]]s.")
    return replace(base, min_hold=mn, max_hold=mx, slots=slots)


def resolve_profile(key, min_hold=None, max_hold=None,
                    default_max: float | None = None,
                    custom=None, types=None, reveal=None) -> MotionProfile:
    """The channel's effective profile: its motion preset, the channel's own
    hold range (seconds), its shot-type spec (`types`, see
    normalize_types / effective_types) and whether reveal shots are allowed.
    Nothing set = today's profile."""
    prof = _resolve_motion(key, min_hold, max_hold, default_max, custom)
    spec = normalize_types(types)
    if spec:
        prof = replace(prof, types=spec)
    if reveal:
        # 1 / True = allowed, 2 = required wherever the narration lists items
        prof = replace(prof, reveal=True, reveal_strict=str(reveal) == "2")
    return prof


# ---- presentation (who is on screen, and when) -----------------------------
PRESENTATION_STARTERS: dict[str, tuple[str, str]] = {
    "host_story": (
        "Host opens and closes, example cast in the middle",
        "The channel's host (the supplied bible character) is on screen in the "
        "opening scenes, introducing the topic to the viewer. When the "
        "narration moves into its examples or case studies (for instance two "
        "families, or Person A and Person B), cast each example as its own "
        "recurring character - invent them on the fly, and keep each one's "
        "look identical every time they appear - and let the examples carry "
        "the middle of the video. Bring the host back on screen when the "
        "narration turns to advice, recommendations or the conclusion. Do not "
        "show the host inside the example scenes."),
    "narrator_throughout": (
        "Narrator on screen throughout",
        "The narrator (the supplied bible character) is on screen in every "
        "scene, speaking to the viewer, and the setting around them changes "
        "with the topic. Show what the narration describes behind, beside or "
        "in the narrator's hands rather than cutting away from them."),
    "faceless": (
        "No narrator on screen",
        "No narrator or host is ever shown. Open directly on the subject the "
        "narration is about and show only the subject matter - its setting, "
        "the mechanism, the people or things the narration describes - for "
        "the whole video."),
    "two_hosts": (
        "Two hosts in conversation",
        "Two recurring hosts (the supplied bible characters) carry the video "
        "as a conversation: show both of them on screen together while they "
        "talk, and vary who is in the foreground, their expressions and the "
        "setting from image to image. When the narration describes an "
        "example, cut to what it describes and return to the hosts after it. "
        "Keep each host's look identical every time they appear."),
    "first_person": (
        "First-person view - only hands on screen",
        "Show the video from the viewer's own point of view: no face and no "
        "full-body narrator, only hands, forearms and what they are doing - "
        "holding, pointing, writing, opening, using the objects the narration "
        "mentions - against changing settings. When the narration is about "
        "an idea rather than an action, show the subject matter itself."),
    "mascot": (
        "A recurring mascot that reacts",
        "A single recurring mascot (the supplied bible character) appears in "
        "most scenes as a silent companion who reacts to what the narration "
        "says - curious, surprised, worried, relieved - while the setting "
        "changes with the topic. The mascot never speaks and never blocks the "
        "subject; where a diagram or a close-up needs the full frame, show "
        "it without the mascot."),
    "host_bookends": (
        "Host only in the opening and closing scenes",
        "The channel's host (the supplied bible character) appears only in "
        "the opening scenes, introducing the topic, and in the closing "
        "scenes, wrapping up and handing off to the call to action. Every "
        "scene in between shows only the subject matter - settings, "
        "mechanisms, examples - with no host on screen."),
    "subject_only": (
        "Subject only - no people at all",
        "No person is ever shown, not even as a silhouette or a pair of "
        "hands. Show only the subject matter: places, animals, objects, "
        "mechanisms, diagrams and symbolic images. When the narration "
        "describes something a person does, show its tools, its setting or "
        "its result instead of the person."),
}


def presentation_block(text) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    return (
        "## SECTION 6B — CHANNEL PRESENTATION (who is on screen, and when)\n\n"
        f"{text}\n\n"
        "This section only decides WHO appears in the images and WHEN: how the "
        "narrator and any example characters are staged across the video. It "
        "does not change any hard rule elsewhere in this brief (cue coverage, "
        "the refs registry, filenames, the output shape). Where it names a "
        "character the bible supplies, use that bible entry; characters the "
        "narration introduces for this one video (examples, case studies) are "
        "invented on the fly per Section 6.")


# ---- reveal shots (items shown one at a time) --------------------------------
REVEAL_MIN_ITEMS = 2
REVEAL_MAX_ITEMS = 4


REVEAL_REQUIRED_RULE = (
    "\n\nTHIS CHANNEL REQUIRES REVEAL SHOTS. Reveals are not optional here: "
    "every passage of the narration that names 2-4 parallel items one after "
    "another (\"first, second, third\", numbered steps, a list of fees, "
    "habits, animals, symptoms or numbers) MUST be planned as a reveal shot - "
    "never as an ordinary shot and never as one multi-panel image. A longer "
    "list is split across consecutive reveal shots of 2-4 items. Give every "
    "reveal a fitting sound effect on each item. Before you return the "
    "shotlist, read the narration once more for such passages and confirm "
    "each one became a reveal shot; a plan that leaves a listed passage as an "
    "ordinary shot is wrong.")


def reveal_block(strict: bool = False) -> str:
    """The planning-brief section that teaches reveal shots. Only added for a
    channel that turned them on (MotionProfile.reveal)."""
    text = (
        "## SECTION 7B — REVEAL SHOTS AND SOUND EFFECTS (items shown one at a time)\n\n"
        "A reveal shot shows 2–4 parallel items as the narrator names them: "
        "the first item is on screen alone, the second appears when the "
        "narration reaches it, then the third. It is ONE image holding the "
        "items in a left-to-right row, each in its own equal-width slice (2 "
        "items = halves, 3 = thirds, 4 = quarters). The assembler cuts the "
        "image along those slice lines and reveals the slices in order, so "
        "the layout has to be exact.\n\n"
        "Use it only when ONE passage of the narration walks through "
        f"{REVEAL_MIN_ITEMS}–{REVEAL_MAX_ITEMS} parallel things one after "
        "another (three causes, two options, four steps) and each deserves "
        "its own beat on screen. It is a rare tool — at most about 10% of "
        "the shots; everything else stays a normal shot. When in doubt, do "
        "not use it.\n\n"
        "Add `\"reveal\": [c1, c2, c3]` to the shot: the SRT cue at which "
        "each item is first named — one cue per item, strictly increasing, "
        "all inside the shot's own `cues`, the first equal to the shot's "
        "first cue. Item 1 is the leftmost slice, item 2 the next, and so on, "
        "in narration order.\n\n"
        "Rules for a reveal shot:\n"
        "- It never moves: `\"motion\": \"ST\"` and the asset file name ends "
        "with the ST code. The ST share and ST hold limits above do not "
        "apply to it; the channel's normal maximum hold does.\n"
        "- Its image prompt must state the item count, name each item in "
        "left-to-right order and put every item in its own slice, e.g. "
        "\"three separate items in a row, left to right: A, B, C, each "
        "centered in its own equal third of the frame\".\n"
        "- Keep clear empty space between neighbouring slices. Nothing may "
        "cross a vertical slice line — no object, shadow, floor line, glow, "
        "arrow, prop or text — and no item may lean into a neighbour's slice.\n"
        "- One plain, identical background and the same lighting, scale and "
        "ground line across all slices, so the slices that are still hidden "
        "leave no visible gap or mismatch.\n"
        "- No character, hand or object may appear in more than one slice, "
        "and an item must not need another item to make sense.\n"
        "- A reveal image cannot be fixed afterwards: the slices are cut by "
        "pixels. If the items cannot be laid out cleanly in equal slices, "
        "plan normal shots instead.\n\n"
        "Variant A — a 2×2 grid, for exactly 4 items in a square: "
        "`\"reveal\": {\"cues\": [c1, c2, c3, c4], \"layout\": \"grid\"}`. "
        "It is still ONE image, now with four equal quadrants revealed in "
        "reading order: top-left, top-right, bottom-left, bottom-right. The "
        "rules above apply to the quadrants: the prompt says \"four separate "
        "items in a 2x2 grid\", names them in that order, centers each in its "
        "own quadrant, leaves clear empty space around the vertical and "
        "horizontal centre lines and lets nothing cross either centre line.\n\n"
        "Variant B — separate images one after another, for when each item "
        "deserves its own picture (so the same-image limits above do not "
        "apply): `\"reveal\": {\"cues\": [c1, c2, c3], \"assets\": "
        "[\"S05_01_INF_ST.png\", \"S05_02_INF_ST.png\", "
        "\"S05_03_INF_ST.png\"], \"layout\": \"row\"}` (layout \"row\" for "
        "2–3 images, \"grid\" for exactly 4; \"row\" is the default). Each "
        "asset is its own normal entry in `images`, with a normal prompt for "
        "what the narrator says at its cue; the shot's `asset` is the first "
        "of `assets`, and every name ends with _ST. The earlier images stay "
        "on screen side by side as the next one appears (build-up). Each "
        "image is centre-cropped to fill its slot — in a row a tall strip one "
        "Nth of the frame wide, in a grid a quarter of the frame — so keep "
        "each image to ONE subject centered with generous empty space on both "
        "sides. If each picture should instead REPLACE the previous one, do "
        "not use `reveal`: plan ordinary consecutive shots.\n\n"
        "Sound effects (optional, any shot): add `\"sfx\": \"pop\"` to play a "
        "short sound (about a second) when the shot starts. On a reveal shot "
        "a single name plays at every item; a list gives one name per item, "
        "e.g. `[\"ding\", \"pop\", \"pop\"]` (the last name repeats when the "
        "list is short). Built-in sounds: pop, ding, click, tick, whoosh, "
        "swipe. Use them sparingly — on reveal items or a deliberate emphasis "
        "— and keep to one or two sounds across the video; never on most "
        "shots, and never as a substitute for the narration. A normal shot "
        "takes ONE name, not a list.\n\n"
        "Final check for every reveal shot, before you output: the `reveal` "
        "list has one cue per item, increasing, starting at the shot's first "
        "cue and inside its `cues`; `motion` is ST and the file name ends "
        "with _ST; the prompt states the item count, names the items left to "
        "right in narration order, keeps each in its own equal slice and "
        "forbids anything crossing a slice line; a grid has exactly 4 cues; "
        "separate images have one `assets` entry per cue, the first equal to "
        "`asset`, each its own entry in `images`; any `sfx` is a built-in "
        "name; and reveal shots are no more than about 10% of all shots.\n")
    return text + (REVEAL_REQUIRED_RULE if strict else "")


# ---- rendering ---------------------------------------------------------------
_MARKER_RE = re.compile(r"\{\{([A-Z_]+)\}\}")


def read_template(path: Path | None = None) -> str:
    return Path(path or TEMPLATE_PATH).read_text(encoding="utf-8")


def render_brief(template: str, profile: MotionProfile | None = None,
                 presentation: str = "") -> str:
    """Fill the template's slots from a motion profile + presentation text.

    A custom template (``studio.manifest_brief``) may have no slots at all - it
    then renders as written, which is only legitimate for the standard profile
    (a non-standard motion policy that could not be applied would silently
    plan with the wrong rules, so that is an error)."""
    profile = profile or STANDARD
    values = profile.slot_values()
    present = set(_MARKER_RE.findall(template))
    if profile.key != STANDARD.key and "MOTION_SECTION" not in present:
        raise RuntimeError(
            f"the planning brief template has no {{{{MOTION_SECTION}}}} slot, "
            f"so the '{profile.key}' motion profile cannot be applied - use "
            "the built-in template or add the slots (see brief_template.md)")
    block = presentation_block(presentation)
    text = template
    if "PRESENTATION" in present:
        if block:
            text = text.replace("{{PRESENTATION}}", block)
        else:  # drop the marker line and the blank line after it
            text = re.sub(r"^\{\{PRESENTATION\}\}\n\n", "", text, flags=re.M)
            text = text.replace("{{PRESENTATION}}", "")
    elif block:
        text = text.rstrip() + "\n\n" + block
    if profile.reveal:
        # right after the output-document section; a custom template without
        # that heading gets it at the end
        marker = "## SECTION 8 "
        at = text.find(marker)
        if at >= 0:
            text = text[:at] + reveal_block(profile.reveal_strict) + "\n" + text[at:]
        else:
            text = text.rstrip() + "\n\n" + reveal_block(profile.reveal_strict)
    for name, value in values.items():
        text = text.replace("{{%s}}" % name, value)
    left = sorted(set(_MARKER_RE.findall(text)))
    if left:
        raise RuntimeError("the planning brief template has unknown slot(s): "
                           + ", ".join("{{%s}}" % n for n in left))
    return text.strip()


# ---- the planner's pacing note ----------------------------------------------
def pacing_note(profile: MotionProfile | None, total_s: float, n_cues: int,
                max_hold: float, min_align: float) -> str:
    """The "PACING MATH FOR THIS CHANNEL" text handed to the planner: the
    narration's length and the max hold give a hard minimum shot count -
    without it a planner that cannot see durations under-plans (e.g. 34 shots
    for ~960s) and the review fails after a very expensive generation."""
    profile = profile or STANDARD
    if not (max_hold > 0 and total_s > 0):
        return ""
    min_shots = int(total_s / max_hold) + (1 if total_s % max_hold else 0)
    head = (f"the narration runs ~{total_s:.0f}s across {n_cues} cues "
            f"(the (Ns) suffix on each line is that cue's length in "
            f"seconds). How this plan will be judged on this channel: "
            f"every cue covered exactly once, in order (the rule that "
            f"outranks everything); no shot holding longer than "
            f"{max_hold:.0f}s; ")
    if profile.min_hold:
        lo = max(1, int(total_s / max_hold))
        hi = max(lo, int(math.ceil(total_s / profile.min_hold)))
        return (
            head + f"shots are meant to hold {profile.min_hold:.0f}-"
            f"{max_hold:.0f}s, so group the cues of one developing idea into "
            f"ONE shot and split only where the idea itself changes; "
            f"detailed prompts on at least {min_align:.0%} of shots. This is "
            f"a slow-paced channel: about {lo} shots (every shot at the "
            f"{max_hold:.0f}s maximum) up to about {hi} (every shot at the "
            f"{profile.min_hold:.0f}s minimum) is the expected range - far "
            f"fewer shots than a fast-cut channel, so do NOT split at every "
            f"new concrete detail (the brief's guidance on that is replaced "
            f"by this channel's pacing)."
            + ("" if profile.allowed == ("ST",) else
               f" Every hold of {profile.min_hold:.0f}s or more carries "
               f"motion.")
            + " A plan that chops the narration into many short shots, or "
              "that leaves prompts terse, fails the review.")
    return (
        head + f"detailed prompts on at least "
        f"{min_align:.0%} of shots. Plan strictly from meaning per "
        f"Section 2 - each new concrete detail deserves its own "
        f"visual. {min_shots} is only the ABSOLUTE FLOOR (it assumes "
        f"every shot runs the full {max_hold:.0f}s, but most cues are "
        f"far shorter), so a meaning-driven plan lands WELL ABOVE "
        f"{min_shots} - do not treat it as a target to stop at. A "
        f"plan that merges many cues into long holds, or that leaves "
        f"prompts terse, fails the review.")
