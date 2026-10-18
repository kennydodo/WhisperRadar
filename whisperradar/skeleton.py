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


# ---- is it usable? ------------------------------------------------------------

def unsupported_reason(eff: dict, profile) -> str:
    """Why this channel needs the whole-JSON flow ("" = the skeleton works).
    Reveal shots, sound effects and a presentation brief change the shape of
    shots in ways the line format does not carry."""
    if getattr(profile, "reveal", False):
        return "the channel uses reveal shots"
    if getattr(profile, "sfx", ""):
        return "the channel uses sound effects"
    if (eff.get("brief_presentation") or "").strip():
        return "the channel has a presentation brief"
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

    @property
    def cues(self) -> str:
        return str(self.first) if self.first == self.last \
            else f"{self.first}-{self.last}"


def _ends_sentence(text: str) -> bool:
    return (text or "").rstrip().endswith(_SENTENCE_END)


def draft_shots(cues: list[dict], max_hold: float, min_hold: float | None = None,
                anim_last_cue: int = 0, short_every: int = 0,
                short_hold: float = 3.5, short_cap: float = 0.0) -> list[Draft]:
    """Cut the cues into consecutive draft shots.

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
    if len(groups) > 1 and len(groups[-1]) == 1:
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


def build(cues: list[dict], profile, max_hold: float,
          anim_last_cue: int = 0) -> list[Draft]:
    short_every, short_hold, short_cap = 0, 3.5, 0.0
    st_share = dict(briefs.mix_targets(profile)).get(("ST",))
    if st_share and profile.st_max_hold:
        short_every = max(2, int(round(1.0 / st_share)) - 2)
        short_hold = min(3.5, profile.st_max_hold * 0.7)
        short_cap = max(1.0, profile.st_max_hold - 0.1)
    shots = draft_shots(cues, max_hold, profile.min_hold, anim_last_cue,
                        short_every, short_hold, short_cap)
    assign_motion(shots, profile)
    return shots


# ---- 3. prompts ---------------------------------------------------------------

def shot_line(s: Draft) -> str:
    flag = " | ANIMATE" if s.animate else ""
    return (f"{s.n} | cues {s.cues} | {s.seconds:g}s | {s.motion}{flag} | "
            f"{s.text}")


def chunks(shots: list[Draft], size: int = CHUNK_SIZE) -> list[list[Draft]]:
    return [shots[i:i + size] for i in range(0, len(shots), size)]


def _section(name: str) -> str:
    """One `## SECTION ...` block of the brief template, verbatim."""
    text = briefs.TEMPLATE_PATH.read_text(encoding="utf-8")
    m = re.search(r"^## SECTION " + re.escape(name) + r"\b.*?(?=^## |\Z)", text,
                  re.S | re.M)
    return m.group(0).strip() if m else ""


def _ref_subject_rule() -> str:
    text = briefs.TEMPLATE_PATH.read_text(encoding="utf-8")
    m = re.search(r"^\*\*A reference prompt describes ONLY.*?$", text, re.M)
    return m.group(0) if m else ""


_REPLY_FORMAT = """\
HOW TO ANSWER - plain text lines, nothing else (no JSON, no code block, no commentary, no summary). One line per shot, in order, using the shot's own number:

  <n> | <TYPE> | <refs or -> | <image prompt>          a NEW image
  <n> | REUSE <m>                                      the same picture as the earlier shot <m>

- TYPE is one of SCN, CU, INF, CMP, PROC, HYB, OVR. refs are names from the reference list, comma-separated (the first is the dominant subject), or `-` for none.
- A shot flagged ANIMATE gets its movement note at the end of its line: `... | <image prompt> || <ANIMATE note>`; a flagged shot is never a REUSE.
- To start a new MAIN BEAT (hook, problem, mechanism, evidence, solution...) put a line `BEAT <n>` before the line of the shot that begins it. Shot 1 always starts the first beat. Beats are semantic sections, not scene changes - usually a handful per video.
- To invent a reference that is not in the list, add a line `REF <CH_/BG_/OBJ_NAME> | <what it looks like>` (before it is first used).
- Answer EVERY shot of the batch. Do not renumber, merge or skip shots; the cue ranges and the camera motion are fixed by the app."""


def rules_text(profile, allow_refs: bool, supplied_refs: list[str],
               animation_note: str = "", density: str = "",
               limit: int = 2400) -> str:
    kinds = briefs.types_text(profile.types)
    parts = [
        "You are a visual director and image-prompt engineer for a "
        "high-retention YouTube channel. The app has already cut the "
        "narration into numbered SHOTS (cue range, length and camera motion "
        "are fixed). Your only job is the pictures: for each shot, decide "
        "whether the narration needs a NEW image or REUSES an earlier one, "
        "and write the image prompt for each new image.",
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
        "- Prefer several shots per image: most pictures should carry two or "
        "three shots of one developing idea.",
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
        _section("8"),
        _section("11A"),
        _section("11B"),
        _section("11C"),
    ]
    if not allow_refs:
        parts.append(
            "REFERENCES ARE OFF for this channel: write no refs and no REF "
            "lines. With no reference image to carry anything, EVERY image "
            "prompt must be fully self-contained: re-describe recurring "
            "characters and objects in full each time they appear (look, "
            "wardrobe, the exact prop) so they stay consistent.")
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
            "genuinely reuses.\n" + _ref_subject_rule())
    if animation_note:
        parts.append(animation_note.strip())
    parts.append(_REPLY_FORMAT)
    return "\n\n".join(p for p in parts if p)


def first_prompt(rules: str, style: str, bible: str, direction: str,
                 batch: list[Draft], total: int) -> str:
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
    blocks.append(batch_block(batch, total, first=True))
    return "\n\n---\n\n".join(blocks)


def batch_block(batch: list[Draft], total: int, first: bool = False) -> str:
    head = (f"SHOTS {batch[0].n}-{batch[-1].n} of {total} "
            f"(line: number | cue range | length | camera motion | narration):")
    tail = ("\n\nAnswer with one line per shot above, "
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
             profile=None) -> dict:
    """The shotlist JSON structure, built from the draft shots and the LLM's
    answers. Call `problems` first - this assumes they are clean."""
    stems_set = {n.rsplit(".", 1)[0] for n in supplied_refs}
    final_motion: dict[int, str] = {}
    if profile is not None:
        _final_motion(shots, ans, final_motion, profile)
    else:
        for s in shots:
            final_motion[s.n] = s.motion
    beats = sorted(set(ans.beats) | {1})
    scene_of = {}
    for s in shots:
        scene_of[s.n] = sum(1 for b in beats if b <= s.n)
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
