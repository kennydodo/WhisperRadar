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

import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

TEMPLATE_PATH = Path(__file__).with_name("brief_template.md")

# ---- the standard limits (what the review gates have always enforced) ------
# ST is only for ~1-2-cue shots (~2-4s) and ~10% of shots; no motion code above
# ~40% (PL/PR tighter, at ~15% - they default too easily); a static hold of
# 15s+ is never acceptable.
ST_MAX_HOLD_SECONDS = 5.0
ST_MAX_SHARE = 0.10
MOTION_MAX_SHARE = 0.40
MOTION_MAX_SHARE_OVERRIDES = {"PL": 0.15, "PR": 0.15}
STATIC_LONG_HOLD_SECONDS = 15.0
# On a channel with a minimum hold, more than this share of shots (the last
# one is exempt - it ends where the narration ends) holding under the minimum
# is a fault: the plan is chopping the narration up instead of grouping ideas.
MIN_HOLD_MAX_SHORT_SHARE = 0.35

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

Motion must vary. A video where every shot is ZI has the same problem as one where every shot is ST — motion stops reading as expressive and becomes wallpaper. No single motion code above ~40% of shots. PL and PR specifically are capped tighter, at 10-15% each: they are the easiest motions to reach for by default, and a plan that leans on them flattens into a side-scroll instead of varying with the composition table above.""",
    "MOTION_FIELD_RULE": (
        "- `motion`: ST | ZI | ZO | PL | PR | PU | PD | PV — **must match the motion code in the asset's own filename** (the image was composed for that motion and overscan). Deviate only with a deliberate reason. **ST is rare and special:** use it only on very short holds — roughly 1–2 cues (~2–4 seconds) — and cap it at ~10% of shots, reserving it for compositions that cannot tolerate overscan (tight symmetric close-ups, exact diagrams). Every shot that holds longer must carry motion. Assign motion with composition per the table in Section 5 and vary the codes — no single motion code above ~40% of shots, and PL/PR each capped at 10-15% of shots (they default too easily; lean on ZI/ZO/PU/PD/PV for the rest of the variety)."),
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
    # None = inherit (the channel / global shotlist_max_hold_seconds)
    min_hold: float | None = None
    max_hold: float | None = None
    slots: dict = field(default_factory=dict)

    def slot_values(self) -> dict[str, str]:
        values = dict(STANDARD_SLOTS)
        values.update(self.slots)
        mn = _num(self.min_hold) if self.min_hold else ""
        mx = _num(self.max_hold) if self.max_hold else ""
        return {k: v.replace("[[MIN]]", mn).replace("[[MAX]]", mx)
                for k, v in values.items()}


STANDARD = MotionProfile(
    key="standard",
    label="Standard - all motions, short holds",
    description="The brief's own policy: every motion code, ST only on short "
                "holds (~10% of shots), no code above ~40% (PL/PR ~15%).")

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

MOTION_PRESETS: dict[str, MotionProfile] = {
    p.key: p for p in (STANDARD, STATIC, LONG_HOLDS)}


def get_profile(key) -> MotionProfile:
    """The channel's motion profile; unknown / empty = standard."""
    return MOTION_PRESETS.get(str(key or "").strip().lower(), STANDARD)


_RANGE_HOLD_RULE = (
    "This channel is slow-paced: a shot normally holds between [[MIN]] and "
    "[[MAX]] seconds (hard maximum [[MAX]]s), so one image carries a whole "
    "developing idea. Group every cue that develops the same idea into ONE "
    "shot; a new concrete detail inside the same idea stays on the same "
    "image, and you split only where the idea itself changes (a pivot, a new "
    "subject, a new example). This replaces the 'each new concrete detail "
    "deserves its own visual' guidance above for this channel.")


def resolve_profile(key, min_hold=None, max_hold=None,
                    default_max: float | None = None) -> MotionProfile:
    """The channel's effective profile: its motion preset, with the channel's
    own hold range (seconds) laid over it. The range feeds the prompt AND the
    review gates (briefs.pacing_note, studio.shotlist_pacing) so they cannot
    disagree. A minimum needs a maximum: the preset's, else `default_max`
    (the global shotlist_max_hold_seconds). A min >= max is ignored."""
    base = get_profile(key)
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
