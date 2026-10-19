"""Shotlist skeleton: the app builds the mechanical part, the LLM only writes
the pictures.

The old flow asked the LLM for the whole shotlist JSON: cue ranges, hold
lengths, motion codes, file names, scenes, transitions and the prompts, and
then sent that JSON back and forth between writer and judge. Most of it is
arithmetic the app can do for free (and checks anyway). Here the app

  1. cuts the narration into numbered DRAFT shots (cue range, length, motion
     code) - fine-grained on purpose, every shot within the channel's hold
     limits;
  2. sends them to the LLM in chunks of a few dozen plain lines, in ONE chat
     (the narration travels once, inside the lines - no narration file);
  3. gets back one short line per shot: a NEW image (type, refs, prompt) or
     "REUSE n" (the same picture as an earlier shot) - so the LLM keeps the
     meaning-based grouping the brief asks for, as reuse, while the cue ranges
     stay under the app's control;
  4. assembles the shotlist JSON itself (cues, assets, scenes, refs registry),
     which then goes through the same code checks and judge as before.

Nothing here talks to a browser: `webstages` does the sending.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import briefs, studio

CHUNK_SIZE = 25
SHOT_TYPES = ("SCN", "CU", "INF", "CMP", "PROC", "HYB", "OVR")
# the animated opening turns each image into an ~8 s clip
ANIM_MAX_HOLD = 8.0
_SENTENCE_END = (".", "!", "?", "…", ".\"", "?\"", "!\"", ".”",
                 "?”", "!”")
_NUMBERISH = re.compile(r"^\W*(?:[$€£]\s*)?\d")


class SkeletonError(RuntimeError):
    """The skeleton flow cannot be used (or gave up); the caller falls back
    to the whole-JSON flow."""


class SkeletonUnsupported(SkeletonError):
    """This channel (or chat setup) cannot run the skeleton flow at all -
    the whole-JSON flow plans it instead. Raised before any chat is opened."""


# ---- is it usable? ------------------------------------------------------------

def unsupported_reason(eff: dict, profile) -> str:
    """Why this channel needs the whole-JSON flow ("" = the skeleton works).
    Reveal shots and sound effects change the shape of shots in ways the
    line format does not carry yet (a presentation brief is fine: it only
    says who is on screen). Text-drawn overlays (the shotlist "overlays"
    field) need the whole-JSON writer's SECTION 7C too: the skeleton's line
    format carries no overlays, so a plan made here would silently lose the
    channel's on-image text."""
    if getattr(profile, "reveal", False):
        return "the channel uses reveal shots"
    if getattr(profile, "sfx", ""):
        return "the channel uses sound effects"
    if eff.get("brief_text_overlays"):
        return "the channel draws text overlays (the renderer writes them)"
    return ""


# ---- 1. draft shots -----------------------------------------------------------

@dataclass
class Draft:
    n: int                      # 1-based shot number (what the LLM answers by)
    first: int                  # first cue index
    last: int                   # last cue index
    seconds: float
    text: str
    motion: str = ""
    animate: bool = False       # starts inside the animated opening
    members: list = field(default_factory=list)   # picture: its shot numbers

    @property
    def cues(self) -> str:
        return str(self.first) if self.first == self.last \
            else f"{self.first}-{self.last}"


def _ends_sentence(text: str) -> bool:
    return (text or "").rstrip().endswith(_SENTENCE_END)


def draft_shots(cues: list[dict], max_hold: float, min_hold: float | None = None,
                anim_last_cue: int = 0, short_every: int = 0,
                short_hold: float = 3.5, short_cap: float = 0.0,
                breaks: set | None = None) -> list[Draft]:
    """Cut the cues into consecutive draft shots (a cue number in `breaks`
    always ends a shot: the end of a beat).

    A shot closes at a sentence end once it holds about `target` seconds (and
    at least two cues), or earlier when the next cue starts with a number (a
    statistic arrives - the brief's own split signal), and always before it
    would pass the maximum hold. Cues of the animated opening hold at most
    ANIM_MAX_HOLD. The result is deliberately finer than the final picture
    count: the LLM merges by reusing images. Every `short_every`-th shot aims
    for `short_hold` seconds instead, so a channel that wants a share of
    static (ST) shots has short holds to put them on."""
    if not cues:
        return []
    cap = max(1.0, float(max_hold or studio.SHOT_MAX_HOLD_DEFAULT))
    target = float(min_hold) if min_hold else min(cap * 0.5, 7.0)
    target = min(target, cap)

    def secs(c):
        return (studio._srt_seconds(c["end"]) - studio._srt_seconds(c["start"]))

    def span(group: list[dict]) -> float:
        """A shot's hold as the checks measure it: from the start of its
        first cue to the end of its last, gaps between cues included (the
        sum of the cue lengths undercounts it by up to a second)."""
        return (studio._srt_seconds(group[-1]["end"])
                - studio._srt_seconds(group[0]["start"]))

    def limit(first_cue: int) -> float:
        return min(cap, ANIM_MAX_HOLD) if first_cue <= anim_last_cue else cap

    groups: list[list[dict]] = []
    cur: list[dict] = []
    dur = 0.0
    short = False                 # the open shot is one of the short ones
    for i, c in enumerate(cues):
        d = secs(c)
        if not cur:
            short = (bool(short_every) and len(groups) % short_every == 0
                     and c["index"] > anim_last_cue)
        lim = min(limit(cur[0]["index"]) if cur else limit(c["index"]),
                  short_cap if short and short_cap else cap)
        if cur and span(cur + [c]) > lim:
            groups.append(cur)
            cur, dur = [], 0.0
            short = (bool(short_every) and len(groups) % short_every == 0
                     and c["index"] > anim_last_cue)
        cur.append(c)
        dur = span(cur)
        nxt = cues[i + 1] if i + 1 < len(cues) else None
        if nxt is None:
            break
        if breaks and c["index"] in breaks:
            groups.append(cur)
            cur, dur = [], 0.0
            continue
        goal = min(target, limit(cur[0]["index"]))
        if short:
            close = dur >= min(goal, short_hold)
        else:
            close = (dur >= goal and len(cur) >= 2
                     and _ends_sentence(c["text"]))
        if (not close and len(cur) >= 2 and dur >= 2.5
                and _NUMBERISH.match(nxt["text"])):
            close = True
        if close:
            groups.append(cur)
            cur, dur = [], 0.0
    if cur:
        groups.append(cur)
    # a lone short tail joins the shot before it when that stays within limits
    if len(groups) > 1 and len(groups[-1]) == 1 and not (
            breaks and groups[-2][-1]["index"] in breaks):
        merged = groups[-2] + groups[-1]
        if span(merged) <= limit(merged[0]["index"]):
            groups[-2:] = [merged]
    out = []
    for n, g in enumerate(groups, 1):
        out.append(Draft(
            n=n, first=g[0]["index"], last=g[-1]["index"],
            seconds=round(span(g), 1),
            text=" ".join(c["text"] for c in g),
            animate=g[0]["index"] <= anim_last_cue))
    return out


# ---- 2. motion codes ----------------------------------------------------------

