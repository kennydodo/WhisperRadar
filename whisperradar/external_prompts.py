"""Copy-paste prompts for running a stage in an EXTERNAL LLM (a ChatGPT or
Claude subscription) instead of WhisperRadar's own providers.

Nothing here calls an LLM. Each builder assembles a prompt from the same
inputs, the same prompt functions and the same channel settings the built-in
stages use, so the external run is held to the bar the app would hold it to:

    script   writer prompt   <- title + the reference's WRITING STYLE + research
                                notes (never the raw reference script) + the
                                channel's target length / direction
             judge prompt    <- the same plus the pasted script, the measured
                                overlap and the channel's pass bar
    shotlist planner prompt  <- the manifest-authoring brief (channel motion
                                profile, hold range, presentation) + narration
                                + visual style + bible + refs policy + pacing
             judge prompt    <- the channel's hard rules in plain text + the
                                prompt-completeness audit + narration + the
                                pasted shotlist

The user pastes the external LLM's script / shotlist back into the Studio's
normal save boxes. The style and bible go only where they are needed: the
writer and judge of the script get the WRITING style, the shotlist prompts get
the visual style and the bible - never both kinds in one prompt.
"""
from __future__ import annotations

import json
import re

from . import autorun, briefs, db, studio


class PromptError(RuntimeError):
    """The prompt cannot be built yet (a missing input); the message says what."""


# ---- shared context ---------------------------------------------------------

def _context(cfg, pid: int) -> dict:
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        prod = db.get_production(conn, pid)
        if not prod:
            raise PromptError("Unknown production")
        pdir = studio.prod_dir(cfg, pid)
        from . import settings
        # same as the built-in shots stage: copy the channel's bible / refs
        # into the production first, so the prompt sees the reference files
        studio.seed_production(cfg, conn, prod)
        prod = db.get_production(conn, pid)
        eff = settings.for_production(conn, prod)
        style_guide, _s, bible, _b = autorun.style_bible(cfg, conn, prod, pdir)
    finally:
        conn.close()
    return {"prod": prod, "pdir": pdir, "eff": eff,
            "visual_style": style_guide or "", "bible": bible or ""}