def _targets(profile) -> dict[str, float]:
    """Desired share per motion code, from the channel's profile: ST and the
    grouped codes at their stated shares, the zoom codes (split evenly) up to
    their cap, and whatever the caps leave over spread over the other allowed
    codes (each within its own cap)."""
    allowed = list(profile.allowed or briefs.MOTION_CODES)
    targets: dict[str, float] = {}
    for codes, share in briefs.mix_targets(profile):
        for c in codes:
            targets[c] = targets.get(c, 0.0) + share / len(codes)
    left = max(0.0, 1.0 - sum(targets.values()))

    def cap_of(c: str) -> float:
        cap = profile.code_share_overrides.get(c, profile.code_max_share)
        return 1.0 if cap is None else cap

    def spread(codes: list[str], left: float) -> float:
        share = {c: 0.0 for c in codes}
        free = list(codes)
        while free and left > 1e-9:
            each = left / len(free)
            left = 0.0
            for c in list(free):
                take = min(each, max(0.0, cap_of(c) - targets.get(c, 0.0)
                                     - share[c]))
                share[c] += take
                left += each - take
                if cap_of(c) - targets.get(c, 0.0) - share[c] <= 1e-9:
                    free.remove(c)
        for c, v in share.items():
            if v > 0:
                targets[c] = targets.get(c, 0.0) + v
        return left

    zoom = [c for c in briefs.ZOOM_CODES if c in allowed]
    if briefs.zoom_balanced(profile):
        left = spread(zoom, left)
    others = [c for c in allowed if c not in targets and c not in zoom
              and c != "ST"]
    if left > 1e-9 and others:
        left = spread(others, left)
    if left > 1e-9 and not briefs.zoom_balanced(profile):
        left = spread([c for c in allowed if c != "ST"] or allowed, left)
    if left > 1e-9:                      # nothing else may take it
        pool = [c for c in targets if c != "ST"] or list(targets) or allowed
        for c in pool:
            targets[c] = targets.get(c, 0.0) + left / len(pool)
    return {c: v for c, v in targets.items() if v > 0} or {allowed[0]: 1.0}


def assign_motion(shots: list[Draft], profile) -> None:
    """Fill `motion` on every draft shot, in place.

    Error diffusion: each shot takes the eligible code that is furthest below
    its target share, never the same code twice in a row when another is
    owed. ST is only eligible on short holds; the animated opening uses ZI/ZO
    alternately (a pan needs a wider image than the clip is made from)."""
    allowed = list(profile.allowed or briefs.MOTION_CODES)
    targets = _targets(profile)
    counts = {c: 0 for c in targets}
    zoom = [c for c in briefs.ZOOM_CODES if c in allowed]
    anim_turn = 0
    prev = ""
    for i, s in enumerate(shots):
        if s.animate and zoom:
            s.motion = zoom[anim_turn % len(zoom)]
            anim_turn += 1
            counts[s.motion] = counts.get(s.motion, 0) + 1
            prev = s.motion
            continue
        eligible = []
        for c in targets:
            if c == "ST" and not st_fits(s.seconds, profile):
                continue
            eligible.append(c)
        if not eligible:
            eligible = [c for c in targets if c != "ST"] or list(targets)
        best = max(eligible, key=lambda c: (
            targets[c] * (i + 1) - counts.get(c, 0) - (0.4 if c == prev else 0.0)))
        s.motion = best
        counts[best] = counts.get(best, 0) + 1
        prev = best


def assign_picture_motion(pictures: list[Draft], profile,
                          shots: list[Draft]) -> None:
    """The camera move of every picture (it is the move of the picture's first
    shot), in place.

    Error diffusion like assign_motion, but every picture counts as many
    shots as it carries. A pan or tilt needs its own bigger canvas, and the
    later shots of a picture can only use codes on that same canvas, so a
    pan/tilt picture with three shots is three pan/tilt shots in the mix:
    counting pictures instead of shots overshoots the share. The pan and tilt
    codes compete as ONE class (so a short video still gets its share), and a
    single code stays within its own cap."""
    allowed = list(profile.allowed or briefs.MOTION_CODES)
    targets = _targets(profile)
    flat = ("ST", "ZI", "ZO")
    pans = [c for c in targets if c not in flat]
    pan_target = sum(targets[c] for c in pans)
    zoom = [c for c in briefs.ZOOM_CODES if c in allowed]
    counts = {c: 0 for c in targets}
    pan_count, done, anim_turn, prev = 0, 0, 0, ""

    def cap_of(c: str) -> float:
        cap = profile.code_share_overrides.get(c, profile.code_max_share)
        return 1.0 if cap is None else cap

    for p in pictures:
        w = max(1, len(p.members))
        first = shots[p.members[0] - 1] if p.members else p
        if p.animate and zoom:
            move = zoom[anim_turn % len(zoom)]
            anim_turn += 1
        else:
            def score(c: str) -> float:
                return (targets[c] * (done + w) - counts.get(c, 0)
                        - (0.4 if c == prev else 0.0))
            flat_ok = [c for c in flat if c in targets and not (
                c == "ST" and (w > 1 or not st_fits(first.seconds, profile)))]
            # the class may run one shot past its share, or is below 60% of
            # it; one code stays within its own cap (same slack)
            pan_open = (pan_count + w <= pan_target * (done + w) + 1.0
                        or pan_count < 0.6 * pan_target * (done + w))
            pan_ok = [c for c in pans if pan_open and counts.get(c, 0) + w
                      <= cap_of(c) * (done + w) + 1.0]
            best_flat = max(flat_ok, key=score) if flat_ok else ""
            pan_score = (pan_target * (done + w) - pan_count
                         - (0.4 if prev in pans else 0.0))
            if pan_ok and (not best_flat or pan_score > score(best_flat)):
                move = max(pan_ok, key=score)
            elif best_flat:
                move = best_flat
            else:
                move = max([c for c in targets if c != "ST"] or list(targets),
                           key=score)
        p.motion = move
        first.motion = move
        counts[move] = counts.get(move, 0) + w
        if move in pans:
            pan_count += w
        done += w
        prev = move


def st_fits(seconds: float, profile) -> bool:
    """May a shot of this length be the static ST code? The limits carry the
    same tolerance as the code checks (briefs.hold_limit): with an ST maximum
    of 5 s a 6-7 s shot still qualifies, so the planner is not stricter than
    the check that judges its result. Shots are CUT to the nominal limit (see
    build) - the tolerance only widens who is eligible."""
    if profile.st_max_hold is not None and seconds + 0.05 > briefs.hold_limit(
            profile.st_max_hold, profile.st_hold_tol):
        return False
    if profile.static_long_hold is not None and seconds >= briefs.hold_limit(
            profile.static_long_hold, profile.static_hold_tol):
        return False
    return True


def _short_params(profile) -> tuple[int, float, float]:
    """(short_every, short_hold, short_cap) for draft_shots: how often a
    short shot is cut so a channel that wants ST shots has holds to put them
    on."""
    short_every, short_hold, short_cap = 0, 3.5, 0.0
    st_share = dict(briefs.mix_targets(profile)).get(("ST",))
    if st_share and profile.st_max_hold:
        short_every = max(2, int(round(1.0 / st_share)) - 2)
        short_hold = min(3.5, profile.st_max_hold * 0.7)
        short_cap = max(1.0, profile.st_max_hold - 0.1)
    return short_every, short_hold, short_cap


def build(cues: list[dict], profile, max_hold: float,
          anim_last_cue: int = 0) -> list[Draft]:
    short_every, short_hold, short_cap = _short_params(profile)
    shots = draft_shots(cues, max_hold, profile.min_hold, anim_last_cue,
                        short_every, short_hold, short_cap)
    assign_motion(shots, profile)
    return shots


# ---- 2b. grouping by the LLM ----------------------------------------------------

# The LLM first GROUPS the numbered CUES into beats (a reply of short
# "first-last" lines); the app checks them (every cue once, in order, no
# runaway beat), cuts every long beat into camera shots of the same picture
# (the later shots simply REUSE the beat's image), and only then asks for the
# pictures - one per beat. Off, or any trouble = the app's own draft shots go
# to the picture step as they are.
GROUPING = True
BEAT_MAX_FACTOR = 1.0            # a beat (one picture) holds at most the channel's max hold

_GROUP_LINE = re.compile(
    r"^[\s>*`#\-•]*\**\s*(?:GROUP\s*)?(?:(BEAT)\s+)?"
    r"(\d+)(?:\s*[-–—]\s*(\d+))?\s*\**"
    r"\s*(?:[:|].*)?$", re.I)


def parse_grouping(text: str) -> tuple[list[tuple[int, int]], set[int]]:
    """((first, last) per group line, cue numbers of groups that start a new
    MAIN BEAT). A line may be prefixed with `BEAT` to mark it as the first
    picture of a new chapter of the video; unmarked lines continue the
    current one."""
    groups: list[tuple[int, int]] = []
    beat_starts: set[int] = set()
    for line in (text or "").splitlines():
        m = _GROUP_LINE.match(line.strip())
        if m:
            a = int(m.group(2))
            groups.append((a, int(m.group(3)) if m.group(3) else a))
            if m.group(1):
                beat_starts.add(a)
    return groups, beat_starts


def parse_groups(text: str) -> list[tuple[int, int]]:
    """The (first, last) cue numbers of every group line in a reply, in the
    order given."""
    return parse_grouping(text)[0]


# a trailing shot shorter than this reads as a flicker, not a beat: the
# mechanical cut left a 0.3s one-word tail behind its neighbour (cue 298,
# "demand.") - fold it back instead of spending an image boundary on it
SUBSECOND_TAIL = 1.0


def _merge_subsecond_tails(shots: list[Draft], by_index: dict,
                           protected_starts: set[int]) -> int:
    """Fold a trailing shot shorter than SUBSECOND_TAIL seconds back into
    the shot before it. The cut exists only because the cap or a sentence
    end split there; a one-word flicker is not a visual beat. A shot that
    STARTS a picture (in `protected_starts`) is the writer's semantic
    boundary and is never folded; the fold keeps the neighbour's motion, so
    both must share a canvas family. In place; returns folds made."""
    if len(shots) < 2:
        return 0
    folded = 0
    i = 1
    while i < len(shots):
        s, prev = shots[i], shots[i - 1]
        span = _cue_span(by_index, s.first, s.last)
        if span >= SUBSECOND_TAIL or s.first in protected_starts:
            i += 1
            continue
        if not ({s.motion, prev.motion} <= {"ZI", "ZO", "ST"}
                or s.motion == prev.motion):
            i += 1
            continue
        prev.last = s.last
        prev.text = f"{prev.text} {s.text}"
        prev.seconds = round(_cue_span(by_index, prev.first, prev.last), 1)
        del shots[i]
        folded += 1
        # do not advance: the merged shot may itself now be a tail
    return folded


def groupable(cues: list[dict], anim_last_cue: int = 0) -> list[dict]:
    """The cues the writer groups: all but the animated opening, whose shots
    the app keeps apart (each needs its own animated picture)."""
    return [c for c in cues if c["index"] > anim_last_cue]


def _cue_span(by_index: dict, first: int, last: int) -> float:
    return (studio._srt_seconds(by_index[last]["end"])
            - studio._srt_seconds(by_index[first]["start"]))


def group_problems(cues: list[dict], groups: list[tuple[int, int]],
                   cap: float) -> list[str]:
    """What is wrong with the LLM's grouping of `cues` (empty = fine): every
    cue in exactly one group, in order, and no beat longer than
    BEAT_MAX_FACTOR x the channel's maximum hold (measured start to end)."""
    if not cues:
        return []
    by_index = {c["index"]: c for c in cues}
    order = [c["index"] for c in cues]
    pos = {i: p for p, i in enumerate(order)}
    problems: list[str] = []
    if not groups:
        return ["no group lines were found"]
    limit = float(cap) * BEAT_MAX_FACTOR
    expected = 0                          # position of the next cue to cover
    for a, b in groups:
        if b < a:
            problems.append(f"group {a}-{b} runs backwards")
            continue
        if a not in pos or b not in pos:
            problems.append(f"group {a}-{b} is outside cues "
                            f"{order[0]}-{order[-1]}")
            continue
        if pos[a] != expected:
            want = order[expected] if expected < len(order) else order[-1]
            problems.append(
                f"group {a}-{b} should start at cue {want} (every cue in "
                f"exactly one group, in order)")
        expected = max(expected, pos[b] + 1)
        span = _cue_span(by_index, a, b)
        if span > limit + 1e-9:
            problems.append(f"group {a}-{b} holds {span:.0f}s - a beat may "
                            f"hold at most {limit:.0f}s, split it")
    if expected != len(order):
        problems.append(f"the groups end at cue "
                        f"{order[min(expected, len(order)) - 1]} but the last "
                        f"cue is {order[-1]}")
    return problems


def regroup(cues: list[dict], groups: list[tuple[int, int]], profile,
            max_hold: float, anim_last_cue: int = 0
            ) -> tuple[list[Draft], list[Draft]]:
    """(shots, pictures) once the beats are known.

    `shots` are the final shots: every beat cut at its sentence ends into
    shots of at most the maximum hold (short ones where the channel wants ST
    shots), the animated opening kept as the app drafted it. `pictures` is
    what the writer is asked for - ONE entry per beat (the animated shots one
    each), numbered 1..P, covering the whole beat; `members` lists the shot
    numbers behind it, the first being the shot that carries the new picture
    and the others reusing it."""
    breaks = {b for _a, b in groups}
    if anim_last_cue:
        breaks.add(anim_last_cue)
    short_every, short_hold, short_cap = _short_params(profile)
    shots = draft_shots(cues, max_hold, profile.min_hold, anim_last_cue,
                        short_every, short_hold, short_cap, breaks=breaks)
    assign_motion(shots, profile)
    starts = {a for a, _b in groups}
    if anim_last_cue:
        starts.add(anim_last_cue + 1)     # first shot after the opening
    by_index = {c["index"]: c for c in cues}
    _merge_subsecond_tails(shots, by_index, starts)
    pictures: list[Draft] = []
    for s in shots:
        if s.animate or s.first in starts or not pictures:
            pictures.append(Draft(
                n=len(pictures) + 1, first=s.first, last=s.last,
                seconds=s.seconds, text=s.text, motion=s.motion,
                animate=s.animate, members=[s.n]))
        else:
            p = pictures[-1]
            p.members.append(s.n)
            p.last = s.last
            p.text = f"{p.text} {s.text}"
            p.seconds = round(_cue_span(by_index, p.first, s.last), 1)
    # Picture canvases are chosen after grouping, counted per SHOT: every
    # shot on a pan/tilt picture is a pan/tilt too, so a picture counts as
    # many shots as it carries.
    assign_picture_motion(pictures, profile, shots)
    return shots, pictures


def expand(pictures: list[Draft], ans: "Answers") -> "Answers":
    """The writer's answers (by picture number) as answers by shot number:
    the picture's first shot carries the new image or the reuse, every later
    shot of the beat reuses it."""
    out = Answers()
    first = {p.n: p.members[0] for p in pictures}
    for p in pictures:
        if p.n in ans.new:
            out.new[first[p.n]] = ans.new[p.n]
        elif p.n in ans.reuse:
            out.reuse[first[p.n]] = first[ans.reuse[p.n]]
        for m in p.members[1:]:
            out.reuse[m] = first[p.n]
    out.beats = {first[b] for b in ans.beats if b in first}
    out.refs = dict(ans.refs)
    return out