def _read(path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip() if path else ""
    except OSError:
        return ""


def _writing_style(ctx) -> str:
    return _read(studio.find_writing_style(ctx["pdir"]))


def save_notes(cfg, pid: int, text: str) -> int:
    """Persist pasted research notes as the production's notes (kept as-is by
    the built-in stages too). Returns the word count."""
    text = (text or "").strip()
    if not text:
        raise PromptError("Paste the notes first")
    ctx = _context(cfg, pid)
    autorun.save_manual_notes(ctx["pdir"], text)
    return len(text.split())


def _research_notes(ctx) -> str:
    return _read(ctx["pdir"] / autorun.RESEARCH_NOTES_FILE)


def _title(ctx, title) -> str:
    return (title or "").strip() or ctx["prod"]["title"]


def _bar(value) -> str:
    return f"{float(value):g}"


# ---- script -----------------------------------------------------------------

# the same window autorun._script_gate enforces
LEN_MIN_RATIO = 0.8
LEN_MAX_RATIO = 1.15

_NO_STYLE_ASK = (
    "STYLE GUIDE: none has been provided with this prompt. Do NOT invent "
    "one. Before writing anything, ask me to paste or attach the writing "
    "style guide, and wait for it. Only if I say there is none, write in a "
    "clear, natural spoken voice.")


def _target_words(cfg, pid: int, target_words) -> int:
    if target_words:
        return int(target_words)
    source = autorun._source_transcript_text(cfg, pid)
    return studio.script_target_words(
        cfg.studio_script_words, len(re.findall(r"\w+", source or "")))


def _length_window(target: int) -> tuple[int, int]:
    return int(target * LEN_MIN_RATIO), int(target * LEN_MAX_RATIO)

_SCRIPT_REPLY_FORMAT = (
    "\n\nREPLY FORMAT: the finished script as plain text only - no title "
    "line, no headings, no scene labels, no notes before or after it. If you "
    "reach your length limit, stop at the end of a paragraph; I will reply "
    "\"continue\" and you resume from the next paragraph without repeating "
    "anything.")


def style_extraction_prompt(cfg, pid: int, title: str = "") -> str:
    """For when the production has no writing style guide yet: asks the
    external LLM to analyse the reference script's style. The reference
    transcript IS included here (the analysis needs it) - the style guide that
    comes back is what every later prompt carries instead of the transcript."""
    ctx = _context(cfg, pid)
    source = autorun._source_transcript_text(cfg, pid)
    if not source.strip():
        raise PromptError("No reference script (source transcript) on this "
                          "production - write the style guide by hand")
    words = len(re.findall(r"\w+", source))
    try:
        return studio.style_prompt(
            _title(ctx, title), ctx["prod"]["genre"], source,
            word_count=words,
            extra_direction=db.stage_extra(ctx["prod"], "style"))
    except RuntimeError as exc:
        raise PromptError(str(exc)) from exc


def notes_extraction_prompt(cfg, pid: int, title: str = "") -> str:
    """For when there are no research notes yet: asks the external LLM to turn
    the reference script into neutral notes (facts only, in its own words)."""
    ctx = _context(cfg, pid)
    source = autorun._source_transcript_text(cfg, pid)
    if not source.strip():
        raise PromptError("No reference script (source transcript) on this "
                          "production")
    return studio.notes_prompt(_title(ctx, title), ctx["prod"]["genre"],
                               source)


def script_writer_prompt(cfg, pid: int, title: str = "",
                         target_words: int | None = None,
                         style_guide: str | None = None,
                         notes: str | None = None) -> str:
    ctx = _context(cfg, pid)
    eff, prod = ctx["eff"], ctx["prod"]
    style = (style_guide if style_guide is not None
             else _writing_style(ctx)).strip()
    facts = (notes if notes is not None else _research_notes(ctx)).strip()
    target_words = _target_words(cfg, pid, target_words)
    text = studio.script_prompt(
        _title(ctx, title), prod["genre"], facts, style_guide=style,
        target_words=int(target_words),
        extra_direction=db.stage_extra(prod, "script"))
    lo, hi = _length_window(int(target_words))
    bar = (f"\n\nQUALITY BAR - a script is only accepted when ALL of these "
           f"hold:\n"
           f"- length between {lo} and {hi} words (target {int(target_words)}); "
           f"shorter or longer is rejected\n"
           f"- an overall rating of at least {_bar(eff['script_min_rating'])}"
           f"/10 from a strict editor\n"
           f"- no more than {float(eff['script_max_overlap']):.0%} of its "
           f"5-word runs shared with the source material (above "
           f"{float(eff['script_hard_overlap']):.0%} is rejected outright)\n"
           f"- it ends on a complete sentence, never cut off")
    if not style:
        text = text.replace("No style guide provided.", _NO_STYLE_ASK)
        text = text.replace("Follow the STYLE GUIDE above precisely.",
                            "Follow the STYLE GUIDE precisely once I give "
                            "it to you.")
    return text + bar + _SCRIPT_REPLY_FORMAT


def script_judge_prompt(cfg, pid: int, script: str, title: str = "",
                        style_guide: str | None = None,
                        notes: str | None = None,
                        target_words: int | None = None) -> str:
    script = (script or "").strip()
    if not script:
        raise PromptError("Paste the script to judge first")
    ctx = _context(cfg, pid)
    eff, prod = ctx["eff"], ctx["prod"]
    style = (style_guide if style_guide is not None
             else _writing_style(ctx)).strip()
    source = autorun._source_transcript_text(cfg, pid)
    facts = (notes if notes is not None else _research_notes(ctx)).strip()
    # the built-in judge grades against the writer's notes; with none, the
    # source is the only ground truth there is
    facts = facts or source
    overlap = studio.overlap_ratio(script, source) if source.strip() else 0.0
    text = studio.rating_prompt(
        _title(ctx, title), prod["genre"], script, facts, style, overlap,
        extra_direction=db.stage_extra(prod, "script"))
    target = _target_words(cfg, pid, target_words)
    lo, hi = _length_window(target)
    words = len(re.findall(r"\w+", script))
    cut = studio.script_looks_truncated(script)
    bar = (f"\n\nTHE CHANNEL'S BAR: the script passes only if ALL hold - "
           f"overall score at least {_bar(eff['script_min_rating'])}; "
           f"5-gram overlap at most {float(eff['script_max_overlap']):.0%} "
           f"(above {float(eff['script_hard_overlap']):.0%} is a hard "
           f"rejection however well it reads); length {lo}-{hi} words "
           f"(target {target}); ends on a complete sentence. Measured by "
           f"software, do not re-estimate: overlap above; length {words} "
           f"words; ending "
           f"{'LOOKS CUT OFF' if cut else 'is complete'}.\n"
           f"Score the writing honestly on its own merits - do not raise or "
           f"lower a score to fit the bar. After the scores, state PASS or "
           f"FAIL separately and list which bar items failed.")
    return text + bar


# ---- shotlist ---------------------------------------------------------------

_SHOTLIST_OUTPUT_RULES = """

---

OUTPUT RULES FOR THIS CHAT (you are being used outside the app; these keep the file machine-readable):
1. Reply with the shotlist JSON in ONE fenced ```json code block - valid JSON, double quotes, no comments, no trailing commas, no text inside the block that is not JSON. If you also write the image batch sheet (Section 7), put it AFTER the JSON in a separate ```text block.
2. If you approach your output limit: stop after the last COMPLETE entry (never mid-token or mid-string), and end the message. When I reply "continue", output ONLY the remaining content, picking up at the exact next entry with no repetition, no commentary and no second "style" field, in a new ```json block that continues the same document, and finish every cue through the last one, closing every bracket cleanly. Keep going on each "continue" until the last cue is covered and the JSON is closed.
3. Every cue from 1 to the last must be covered exactly once. A message that ends before the final cue is a hard failure."""


def _plan_inputs(cfg, pid: int, ctx: dict) -> dict:
    srt = studio.find_srt(ctx["pdir"])
    if not srt:
        raise PromptError("No subtitles yet - generate or upload the SRT "
                          "first")
    srt_text = srt.read_text(encoding="utf-8")
    cues = studio.parse_srt_cues(srt_text)
    if not cues:
        raise PromptError("The subtitles file has no cues")
    eff = ctx["eff"]
    profile = briefs.resolve_profile(
        eff.get("brief_motion"), eff.get("brief_min_hold"),
        eff.get("brief_max_hold"),
        default_max=eff["shotlist_max_hold_seconds"],
        custom=eff.get("brief_custom"))
    max_hold = profile.max_hold or eff["shotlist_max_hold_seconds"]
    total_s = studio._srt_seconds(cues[-1]["end"])
    return {"srt_text": srt_text, "cues": cues, "profile": profile,
            "max_hold": max_hold, "total_s": total_s,
            "min_align": eff["shotlist_min_alignment"],
            "narration": studio.compact_srt(srt_text),
            "allow_refs": bool(eff["generate_references"])}


def shotlist_planner_prompt(cfg, pid: int) -> str:
    ctx = _context(cfg, pid)
    eff = ctx["eff"]
    plan = _plan_inputs(cfg, pid, ctx)
    profile = plan["profile"]
    brief = studio.load_manifest_brief(
        cfg, profile, eff.get("brief_presentation") or "")
    pacing = briefs.pacing_note(profile, plan["total_s"], len(plan["cues"]),
                                plan["max_hold"], plan["min_align"])
    bible = ctx["bible"]
    text = studio.shotlist_prompt(
        brief, plan["narration"], ctx["visual_style"],
        extra_direction=db.stage_extra(ctx["prod"], "shots"),
        bible=bible, supplied_refs=studio.find_supplied_refs(ctx["pdir"]),
        pacing_note=pacing, allow_refs=plan["allow_refs"])
    if not bible.strip():
        # the brief's bible gate otherwise makes the LLM ask for one
        text += ("\n\nINPUT 3 - CHARACTER / REFERENCE BIBLE: this channel "
                 "has none. Do not ask for one - plan without it.")
    return text + _SHOTLIST_OUTPUT_RULES


def _hard_rules(plan: dict, eff: dict, ctx: dict, n_cues: int) -> str:
    profile, cap = plan["profile"], plan["max_hold"]
    lines = [
        f"COVERAGE: every cue from 1 to {n_cues} is covered by exactly one "
        f"shot - no gaps, no overlaps, in narration order, and the last shot "
        f"ends at cue {n_cues}.",
        "ASSETS: every shot's \"asset\" has a matching entry in "
        "\"images\" (by \"file\"); no two image prompts are identical.",
        f"MAXIMUM HOLD: no shot holds longer than {cap:g}s. A shot's hold "
        f"is the sum of the (Ns) durations of the cues in its range. A shot "
        f"over {cap:g}s is a hard fault whether or not it has motion.",
    ]
    if profile.min_hold:
        lines.append(
            f"MINIMUM HOLD: this is a slow-paced channel - shots are meant "
            f"to hold {profile.min_hold:g}-{cap:g}s. More than "
            f"{briefs.MIN_HOLD_MAX_SHORT_SHARE:.0%} of the shots (the last "
            f"one is exempt) holding under {profile.min_hold:g}s is a fault.")
    if profile.allowed:
        only = "/".join(profile.allowed)
        lines.append(f"MOTION CODES: only {only} is allowed on this "
                     f"channel; any other motion code is a fault.")
    motion = []
    if profile.static_long_hold is not None:
        motion.append(f"a static (ST) shot is never acceptable at "
                      f"{profile.static_long_hold:g}s or more")
    if profile.st_max_hold is not None:
        motion.append(f"an ST shot may not hold longer than "
                      f"{profile.st_max_hold:g}s")
    if profile.st_max_share is not None:
        motion.append(f"ST may be at most {profile.st_max_share:.0%} of shots")
    if profile.code_max_share is not None:
        caps = ", ".join(f"{c} {v:.0%}" for c, v in
                         sorted(profile.code_share_overrides.items()))
        motion.append(f"no single motion code may exceed "
                      f"{profile.code_max_share:.0%} of shots"
                      + (f" ({caps} are capped lower)" if caps else ""))
    if motion:
        lines.append("MOTION MIX: " + "; ".join(motion) + ".")
    lines.append(
        f"FRAGMENTATION: more than {studio.FRAGMENTATION_SHARE:.0%} of "
        f"the shots spanning a single cue is a fault - images should change "
        f"when the idea changes, not at every cue.")
    if not plan["allow_refs"]:
        lines.append(
            "REFERENCES ARE OFF for this channel: the shotlist must have "
            "no top-level \"refs\" registry, no \"refPrompts\", and no "
            "\"refs\" array on any image. Every image prompt must be fully "
            "self-contained.")
    else:
        supplied = studio.find_supplied_refs(ctx["pdir"])
        lines.append(
            f"REFERENCES: every generated reference name follows "
            f"CH_/BG_/OBJ_ + CAPS/digits (e.g. CH_MAYA, OBJ_COIN_JAR_02) - "
            f"supplied files may keep their own names; at most "
            f"{studio.REFS_ON_THE_FLY_CAP} references are generated on the "
            f"fly; every name an image uses is declared in the refs "
            f"registry; every declared supplied file is attached by at least "
            f"one image; a name with no supplied path and no refPrompts "
            f"entry is a fault."
            + (f" Supplied files that exist: "
               f"{', '.join(supplied)}." if supplied else ""))
    lines.append(
        f"DETAIL: at least {plan['min_align']:.0%} of the shots must "
        f"have an image prompt that is complete for its narration (verdict "
        f"\"ok\" in the audit below).")
    return "\n".join(f"{i}. {line}" for i, line in enumerate(lines, 1))


_ALIGN_REPLY_MARK = "\n\nReply with ONLY a JSON object:"


def _alignment_rules(style_guide: str) -> str:
    """The built-in judge's completeness instructions, minus its reply format
    (this judge replies once for the whole plan, in its own format)."""
    head = studio.alignment_prompt([], {}, style_guide=style_guide)
    cut = head.find(_ALIGN_REPLY_MARK)
    return (head[:cut] if cut != -1 else head).rstrip()


def shotlist_judge_prompt(cfg, pid: int, shotlist_text: str) -> tuple[str, list[str]]:
    """(prompt, local_faults). `local_faults` are the app's own code checks
    run on the pasted shotlist right now - shown to the user, not sent to the
    judge, so the external judge stays independent."""
    shotlist_text = (shotlist_text or "").strip()
    if not shotlist_text:
        raise PromptError("Paste the shotlist JSON to judge first")
    try:
        data, _sheet = studio.parse_shotlist_output(shotlist_text)
    except RuntimeError as exc:
        raise PromptError(f"That is not a usable shotlist: {exc}") from exc
    ctx = _context(cfg, pid)
    eff = ctx["eff"]
    plan = _plan_inputs(cfg, pid, ctx)
    cues, n = plan["cues"], len(plan["cues"])
    local = (studio.shotlist_structural_faults(data, n)
             + studio.shotlist_pacing(data, cues, plan["max_hold"],
                                      plan["profile"])[0])
    rules = _hard_rules(plan, eff, ctx, n)
    audit = _alignment_rules(ctx["visual_style"])
    shot_json = json.dumps(data, ensure_ascii=False, indent=1)
    prompt = (
        "You are an independent reviewer of a video SHOTLIST, before any "
        "image is rendered. You are given the narration (one line per cue: "
        "\"N: text (Ns)\" - N is the cue number, Ns its length in seconds) "
        "and the shotlist JSON. Judge it in two parts.\n\n"
        "PART A - HARD RULES (objective; list every violation you can find "
        "with the shot asset names and cue numbers):\n"
        f"{rules}\n\n"
        "PART B - PROMPT COMPLETENESS, per shot. In the instructions below, "
        "\"the narration at its cue range\" means the lines of the NARRATION "
        "section for that shot's \"cues\", and \"the image prompt\" is the "
        "shot's entry in \"images\" (matched by asset = file):\n"
        f"{audit}\n\n"
        f"NARRATION:\n{plan['narration'].strip()}\n\n"
        f"SHOTLIST JSON:\n{shot_json}\n\n"
        "Reply with ONLY one JSON object, no commentary:\n"
        '{"faults": ["<hard-rule violation, with assets and cue numbers>", '
        '...], "shots": [{"asset": "<asset>", "verdict": "weak"|"missing", '
        '"missing": ["<element the narration requires that the prompt does '
        'not state>", ...], "reason": "<one short line>"}], '
        '"detailed_ratio": <share of ALL shots whose prompt is complete, '
        '0-1>, "pass": <true only if faults is empty and detailed_ratio >= '
        f'{plan["min_align"]:g}>, "fixes": ["<the most useful changes, in '
        'order>", ...]}\n'
        "In \"shots\" list ONLY the weak or missing shots (an ok shot is "
        "simply left out), so the reply stays short.")
    return prompt, local