def _cue_line(c: dict) -> str:
    secs = (studio._srt_seconds(c["end"]) - studio._srt_seconds(c["start"]))
    return f"{c['index']} | {secs:.1f}s | {c['text']}"


GROUPING_ONLY = (
    "CURRENT TASK: CUE GROUPING ONLY. Do not write a shotlist, image "
    "prompts, filenames, references, camera moves or JSON; character "
    "details are handled later.")


def hold_range_note(profile, cues: list[dict] | None = None) -> str:
    """The channel's own hold window as one grouping line ("" = the standard
    fast pacing, where meaning alone decides). A slow-paced channel's beats
    are MEANT to be long: without the window the writer splits every idea at
    a sentence end and the channel gets far more images than it wants."""
    if not (getattr(profile, "min_hold", None)
            and getattr(profile, "max_hold", None)):
        return ""
    cue = max(0.5, _mean_cue(cues) or 4.0)
    lo = max(2, int(math.floor(profile.min_hold / cue)))
    hi = max(lo, int(math.ceil(profile.max_hold / cue)))
    return (f"This channel is slow-paced: a shot normally holds "
            f"{profile.min_hold:g}-{profile.max_hold:g}s of narration, so "
            f"KEEP one beat while the idea keeps developing and prefer "
            f"fewer, longer beats (a picture on this narration commonly "
            f"carries about {lo}-{hi} cues). The limit above is a ceiling, "
            f"not a target.")


def group_prompt(title: str, genre: str, cues: list[dict], cap: float,
                 anim_last_cue: int = 0, direction: str = "",
                 hold_note: str = "") -> str:
    first, last = cues[0]["index"], cues[-1]["index"]
    limit = float(cap) * BEAT_MAX_FACTOR
    anim = ""
    if anim_last_cue:
        anim = (f" Cues 1-{anim_last_cue} are the animated opening: the app "
                f"handles them itself, so they are not listed - start at "
                f"{first}.")
    return (
        "You are the story editor for a YouTube video"
        + (f" titled \"{title}\"" if title else "")
        + (f" ({genre})" if genre else "")
        + ". The narration is cut into numbered CUES (line: cue number | "
        "length | narration). In this step you ONLY decide how the cues "
        "GROUP into BEATS: a beat is one developing idea that gets ONE "
        "picture, so a beat never runs longer than the limit below: a longer "
        "idea becomes consecutive beats. Do not write image prompts yet.\n\n"
        "RULES:\n"
        f"- Reply with one group per line, as first-last cue numbers: `1-4`, "
        f"`5`, `6-12`. Every cue belongs to exactly one group, in order, "
        f"none missing or repeated, starting at {first} and ending at "
        f"{last}.{anim}\n"
        f"- A beat holds at most {limit:g} seconds of narration (the app "
        f"measures it and cuts any longer beat itself). Follow meaning for "
        f"where a beat ends; there is no target count.\n"
        f"- A MAIN BEAT is a chapter of the video, several beats long (hook, "
        f"problem, mechanism, evidence, solution, conclusion - roughly 8-14 "
        f"per video). Prefix the line of the group that STARTS a new main "
        f"beat with `BEAT`: `BEAT 45-48`. Unmarked lines continue the "
        f"current main beat; the first group starts the first one."
        + (f"\n- {hold_note}\n" if hold_note else "\n")
        + grouping_brief() + "\n\n"
        + GROUPING_ONLY + "\n\n"
        + ("CREATOR DIRECTION (follow it):\n" + direction.strip() + "\n\n"
           if direction.strip() else "")
        +
        "- Answer with ONLY the group lines - no commentary, no prompts.\n\n"
        f"CUES {first}-{last}:\n" + "\n".join(_cue_line(c) for c in cues))


def grouping_brief() -> str:
    """The grouping rules in a few lines, written for this step only (the
    picture step has its own rules)."""
    return (
        "HOW TO GROUP (the narration decides, not the clock):\n"
        "- Start a NEW beat when a number, statistic or price arrives; a "
        "second character, object or place enters; the narration pivots "
        "(problem to evidence, question to answer, claim to example, "
        "description to instruction); the action changes; or a list of "
        "parallel items ends and the payoff begins.\n"
        "- KEEP one beat while the same idea continues or is rephrased. List "
        "items and the parts of one thing (legs, eyes, arms) belong "
        "together; one sentence that belongs to a larger idea is not a beat "
        "of its own.\n"
        "- A cue is never split, and a sentence that runs across two cues "
        "stays in one beat. Do not break a number phrase just because the "
        "next cue starts with a number.\n"
        "- When the last cue of an idea ends MID-SENTENCE (\"They are\" / "
        "\"Food\"), extend the picture through the cue that completes the "
        "thought - a picture that ends on an unfinished phrase reads as a "
        "cut in the middle of the sentence. Finish the thought first, then "
        "change the picture.\n"
        "- One cue per beat is a failure unless that cue is a whole idea; so "
        "is one beat that joins unrelated ideas.\n"
        "- Follow meaning for where a beat ends, not a target count; but one "
        "beat never runs past the length limit - a longer idea becomes "
        "consecutive beats.")


# the writer may leave out at most this share of the cues; the app patches
# small gaps, a grouping with many is sent back as a failure
REPAIR_MAX_SHARE = 0.10


def uncovered_share(cues: list[dict], groups: list[tuple[int, int]]) -> float:
    """The share of `cues` that no group names."""
    if not cues:
        return 0.0
    have = {c["index"] for c in cues}
    covered: set[int] = set()
    for lo, hi in groups:
        lo, hi = (lo, hi) if lo <= hi else (hi, lo)
        covered.update(i for i in have if lo <= i <= hi)
    return 1.0 - len(covered) / len(have)


def repair_groups(cues, groups, cap):
    """Fix ordered coverage and overlong spans locally, preserving boundaries."""
    if not cues: return [], []
    first,last=cues[0]["index"],cues[-1]["index"]
    by_index={c["index"]:c for c in cues}
    covered=[];cursor=first
    for lo,hi in sorted(tuple(sorted(g)) for g in groups):
        if lo<first or hi>last:
            return groups,[f"range {lo}-{hi} is outside available cues {first}-{last}"]
        if lo>cursor:covered.append((cursor,lo-1))
        lo=max(lo,cursor)
        if hi>=lo:covered.append((lo,hi));cursor=hi+1
    if cursor<=last:covered.append((cursor,last))
    result=[];limit=cap*BEAT_MAX_FACTOR
    for lo,hi in covered:
        while _cue_span(by_index,lo,hi)>limit:
            fits=[i for i in range(lo,hi+1) if _cue_span(by_index,lo,i)<=limit]
            if not fits:return groups,[f"cue {lo} alone exceeds the duration limit"]
            ends=[i for i in fits if _ends_sentence(by_index[i]["text"])]
            end=ends[-1] if ends else fits[-1]
            result.append((lo,end));lo=end+1
        result.append((lo,hi))
    return result,group_problems(cues,result,cap)


def snap_beat_starts(markers: set[int], groups: list[tuple[int, int]]
                     ) -> tuple[set[int], int]:
    """Markers re-anchored to the final group starts: a chapter begins at
    the first group that starts at or after the marked cue, so a repair or
    a judge correction that moved a marked boundary keeps its chapter
    instead of dropping it. Returns (markers, moved count); a marker that
    lands on the first group is dropped there (chapter 1 is implicit)."""
    starts = sorted(a for a, _b in groups)
    if not starts:
        return set(), len(markers)
    out: set[int] = set()
    moved = 0
    for s in markers:
        nxt = next((a for a in starts if a >= s), None)
        if nxt is None or nxt == starts[0]:
            continue                    # chapter 1 is implicit
        out.add(nxt)
        moved += 1 if nxt != s else 0
    return out, moved


# a scene holding more sub-beats than this (and no image boundary inside to
# split at) is a writer that stopped marking - split it mechanically rather
# than ship S13_01...S13_54
MAX_SUBBEATS_PER_SCENE = 30


def _cap_scene_size(shots: list[Draft], scene_of: dict[int, int],
                    ans: "Answers",
                    cap: int = MAX_SUBBEATS_PER_SCENE) -> dict[int, int]:
    """scene_of with every run of more than `cap` shots in one scene split
    at the shot that starts a NEW image (so filenames keep their scene).
    Scenes with no image boundary inside are left alone - splitting one
    picture's reuse run would put one asset under two scene labels."""
    out: dict[int, int] = {}
    scene, run, last_writer = None, 0, None
    for s in shots:
        boundary = s.n in ans.new
        if scene_of[s.n] != last_writer:
            scene, run = scene_of[s.n], 0     # the writer's own boundary
        elif boundary and run >= cap:
            scene += 1                        # mechanical split under the cap
            run = 0
        last_writer = scene_of[s.n]
        out[s.n] = scene
        run += 1
    return out


def group_judge_prompt(cues: list[dict], groups: list[tuple[int, int]],
                       cap: float, direction: str = "",
                       hold_note: str = "") -> str:
    """One review: the judge returns the COMPLETE corrected list of groups
    (the same line format the writer used), which the app checks itself."""
    first, last = cues[0]["index"], cues[-1]["index"]
    text = ("You are the grouping judge. A writer grouped the narration cues "
            "into beats (one picture each). Check the grouping against the "
            "rules below using the EXACT cue text, and reply with the "
            f"COMPLETE corrected list of groups: one first-last cue range per "
            f"line, every cue exactly once, in order, from {first} to {last}. "
            "Change a boundary only for a material problem - unrelated ideas "
            "joined, one idea fragmented, a pivot cut too early or too late. "
            "If the grouping is already good, repeat it unchanged. Cues are "
            "indivisible: a cue may end one sentence and start another, so "
            "accept a reasonable boundary when a perfect cut would split a "
            "cue. A group whose line is prefixed `BEAT` starts a new MAIN "
            "BEAT (a chapter of the video): keep those markers, and move one "
            "when a chapter boundary lands elsewhere. Reply with ONLY the "
            "group lines - no JSON, no commentary, no image prompts.\n\n"
            + grouping_brief() + "\n\n" + GROUPING_ONLY
            + f"\nMaximum picture-group span: {cap * BEAT_MAX_FACTOR:g}s. "
            "Each group is one picture and must not run past that span.\n"
            + (f"\n{hold_note}\n" if hold_note else ""))
    if direction.strip():
        text += "CREATOR DIRECTION:\n" + direction.strip() + "\n"
    for a, b in groups:
        text += f"\nGROUP {a}-{b}:\n" + "\n".join(
            f"[{c['index']}] {c['text']}" for c in cues if a <= c["index"] <= b) + "\n"
    return text


def group_fix_prompt(problems: list[str]) -> str:
    return ("Not accepted yet:\n- " + "\n- ".join(problems)
            + "\n\nReply again with the COMPLETE list of groups (every cue, "
            "first-last per line, nothing else), fixing these.")


# ---- 3. prompts ---------------------------------------------------------------

def shot_line(s: Draft) -> str:
    flag = " | ANIMATE" if s.animate else ""
    cams = (f" (the app cuts it into {len(s.members)} camera shots of this "
            f"picture)" if len(s.members) > 1 else "")
    return (f"{s.n} | cues {s.cues} | {s.seconds:g}s{cams} | {s.motion}"
            f"{flag} | {s.text}")


def chunks(shots: list[Draft], size: int = CHUNK_SIZE) -> list[list[Draft]]:
    return [shots[i:i + size] for i in range(0, len(shots), size)]


def _template(cfg) -> str:
    """The raw brief template this channel plans with: the custom manifest
    brief when one is configured (the same file the whole-JSON flow renders),
    else the built-in template. Raises SkeletonUnsupported when the custom
    template is missing (the whole-JSON flow reports it the same way)."""
    if cfg.studio_manifest_brief:
        p = Path(cfg.studio_manifest_brief)
        path = p if p.is_absolute() else cfg.base_dir / p
        if not path.exists():
            raise SkeletonUnsupported(
                f"studio.manifest_brief points at {path}, which does not "
                "exist - fix or clear it in config.yaml")
        return Path(path).read_text(encoding="utf-8")
    return briefs.TEMPLATE_PATH.read_text(encoding="utf-8")


def _section(name: str, template_text: str | None = None) -> str:
    """One `## SECTION ...` block of the brief template, verbatim."""
    text = template_text if template_text is not None \
        else briefs.TEMPLATE_PATH.read_text(encoding="utf-8")
    m = re.search(r"^## SECTION " + re.escape(name) + r"\b.*?(?=^## |\Z)", text,
                  re.S | re.M)
    return m.group(0).strip() if m else ""


def _ref_subject_rule(template_text: str | None = None) -> str:
    text = template_text if template_text is not None \
        else briefs.TEMPLATE_PATH.read_text(encoding="utf-8")
    m = re.search(r"^\*\*A reference prompt describes ONLY.*?$", text, re.M)
    return m.group(0) if m else ""


_REPLY_BEAT_BULLET = (
    "- To start a new MAIN BEAT (hook, problem, mechanism, evidence, "
    "solution...) put a line `BEAT <n>` before the line of the shot that "
    "begins it. Shot 1 always starts the first beat. Beats are semantic "
    "sections, not scene changes - usually a handful per video.\n")
_REPLY_FORMAT = f"""\
HOW TO ANSWER - plain text lines, nothing else (no JSON, no code block, no commentary, no summary). One line per shot, in order, using the shot's own number:

  <n> | <TYPE> | <refs or -> | <image prompt>          a NEW image
  <n> | REUSE <m>                                      the same picture as the earlier shot <m>

- TYPE is one of SCN, CU, INF, CMP, PROC, HYB, OVR. refs are names from the reference list, comma-separated (the first is the dominant subject), or `-` for none.
- A shot flagged ANIMATE gets its movement note at the end of its line: `... | <image prompt> || <ANIMATE note>`; a flagged shot is never a REUSE.
{_REPLY_BEAT_BULLET}- To invent a reference that is not in the list, add a line `REF <CH_/BG_/OBJ_NAME> | <what it looks like>` (before it is first used).
- Answer EVERY shot of the batch. Do not renumber, merge or skip shots; the cue ranges and the camera motion are fixed by the app."""

_REPLY_FORMAT_GROUPED = f"""\
HOW TO ANSWER - plain text lines, nothing else (no JSON, no code block, no commentary, no summary). One line per picture, in order, using the picture's own number:

  <n> | <TYPE> | <refs or -> | <image prompt>          a NEW image
  <n> | REUSE <m>                                      the same picture as the earlier picture <m>

- TYPE is one of SCN, CU, INF, CMP, PROC, HYB, OVR. refs are names from the reference list, comma-separated (the first is the dominant subject), or `-` for none.
- A shot flagged ANIMATE gets its movement note at the end of its line: `... | <image prompt> || <ANIMATE note>`; a flagged shot is never a REUSE.
- The MAIN BEATS (scenes) were already decided in the grouping step: do not write `BEAT <n>` lines, they would be ignored.
- To invent a reference that is not in the list, add a line `REF <CH_/BG_/OBJ_NAME> | <what it looks like>` (before it is first used).
- Answer EVERY picture of the batch. Do not renumber, merge or skip pictures; the cue ranges and the camera motion are fixed by the app."""


_PICTURE_INTRO = (
    "You are a visual director and image-prompt engineer for a "
    "high-retention YouTube channel. The app has already grouped the "
    "narration into numbered PICTURES (cue range, length and camera motion "
    "are fixed); where a picture runs long the app cuts it into camera shots "
    "of that same picture itself. Your only job is the pictures: write the "
    "image prompt for each picture, or REUSE an earlier picture when the "
    "narration clearly returns to it.")

_PICTURE_REUSE = (
    "WHEN TO USE A NEW IMAGE AND WHEN TO REUSE:\n"
    "- Every picture below is one developing idea and normally gets its OWN "
    "image. Compose it to carry the WHOLE narration shown, since the app may "
    "cut it into several camera shots of this one picture.\n"
    "- REUSE an earlier picture only when the narration clearly returns to "
    "that same idea or visual (a callback, a recurring comparison, a list "
    "revisited). Never reuse when the concept changed, and never create an "
    "image just to pad variety.")


def rules_text(profile, allow_refs: bool, supplied_refs: list[str],
               animation_note: str = "", density: str = "",
               limit: int = 2400, grouped: bool = False,
               template_text: str | None = None,
               cues: list[dict] | None = None,
               beats_from_grouping: bool = False) -> str:
    kinds = briefs.types_text(profile.types)
    parts = [
        _PICTURE_INTRO if grouped else
        "You are a visual director and image-prompt engineer for a "
        "high-retention YouTube channel. The app has already cut the "
        "narration into numbered SHOTS (cue range, length and camera motion "
        "are fixed). Your only job is the pictures: for each shot, decide "
        "whether the narration needs a NEW image or REUSES an earlier one, "
        "and write the image prompt for each new image.",
        _PICTURE_REUSE if grouped else
        "WHEN TO USE A NEW IMAGE AND WHEN TO REUSE - the narration decides:\n"
        "- A genuinely different visual idea starts a new image: a number, "
        "statistic or price arrives; a second character, object or location "
        "enters; the narration pivots (problem to evidence, question to "
        "answer, claim to example); the action changes; a list of parallel "
        "items ends and the payoff begins.\n"
        "- The same idea continues or is rephrased: REUSE the earlier picture. "
        "List items share one image; body parts described in turn share one "
        "figure; do not build an infographic for a single sentence that "
        "belongs to a larger visual idea. Never reuse when the concept "
        "clearly changed, and never create an image just because a cue began "
        "or to pad variety.\n"
        + _picture_span_text(profile, _mean_cue(cues)),
        "CAMERA MOTION CODES (the app fixed one per shot). A pan or tilt "
        "(PL, PR, PU, PD, PV) is binding - compose the image for it. ST, ZI "
        "and ZO share one canvas and the app may swap between them to balance "
        "the channel's mix, so compose those centred with room to push in or "
        "pull out:\n" + "\n".join(
            f"- {c}: {briefs.MOTION_CODE_INFO[c][0]}"
            for c in (profile.allowed or briefs.MOTION_CODES)),
        "VISUAL FUNCTION - choose the strongest type per image: SCN scene / "
        "character / environment; CU close-up / detail; INF infographic / "
        "diagram; CMP comparison (A vs B, before/after, myth vs evidence); "
        "PROC process / stages; HYB scene + explanatory graphics; OVR "
        "conceptual overview. Vary camera angle, distance, subject placement, "
        "environment, focal object, metaphor, diagram structure, scale and "
        "subject count; avoid 'person standing + floating icons' on every "
        "image. For finance, statistics or data-heavy narration show the "
        "actual mechanism (compound growth = growing stacks over time; "
        "inflation = the same basket costing more) - the visual must teach "
        "before any editor-added text." + (("\n" + kinds) if kinds else ""),
        "IMAGE PROMPTS: describe ONLY what makes this image different from "
        "every other - subject and what it is DOING, the specific location, "
        "every prop the narration mentions, the spatial layout (who is where, "
        "foreground / background), the composition for the shot's camera "
        "motion (leave overscan in the direction of a pan, centred subject "
        "for a push in or pull out), and where the light comes from. No style "
        "language at all (palette, grade, 'cinematic', film grain, mood "
        "adjectives) - the channel style is added by the app. Each prompt "
        f"stays under {limit} characters. No two prompts may be identical."
        + studio.density_planner_block(density),
        _section("8", template_text),
        _section("11A", template_text),
        _section("11B", template_text),
        _section("11C", template_text),
    ]
    if not allow_refs:
        parts.append(
            "REFERENCES ARE OFF for this channel: write no refs and no REF "
            "lines, and never mention an attachment: if the bible says to "
            "write \"the protagonist from the attached reference\" or to "
            "attach a character file, that does not apply here - there is no "
            "reference image. With no reference image to carry anything, "
            "EVERY image prompt must be fully self-contained: re-describe "
            "recurring characters and objects in full each time they appear "
            "(look, wardrobe, the exact prop) so they stay consistent, and "
            "never point to another picture (\"same as picture 1\").")
    else:
        listing = ("\n".join(f"- {n.rsplit('.', 1)[0]}" for n in supplied_refs)
                   if supplied_refs else "(none supplied)")
        parts.append(
            "REFERENCES: a reference is a named picture the image generator "
            "attaches so a character, place or object stays the same across "
            "shots. Attach refs per image only for what that image actually "
            "shows (most images carry none or one; at most 10, prefer 1-3; "
            "diagrams (INF/PROC) carry none). The first ref is the dominant "
            "subject. Any shot that shows a person IS the bible's recurring "
            "character when there is one - cast them in the prompt and attach "
            "their ref; a supplied character must appear somewhere in the "
            "plan. Supplied references (use these names exactly, never write "
            f"a REF line for them):\n{listing}\n"
            "Every other reference you need is invented with a REF line "
            "(names: CH_ characters, BG_ backgrounds, OBJ_ objects, CAPS and "
            "digits joined by underscores, e.g. CH_MAYA, BG_BATHROOM_01), at "
            "most 20 for the whole video - invent only what the story "
            "genuinely reuses.\n" + _ref_subject_rule(template_text))
    if animation_note:
        parts.append(animation_note.strip())
    if getattr(profile, "custom_rules", ""):
        parts.append(
            "CREATOR'S OWN MOTION RULES (the app already fixed each shot's "
            "code; follow these where they shape what the image shows):\n"
            + getattr(profile, "custom_rules", "").strip())
    # in the grouped flow the scenes come from the grouping's BEAT markers,
    # so the reply format drops the writer's own BEAT-line instruction - it
    # only applies to the ungrouped flow (and the markerless fallback)
    parts.append(_REPLY_FORMAT_GROUPED
                 if grouped and beats_from_grouping else _REPLY_FORMAT)
    return "\n\n".join(p for p in parts if p)


def _picture_span_text(profile, mean_cue: float = 4.0) -> str:
    """The shots-per-picture guidance for the writer: the fixed 'two or
    three' heuristic by default; on a slow-paced channel (a minimum hold is
    set) the range follows the channel's own hold window against the
    narration's average cue length, so the writer does not over-split beats
    into more images than the channel wants."""
    if profile.min_hold and profile.max_hold:
        cue = max(0.5, mean_cue or 4.0)
        lo = max(2, int(math.floor(profile.min_hold / cue)))
        hi = max(lo, int(math.ceil(profile.max_hold / cue)))
        return (
            "- PICTURES CARRY SHOTS: the app splits every picture into "
            f"camera shots itself. On this slow-paced channel a picture "
            f"normally carries {lo}-{hi} shots of one developing idea "
            f"(its beats hold {profile.min_hold:g}-{profile.max_hold:g}s "
            f"of narration at about {cue:.1f}s per cue); do not split an "
            "idea into several pictures just because it runs long.")
    return ("- Prefer several shots per image: most pictures should carry "
            "two or three shots of one developing idea.")


def _mean_cue(cues: list[dict] | None) -> float:
    """The average cue length in seconds (4 s when nothing is known): the
    rough size of one sentence of narration. Cue timestamps are SRT strings,
    the same format the grouping lines show."""
    secs = [studio._srt_seconds(c.get("end")) - studio._srt_seconds(c.get("start"))
            for c in (cues or [])]
    secs = [s for s in secs if s > 0]
    return sum(secs) / len(secs) if secs else 4.0


def first_prompt(rules: str, style: str, bible: str, direction: str,
                 batch: list[Draft], total: int,
                 grouped: bool = False) -> str:
    blocks = [rules,
              "CHANNEL VISUAL STYLE (the app adds it to every prompt - do not "
              "repeat it in the prompts):\n"
              + ((style or "").strip() or "(none supplied)")]
    if (bible or "").strip():
        blocks.append("CHARACTER / REFERENCE BIBLE (preserve these "
                      "characters, environments and objects):\n"
                      + bible.strip())
    else:
        blocks.append("CHARACTER / REFERENCE BIBLE: this channel has none. "
                      "Do not ask for one - plan without it.")
    if (direction or "").strip():
        blocks.append("CREATOR DIRECTION (follow it):\n" + direction.strip())
    if grouped:
        blocks.append("The app applied your grouping: every beat below is now "
                      "ONE picture, numbered from 1. Where a beat runs long "
                      "the app cuts it into camera shots of that same "
                      "picture itself - you write ONE picture for the whole "
                      "beat (compose it to carry the whole narration shown) "
                      "and never answer for the camera shots. Now write the "
                      "pictures.")
    blocks.append(batch_block(batch, total, first=True))
    return "\n\n---\n\n".join(blocks)


def batch_block(batch: list[Draft], total: int, first: bool = False) -> str:
    unit = "picture" if batch[0].members else "shot"
    head = (f"{unit.upper()}S {batch[0].n}-{batch[-1].n} of {total} "
            f"(line: number | cue range | length | camera motion | narration):")
    tail = (f"\n\nAnswer with one line per {unit} above, "
            f"{batch[0].n} to {batch[-1].n}."
            + ("" if first else
               " Same rules, style and bible as in this chat; you can see "
               "your earlier answers, so keep characters, places and "
               "references consistent and REUSE earlier pictures where the "
               "narration returns to them."))
    return head + "\n" + "\n".join(shot_line(s) for s in batch) + tail


def next_prompt(batch: list[Draft], total: int) -> str:
    return batch_block(batch, total)


def fix_prompt(problems: list[str], batch: list[Draft]) -> str:
    return ("Not accepted yet - fix ONLY these shots and answer with just "
            "their lines (same format):\n- " + "\n- ".join(problems)
            + "\n\nThe shots again:\n"
            + "\n".join(shot_line(s) for s in batch))


# ---- 4. parsing ---------------------------------------------------------------

_LINE = re.compile(r"^[\s>*`#\-•]*\**\s*(\d+)\s*\**\s*\|\s*(.+?)\s*$")
_BEAT = re.compile(r"^[\s>*`#\-•]*\**\s*BEAT\s+(\d+)\b", re.I)
_REF = re.compile(
    r"^[\s>*`#\-•]*\**\s*REF\s+([A-Za-z0-9_]+)\s*\|\s*(.+?)\s*$", re.I)
_REUSE = re.compile(r"^REUSE\s+#?(\d+)\b", re.I)


@dataclass
class Answers:
    new: dict = field(default_factory=dict)      # n -> {type, refs, prompt, anim}
    reuse: dict = field(default_factory=dict)    # n -> m
    beats: set = field(default_factory=set)
    refs: dict = field(default_factory=dict)     # name -> prompt

    def answered(self) -> set:
        return set(self.new) | set(self.reuse)

    def update(self, other: "Answers") -> None:
        self.new.update(other.new)
        self.reuse.update(other.reuse)
        self.beats |= other.beats
        self.refs.update(other.refs)


def parse_reply(text: str) -> Answers:
    ans = Answers()
    for raw in (text or "").splitlines():
        line = raw.strip().strip("`")
        if not line:
            continue
        m = _BEAT.match(line)
        if m:
            ans.beats.add(int(m.group(1)))
            continue
        m = _REF.match(line)
        if m:
            ans.refs[m.group(1).upper()] = m.group(2).strip()
            continue
        m = _LINE.match(line)
        if not m:
            continue
        n, rest = int(m.group(1)), m.group(2)
        r = _REUSE.match(rest)
        if r:
            ans.reuse[n] = int(r.group(1))
            ans.new.pop(n, None)
            continue
        fields = [f.strip() for f in rest.split("|", 2)]
        if len(fields) < 3:
            continue
        kind = fields[0].upper().strip("*` ")
        if kind not in SHOT_TYPES:
            continue
        refs = [x.strip("*` ") for x in re.split(r"[,;]", fields[1])
                if x.strip("*` ") and x.strip("*` ") not in ("-", "none", "None")]
        prompt, anim = fields[2], ""
        if "||" in prompt:
            prompt, anim = (p.strip() for p in prompt.rsplit("||", 1))
        if not prompt:
            continue
        ans.new[n] = {"type": kind, "refs": refs, "prompt": prompt,
                      "anim": anim}
        ans.reuse.pop(n, None)
    return ans


def covers(text: str, batch: list[Draft]) -> bool:
    """True when the reply holds a line for every shot of `batch` (the
    transport's `ready` test: a half-written reply is not an answer yet)."""
    got = parse_reply(text).answered()
    return all(s.n in got for s in batch)


# ---- 5. checking and assembling -------------------------------------------------

def reuse_motion(own: str, target: str) -> str:
    """The motion a REUSED shot carries. The picture was made for its
    original shot's canvas, so a shot keeps its own camera move only when that
    canvas is the same size (ST / ZI / ZO share one); otherwise (a pan or tilt
    needs a bigger canvas) it takes the original's move."""
    info = briefs.MOTION_CODE_INFO
    if own in info and target in info and info[own][1] == info[target][1]:
        return own
    return target


def prompt_limit(style: str = "") -> int:
    """Longest image prompt Flow takes with this style in front of it
    (style + one space + prompt must stay within the Flow limit; a longer one
    makes Flow drop the style from the images)."""
    budget = studio.FLOWBATCH_MAX_PROMPT_CHARS - (
        len((style or "").strip()) + 1 if (style or "").strip() else 0)
    return max(200, min(2400, budget))


def problems(shots: list[Draft], ans: Answers, profile, allow_refs: bool,
             supplied_stems: set[str], only: set | None = None,
             style: str = "") -> list[str]:
    """What is still wrong with the answers, one line per problem, naming the
    shot number (so a fix request can be narrow). Empty = ready to assemble."""
    out: list[str] = []
    limit = prompt_limit(style)
    by_n = {s.n: s for s in shots}
    motion_of: dict[int, str] = {}
    for s in shots:
        n = s.n
        if only is not None and n not in only:
            continue
        if n in ans.new:
            motion_of[n] = s.motion
            a = ans.new[n]
            if s.animate and not a["anim"]:
                out.append(f"shot {n}: flagged ANIMATE but has no `|| note`")
            if not s.animate and a["anim"]:
                out.append(f"shot {n}: not flagged ANIMATE - no `||` note")
            if allow_refs:
                for r in a["refs"]:
                    known = (r in supplied_stems or r.upper() in ans.refs)
                    if not known:
                        out.append(f"shot {n}: ref {r} is neither supplied "
                                   f"nor defined with a REF line")
            elif a["refs"]:
                out.append(f"shot {n}: refs are off for this channel")
            if len(a["prompt"]) > limit:
                out.append(f"shot {n}: the prompt is {len(a['prompt'])} "
                           f"characters (limit {limit} with the channel "
                           f"style in front) - shorten it")
        elif n in ans.reuse:
            m = ans.reuse[n]
            if s.animate:
                out.append(f"shot {n}: an ANIMATE shot needs its own image")
            elif m not in by_n or m >= n or m not in ans.new:
                out.append(f"shot {n}: REUSE {m} must point to an earlier "
                           f"shot that has its own image")
            elif by_n[m].animate:
                out.append(f"shot {n}: cannot reuse an ANIMATE shot's image")
        else:
            out.append(f"shot {n}: no line")
    return out


def _ref_key(name: str, supplied: set, ans: "Answers") -> str:
    """The spelling a ref is declared under: a supplied file's name as it is,
    an invented one in CAPS (the REF line's name)."""
    if name in supplied:
        return name
    return name.upper() if name.upper() in ans.refs else name


_FLAT = ("ST", "ZI", "ZO")          # one canvas size: they can swap freely


def _final_motion(shots: list[Draft], ans: Answers, motion_of: dict,
                  profile) -> None:
    """The camera move of every shot, decided once the picture grouping is
    known, so the channel's mix comes out right whatever the writer reused.

    A new image keeps the move it was composed for when that is a pan or a
    tilt (it needs its bigger canvas). Everything else - a new image on the
    ST / ZI / ZO canvas, and every reused shot (which may only take a code on
    its picture's canvas) - takes whichever allowed code is furthest below
    its target share. The animated opening stays on its zooms."""
    targets = _targets(profile)
    total = len(shots)
    info = briefs.MOTION_CODE_INFO
    counts = {c: 0 for c in targets}
    fixed = {}
    for s in shots:
        if s.animate or (s.n in ans.new and s.motion not in _FLAT):
            fixed[s.n] = s.motion
            counts[s.motion] = counts.get(s.motion, 0) + 1

    def st_ok(c: str, s: Draft) -> bool:
        return c != "ST" or st_fits(s.seconds, profile)

    # ST is scarce - it only fits on short holds - so it is planned first:
    # spread evenly over the shots that can take it
    st_plan: set[int] = set()
    if "ST" in targets and len(targets) > 1:
        def flat_pic(s: Draft) -> bool:
            if s.n in fixed:
                return False
            pic = ans.reuse.get(s.n, s.n)
            return shots[pic - 1].motion in _FLAT if pic - 1 < len(shots) \
                and shots[pic - 1].n == pic else True
        able = [s for s in shots if s.n not in fixed and st_ok("ST", s)
                and flat_pic(s)]
        want = min(len(able), math.ceil(targets["ST"] * total * 0.9))
        if want:
            step = len(able) / want
            st_plan = {able[int(i * step + step / 2)].n for i in range(want)}

    def pick(pool: list[str], s: Draft, fallback: str) -> str:
        if s.n in st_plan and "ST" in pool:
            counts["ST"] = counts.get("ST", 0) + 1
            return "ST"
        ok = [c for c in pool if c in targets and st_ok(c, s)
              and (c != "ST" or len(targets) == 1)]
        if not ok:
            return fallback
        best = max(ok, key=lambda c: targets[c] * total - counts.get(c, 0))
        counts[best] = counts.get(best, 0) + 1
        return best

    for s in shots:
        if s.n in fixed:
            motion_of[s.n] = fixed[s.n]
        elif s.n in ans.new:
            motion_of[s.n] = pick(list(_FLAT), s, s.motion)
        else:
            base = motion_of[ans.reuse[s.n]]
            same = [c for c in targets if c in info and base in info
                    and info[c][1] == info[base][1]]
            motion_of[s.n] = pick(same, s, base)


def assemble(shots: list[Draft], ans: Answers, style: str,
             supplied_refs: list[str], allow_refs: bool,
             profile=None, scenes: dict[int, int] | None = None) -> dict:
    """The shotlist JSON structure, built from the draft shots and the LLM's
    answers. Call `problems` first - this assumes they are clean.

    `scenes` maps shot number to 1-based main beat (scene), decided by the
    grouping step's BEAT markers; when it is left out the scenes follow the
    writer's own `BEAT <n>` lines in the picture replies (ungrouped flow)."""
    stems_set = {n.rsplit(".", 1)[0] for n in supplied_refs}
    final_motion: dict[int, str] = {}
    if profile is not None:
        _final_motion(shots, ans, final_motion, profile)
    else:
        for s in shots:
            final_motion[s.n] = s.motion
    if scenes:
        scene_of = {s.n: int(scenes.get(s.n, 1)) for s in shots}
    else:
        beats = sorted(set(ans.beats) | {1})
        scene_of = {}
        for s in shots:
            scene_of[s.n] = sum(1 for b in beats if b <= s.n)
    # a chapter that swallowed the video (S13_01...S13_54) is worse than a
    # mechanical split: cap every scene's sub-beat count, splitting at image
    # boundaries only, then relabel the scenes 1..K in narration order
    scene_of = _cap_scene_size(shots, scene_of, ans)
    renumber = {}
    for s in shots:
        sc = scene_of[s.n]
        if sc not in renumber:
            renumber[sc] = len(renumber) + 1
        scene_of[s.n] = renumber[sc]
    sub: dict[int, int] = {}
    file_of: dict[int, str] = {}
    motion_of: dict[int, str] = {}
    out_shots, images = [], []
    used_refs: list[str] = []
    for s in shots:
        sc = scene_of[s.n]
        if s.n in ans.new:
            a = ans.new[s.n]
            sub[sc] = sub.get(sc, 0) + 1
            mv = final_motion.get(s.n) or s.motion
            fname = f"S{sc:02d}_{sub[sc]:02d}_{a['type']}_{mv}.png"
            file_of[s.n], motion_of[s.n] = fname, mv
            img = {"file": fname, "prompt": a["prompt"]}
            if allow_refs and a["refs"]:
                img["refs"] = list(dict.fromkeys(
                    _ref_key(r, stems_set, ans) for r in a["refs"]))[:10]
                used_refs.extend(img["refs"])
            images.append(img)
        else:
            m = ans.reuse[s.n]
            file_of[s.n] = file_of[m]
            motion_of[s.n] = (final_motion.get(s.n)
                              or reuse_motion(s.motion, motion_of[m]))
        shot = {"cues": s.cues, "asset": file_of[s.n],
                "scene": f"S{sc:02d}", "motion": motion_of[s.n]}
        if s.animate:
            shot["ai_motion"] = ans.new[s.n]["anim"]
        out_shots.append(shot)
    data: dict = {"style": (style or "").strip()}
    if allow_refs and (supplied_refs or used_refs):
        stems = {n.rsplit(".", 1)[0]: n for n in supplied_refs}
        registry = {stem: f"refs/{name}" for stem, name in stems.items()}
        prompts = {}
        for name in dict.fromkeys(used_refs):
            if name not in registry:
                registry[name] = None
                prompts[name] = ans.refs.get(name, "")
        data["refs"] = registry
        if prompts:
            data["refPrompts"] = prompts
    data["shots"] = out_shots
    data["images"] = images
    return data
