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
import math
import re

from . import autorun, briefs, db, handover, refcatalog, studio
from . import plan as packplan


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
        density = studio.visual_density(conn=conn, pid=pid)
        style_guide, _s, bible, _b = autorun.style_bible(cfg, conn, prod, pdir)
    finally:
        conn.close()
    return {"prod": prod, "pdir": pdir, "eff": eff,
            "visual_style": style_guide or "", "bible": bible or "",
            "density": density}


def _read(path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip() if path else ""
    except OSError:
        return ""


def _writing_style(ctx) -> str:
    return _read(studio.find_writing_style(ctx["pdir"]))


# the inline prompts carry at most this much of the style guide
STYLE_INLINE_MAX = 6000


def style_needs_file(cfg, pid: int) -> bool:
    """True when the production's style guide is longer than an inline
    prompt carries - it then goes to the chat as an attached file, whole."""
    return len(_writing_style(_context(cfg, pid)).strip()) > STYLE_INLINE_MAX


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


# ---- files mode -------------------------------------------------------------
# Instead of pasting big inputs into the prompt, the prompt can name files the
# user attaches to the same chat message. Each builder takes an optional
# `files` list: when given, big inputs are appended to it as
# {"name", "text", "about"} and the prompt carries a short reference plus a
# hard gate - the LLM must stop and ask for any listed file that is not
# attached. (Attach them to the message itself, not to Project knowledge: a
# retrieval index would summarise narration cues or a shotlist under review.)

WRITING_STYLE_FILE = "writing_style.md"
NOTES_FILE = "research_notes.md"
SOURCE_FACTS_FILE = "source_facts.txt"
SCRIPT_FILE = "script.txt"
NARRATION_FILE = "narration.txt"
SHOTLIST_FILE = "shotlist.json"
STORYBOARD_FILE = "storyboard_rows.json"
# 'auto' builds inline and switches to files above this size
AUTO_FILES_CHARS = 30000


def _gate(files: list[dict], extra: list[dict] | None = None) -> str:
    files = list(files) + list(extra or [])
    if not files:
        return ""
    names = ", ".join(f["name"] for f in files)
    listing = "\n".join(f"- {f['name']}: {f['about']}" for f in files)
    return (
        f"REQUIRED FILES - these {len(files)} file(s) must be attached to "
        f"THIS message:\n{listing}\n"
        f"Check this first, before anything else. If any of them is not "
        f"attached, or you cannot read it in full, reply with ONLY "
        f"`Missing: <the file names>` and stop - do not guess, do not write "
        f"from memory, do not start the task. (Required: {names}.)\n\n")


def _add_file(files: list, name: str, text: str, about: str) -> None:
    files.append({"name": name, "text": text.strip() + "\n", "about": about})


# ---- script -----------------------------------------------------------------

_STYLE_TOKEN = "@@STYLE-FILE@@"
_FACTS_TOKEN = "@@FACTS-FILE@@"
_ORIG_TOKEN = "@@ORIGINAL-FILE@@"


def _swap_blocks(text: str, out_files: list, style_ref: str = "",
                 facts_ref: str = "", facts_label: str = "",
                 orig_ref: str = "") -> str:
    """Replace the token-marked style / facts blocks of a studio prompt with
    short references to the attached files."""
    text = re.sub(r"STYLE GUIDE \(match (?:this exactly|its tone).*?\):\n" +
                  re.escape(_STYLE_TOKEN), lambda m: style_ref, text,
                  flags=re.S)
    text = re.sub(r"FACTS gathered from research \(use these, nothing "
                  r"else\):\n" + re.escape(_FACTS_TOKEN),
                  lambda m: facts_ref, text)
    text = re.sub(r"SOURCE FACTS:\n" + re.escape(_FACTS_TOKEN),
                  lambda m: facts_ref, text)
    text = re.sub(r"ORIGINAL \(for tone, style, hook, flow and ending "
                  r"ONLY\):\n" + re.escape(_ORIG_TOKEN),
                  lambda m: orig_ref, text)
    text = re.sub(r"CHANNEL STYLE GUIDE:\n" + re.escape(_STYLE_TOKEN),
                  lambda m: style_ref, text)
    # Lean script: the style guide is a formula (see studio.script_prompt)
    text = re.sub(r"FORMULA \(the structure and rules for this script:.*?\):\n"
                  + re.escape(_STYLE_TOKEN), lambda m: style_ref, text,
                  flags=re.S)
    text = re.sub(r"CHANNEL FORMULA:\n" + re.escape(_STYLE_TOKEN),
                  lambda m: style_ref, text)
    return text


_NO_STYLE_ASK = (
    "STYLE GUIDE: none has been provided with this prompt. Do NOT invent "
    "one. Before writing anything, ask me to paste or attach the writing "
    "style guide, and wait for it. Only if I say there is none, write in a "
    "clear, natural spoken voice.")


def _target_words(cfg, pid: int, target_words) -> int:
    if target_words:
        return int(target_words)
    source = autorun._source_transcript_text(cfg, pid)
    conn = db.connect(cfg.db_path)
    try:
        explicit = studio.script_words_override(db.get_production(conn, pid))
    finally:
        conn.close()
    return studio.script_target_words(
        cfg.studio_script_words, len(re.findall(r"\w+", source or "")),
        explicit=explicit, vertical=studio.production_frame(cfg, pid) == "9:16")


_SCRIPT_REPLY_FORMAT = (
    "\n\nREPLY FORMAT: the finished script as plain text only - no title "
    "line, no headings, no scene labels, no notes before or after it. If you "
    "reach your length limit, stop at the end of a paragraph; I will reply "
    "\"continue\" and you resume from the next paragraph without repeating "
    "anything. The requested opening-style metadata comment is the only allowed "
    "exception to plain narration; put it before the script, never speak it.")


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
            variant=studio.style_variant(cfg, pid=pid),
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
                         notes: str | None = None,
                         files: list | None = None,
                         variation: str = "",
                         full_style: bool = False) -> str:
    ctx = _context(cfg, pid)
    eff, prod = ctx["eff"], ctx["prod"]
    lean = autorun.lean_script(cfg)     # Settings > Lean script
    style = (style_guide if style_guide is not None
             else _writing_style(ctx)).strip()
    # lean: only the title and the formula - no brief, no notes
    facts = "" if lean else (notes if notes is not None
                             else _research_notes(ctx)).strip()
    # a typed Target words number is a requirement; the default is a guide
    required = studio.script_words_override(prod) is not None
    target_words = _target_words(cfg, pid, target_words)
    out_files: list[dict] = []
    if files is not None:
        if style:
            _add_file(out_files, WRITING_STYLE_FILE, style,
                      "the formula for this script - follow it" if lean else
                      "the writing style guide - match it exactly")
    text = studio.script_prompt(
        _title(ctx, title), prod["genre"], facts,
        style_guide=_STYLE_TOKEN if (files is not None and style) else style,
        target_words=int(target_words), variation=variation,
        target_required=required,
        vertical=studio.production_frame(cfg, pid) == "9:16",
        style_variant=studio.style_variant(cfg, pid=pid),
        style_limit=(10**6 if (full_style or lean) else STYLE_INLINE_MAX),
        lean=lean,
        extra_direction=packplan.with_plan(cfg, prod,
                                         handover.instruction(cfg, prod, "script")))
    if required:
        lo, hi = studio.script_length_range(int(target_words))
        length_bar = (f"- between {lo} and {hi} words (the creator asked for "
                      f"{int(target_words)}); a script under {lo} words is "
                      f"rejected\n")
    else:
        length_bar = (f"- around {int(target_words)} words is a flexible guide, "
                      f"not a requirement; assess completeness and pacing, "
                      f"never length alone\n")
    bar = (f"\n\nQUALITY BAR - a script is only accepted when ALL of these "
           f"hold:\n"
           + length_bar +
           f"- an overall rating of at least {_bar(eff['script_min_rating'])}"
           f"/10 from a strict editor\n"
           + ("" if lean else
              f"- no more than {float(eff['script_max_overlap']):.0%} of its "
              f"5-word runs shared with the source material (above "
              f"{float(eff['script_hard_overlap']):.0%} is rejected "
              f"outright)\n")
           + f"- it ends on a complete sentence, never cut off\n"
           f"- the closing passage states one clear takeaway, and the script "
           f"has a concrete hook, delivers the title's promise, and repeats "
           f"nothing")
    if not style:
        text = text.replace("No style guide provided.", _NO_STYLE_ASK)
        text = text.replace("Follow the STYLE GUIDE above precisely.",
                            "Follow the STYLE GUIDE precisely once I give "
                            "it to you.")
    if files is not None:
        text = _swap_blocks(text, out_files,
                            style_ref=f"STYLE GUIDE: in the attached "
                            f"{WRITING_STYLE_FILE} (read all of it).")
        files.extend(out_files)
        text = _gate(out_files) + text
    return text + bar + _SCRIPT_REPLY_FORMAT


def script_judge_prompt(cfg, pid: int, script: str, title: str = "",
                        style_guide: str | None = None,
                        notes: str | None = None,
                        target_words: int | None = None,
                        files: list | None = None,
                        full_style: bool = False) -> str:
    script = (script or "").strip()
    # no script in the box: the prompt asks for the user's own script file
    # instead (it cannot be measured here, and the LLM must ask if missing)
    own_script = not script
    ctx = _context(cfg, pid)
    eff, prod = ctx["eff"], ctx["prod"]
    lean = autorun.lean_script(cfg)     # Settings > Lean script
    guard = handover.for_production(cfg, prod, "script")["guard"]
    style = (style_guide if style_guide is not None
             else _writing_style(ctx)).strip()
    # lean: no original at all - the judge works from the formula
    source = "" if lean else autorun._source_transcript_text(cfg, pid)
    # the writer's BRIEF (topic + keywords); the original script is shown to
    # the judge for tone, style, hook, flow and ending only
    facts = "" if lean else (notes if notes is not None
                             else _research_notes(ctx)).strip()
    overlap = studio.overlap_ratio(script, source) if source.strip() else 0.0
    out_files: list[dict] = []
    if files is not None:
        if source.strip():
            _add_file(out_files, SOURCE_FACTS_FILE, source.strip(),
                      "the ORIGINAL script - compare tone, style, hook, flow "
                      "and ending only; never facts, points or names")
        if style:
            _add_file(out_files, WRITING_STYLE_FILE, style,
                      "the formula the script must follow" if lean else
                      "the writing style guide the script should match")
    text = studio.rating_prompt(
        _title(ctx, title), prod["genre"], script, facts,
        _STYLE_TOKEN if (files is not None and style) else style, overlap,
        extra_direction=packplan.with_plan(cfg, prod,
                                         handover.instruction(cfg, prod, "script")),
        original=(_ORIG_TOKEN if (files is not None and source.strip())
                  else source),
        niche_block=studio.judge_niche_block(cfg, prod["genre"]),
        guard=guard,
        style_limit=(10**6 if (full_style or lean) else STYLE_INLINE_MAX),
        vertical=studio.production_frame(cfg, pid) == "9:16",
        lean=lean,
        judge_direction=handover.instruction(cfg, prod, "script_judge"))
    if files is not None:
        text = _swap_blocks(
            text, out_files,
            style_ref=(f"CHANNEL FORMULA: in the attached "
                       f"{WRITING_STYLE_FILE} (read all of it)." if lean else
                       f"CHANNEL STYLE GUIDE: in the attached "
                       f"{WRITING_STYLE_FILE} (read all of it)."),
            orig_ref=f"ORIGINAL (for tone, style, hook, flow and ending "
                     f"ONLY): in the attached {SOURCE_FACTS_FILE} (read all "
                     f"of it).")
    target = _target_words(cfg, pid, target_words)
    words = len((script or "").split())      # as a word processor counts
    cut = studio.script_looks_truncated(script)
    extra = []
    if own_script:
        text = text.replace("SCRIPT:\n\n", f"SCRIPT: in the attached "
                            f"{SCRIPT_FILE} (read all of it).\n\n")
        text = re.sub(r"Measured 5-gram overlap with the original: "
                      r"[\d.]+%\. ", "Overlap with the source was not "
                      "measured: estimate it yourself by comparing the "
                      "script with the ORIGINAL. ", text)
        extra = [{"name": SCRIPT_FILE, "about": "the script to judge - "
                  "attach your own script file"}]
    st = (studio.structure_copy(script, source) if (script and source.strip())
          else None)
    struct_txt = ("" if not st else
                  f" Story order vs the source: {st['matched']:.0%} of the "
                  f"script's sentences echo a source sentence and "
                  f"{st['order']:.0%} of those follow the source's order"
                  f"{' - TOO CLOSE, fail it' if st['flag'] else ''}.")
    measured = ("" if own_script else
                f" Measured by software, do not re-estimate: "
                + ("" if lean else "overlap above; ")
                + f"length {words} words; ending "
                f"{'LOOKS CUT OFF' if cut else 'is complete'}."
                + struct_txt)
    bar = (f"\n\nTHE CHANNEL'S BAR: the script passes only if ALL hold - "
           f"overall score at least {_bar(eff['script_min_rating'])}; "
           f"5-gram overlap at most {float(eff['script_max_overlap']):.0%} "
           f"(above {float(eff['script_hard_overlap']):.0%} is a hard "
           f"rejection however well it reads); ends on a complete sentence; "
           f"word guide {target} is not enforced and length alone never "
           f"fails or lowers the score; it is spoken "
           f"narration only - it never mentions or alludes to the "
           f"thumbnail, cover, video title or packaging.{measured}\n"
           f"Judge it BEAT BY BEAT against the ORIGINAL, for tone, style, hook, "
           f"flow and ending ONLY: the script must match them in quality and "
           f"feel without copying. A script that follows the original's "
           f"sequence of beats, or reuses its names or scenario, fails "
           f"'originality' - tell the writer only the KIND of change (a "
           f"different opening, a different order of reveals, a new ending), "
           f"never what the original does. Never judge or require the "
           f"original's facts or points.\n"
           f"If the script mentions any of those, FAIL it, quote each "
           f"sentence in weak_spans and write in feedback: remove it.\n"
           + (f"Your feedback and weak_spans are forwarded to the writer, who has "
              f"never seen the original. Put NOTHING from the original in them: "
              f"no names, companies, places, figures, events or quotes. Describe "
              f"the problem and the kind of change needed; if names or "
              f"scenario are reused, say 'invent your own', never what to use.\n"
              if guard else
              f"Your feedback and weak_spans are forwarded to the writer, who "
              f"has never seen the original. You may refer to the original to "
              f"explain what to change.\n") +
           f"A script with anything in \"must_fix\" (or a claim marked "
           f"\"wrong\") FAILS whatever its score: never state PASS while "
           f"asking for a necessary factual correction, and never claim "
           f"verification you did not do - mark such claims \"unverified\".\n"
           f"Score the writing honestly on its own merits - do not raise or "
           f"lower a score to fit the bar. After the scores, state PASS or "
           f"FAIL separately and list which bar items failed.")
    if lean:
        bar = (f"\n\nTHE CHANNEL'S BAR: the script passes only if ALL hold - "
               f"overall score at least {_bar(eff['script_min_rating'])}; "
               f"it follows the FORMULA and the channel's script rules; ends "
               f"on a complete sentence; word guide {target} is not enforced "
               f"and length alone never fails or lowers the score; it is "
               f"spoken narration only - it never mentions or alludes to the "
               f"thumbnail, cover, video title or packaging.{measured}\n"
               f"Judge it BEAT BY BEAT against the FORMULA (there is no "
               f"original script): its beats in order, its roles, voice, "
               f"tests and ending framework, its checklist and its "
               f"never-list. The FORMULA gives the pattern, never the "
               f"content: a name, line, object or figure copied from its "
               f"examples fails originality.\n"
               f"If the script mentions the thumbnail, cover, title or "
               f"packaging, FAIL it, quote each sentence in weak_spans and "
               f"write in feedback: remove it.\n"
               f"Your feedback and weak_spans are forwarded to the writer, "
               f"who has only the FORMULA. Describe the problem and the kind "
               f"of change needed in your own words; never quote the "
               f"FORMULA's examples back, and for names or figures say "
               f"'invent your own'.\n"
               f"A script with anything in \"must_fix\" (or a claim marked "
               f"\"wrong\") FAILS whatever its score: never state PASS while "
               f"asking for a necessary factual correction, and never claim "
               f"verification you did not do - mark such claims "
               f"\"unverified\".\n"
               f"Score the writing honestly on its own merits - do not raise "
               f"or lower a score to fit the bar. After the scores, state "
               f"PASS or FAIL separately and list which bar items failed.")
    if files is not None:
        files.extend(out_files)
    if files is not None or own_script:
        text = _gate(out_files, extra) + text
    return text + bar


# ---- shotlist ---------------------------------------------------------------

_SHOTLIST_OUTPUT_RULES = """

---

OUTPUT RULES FOR THIS CHAT (you are being used outside the app; these keep the file machine-readable):
1. Reply with the shotlist JSON in ONE fenced ```json code block - valid JSON, double quotes, no comments, no trailing commas, no text inside the block that is not JSON. Write nothing after the JSON - no image batch sheet, no text copy of the prompts, no summary.
2. If you approach your output limit: stop after the last COMPLETE entry (never mid-token or mid-string), and end the message. When I reply "continue", output ONLY the remaining content, picking up at the exact next entry with no repetition, no commentary and no second "style" field, in a new ```json block that continues the same document, and finish every cue through the last one, closing every bracket cleanly. Keep going on each "continue" until the last cue is covered and the JSON is closed.
3. Every cue from 1 to the last must be covered exactly once. A message that ends before the final cue is a hard failure.
4. Put the JSON in the chat message itself, as text. Do NOT create a file, canvas, artifact, document or download link for it, and never answer with only a file name or a link: the reply is read as text and anything in a file cannot be read."""


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
        custom=eff.get("brief_custom"), types=eff.get("brief_types"),
        reveal=eff.get("brief_reveal"), sfx=eff.get("brief_sfx"),
        text_overlays=eff.get("brief_text_overlays"),
        vertical=eff.get("frame") == "9:16")
    max_hold = profile.max_hold or eff["shotlist_max_hold_seconds"]
    total_s = studio._srt_seconds(cues[-1]["end"])
    return {"srt_text": srt_text, "cues": cues, "profile": profile,
            "max_hold": max_hold, "total_s": total_s,
            "min_align": eff["shotlist_min_alignment"],
            "narration": studio.compact_srt(srt_text),
            "allow_refs": bool(eff["generate_references"])}


def _animation_note(eff: dict, cues: list) -> str:
    """The animated-opening rules for the writer, or "" (channel has it off)."""
    if not eff.get("animate_enabled"):
        return ""
    from . import motion
    seconds = eff["animate_seconds"]
    return motion.writer_note(
        seconds, motion.window_last_cue(cues, seconds, studio._srt_seconds))


def planning_context_prompt(cfg, ctx: dict, plan: dict, narration: str,
                            animation_note: str = "") -> str:
    """The common channel brief and inputs, shared by shotlists and beats."""
    eff, profile = ctx["eff"], plan["profile"]
    brief = studio.load_manifest_brief(
        cfg, profile, eff.get("brief_presentation") or "")
    pacing = briefs.pacing_note(profile, plan["total_s"], len(plan["cues"]),
                               plan["max_hold"], plan["min_align"])
    if getattr(cfg, "studio_prompt_fidelity", False):
        from . import fidelity
        brief += fidelity.brief_block()
    return studio.shotlist_prompt(
        brief, narration, ctx["visual_style"],
        extra_direction=db.stage_extra(ctx["prod"], "shots"),
        bible=ctx["bible"], supplied_refs=studio.find_supplied_refs(ctx["pdir"]),
        ref_notes=refcatalog.notes(ctx["pdir"], studio.find_supplied_refs(ctx["pdir"])),
        pacing_note=pacing, allow_refs=plan["allow_refs"],
        animation_note=animation_note, density=ctx["density"])


def storyboard_context(cfg, ctx: dict, plan: dict) -> str:
    """The channel rules for the STORYBOARD writer: the planning brief cut down to the
    sections about what to show (briefs.STORYBOARD_SECTIONS), plus the creator's
    direction. No shotlist format, references registry, filenames or cue numbers."""
    eff, profile = ctx["eff"], plan["profile"]
    brief = studio.load_manifest_brief(
        cfg, profile, eff.get("brief_presentation") or "")
    out = briefs.storyboard_brief(brief)
    pacing = storyboard_pacing(profile, plan["total_s"], plan["max_hold"])
    if pacing:
        out += "\n\n" + pacing
    extra = (db.stage_extra(ctx["prod"], "shots") or "").strip()
    if extra:
        out += f"\n\nCREATOR DIRECTION (follow it):\n{extra}"
    return out


def storyboard_pacing(profile, total_s: float, max_hold: float) -> str:
    """How many rows the script needs, from its length and the channel's holds. Without
    it a writer under-plans (a few long rows); the app rejects rows over the limit."""
    if not (max_hold and max_hold > 0 and total_s and total_s > 0):
        return ""
    floor = int(total_s // max_hold) + (1 if total_s % max_hold else 0)
    min_hold = getattr(profile, "min_hold", None) or 0
    head = (f"PACING FOR THIS CHANNEL: the script runs about {total_s:.0f} seconds. A row may "
            f"hold at most {max_hold:g} seconds (the app rejects a longer row), so you need "
            f"at least {floor} rows. ")
    if min_hold:
        hi = max(floor, int(math.ceil(total_s / min_hold)))
        return (head + f"This is a slow-paced channel: rows are meant to hold {min_hold:g}-"
                f"{max_hold:g} seconds, so expect about {floor} to {hi} rows. Join the sentences "
                "of one developing idea into ONE row and split only where the idea, the subject "
                "or the example changes; do not cut at every new detail. Most rows must hold "
                f"at least {min_hold:g} seconds.")
    return (head + f"{floor} is only the floor (it assumes every row runs the full "
            f"{max_hold:g} seconds); most sentences are shorter, so a plan that follows the "
            f"meaning lands well above {floor}. Start a new row where the idea, the subject, "
            "the example, the place or the action changes. Do not stretch one row over several "
            "ideas to save rows: too few rows is rejected.")


def shotlist_planner_prompt(cfg, pid: int, files: list | None = None) -> str:
    ctx = _context(cfg, pid)
    eff = ctx["eff"]
    plan = _plan_inputs(cfg, pid, ctx)
    bible = ctx["bible"]
    # The brief is already chosen for this channel, so it goes in the prompt
    # itself and nothing about it has to be attached. Only the narration (the
    # long, per-video part) becomes a file in files mode.
    out_files: list[dict] = []
    narration = plan["narration"]
    if files is not None:
        _add_file(out_files, NARRATION_FILE, narration,
                  "INPUT 1 of the brief: the narration, one line per cue: "
                  "\"N: text (Ns)\" - read all of it")
        narration = (f"in the attached {NARRATION_FILE} (read all of it; "
                     f"one line per cue).")
    text = planning_context_prompt(cfg, ctx, plan, narration,
                                   _animation_note(eff, plan["cues"]))
    if not bible.strip():
        # the brief's bible gate otherwise makes the LLM ask for one
        text += ("\n\nINPUT 3 - CHARACTER / REFERENCE BIBLE: this channel "
                 "has none. Do not ask for one - plan without it.")
    # the elements step (optional): recurring characters / places / objects,
    # each with one fixed look
    from . import elements
    text += elements.planner_block(elements.load_text(ctx["pdir"]))
    if files is not None:
        files.extend(out_files)
        text = _gate(out_files) + text
    return text + _SHOTLIST_OUTPUT_RULES


def _hard_rules(plan: dict, eff: dict, ctx: dict, n_cues: int) -> str:
    profile, cap = plan["profile"], plan["max_hold"]
    lines = [
        f"COVERAGE: every cue from 1 to {n_cues} is covered by exactly one "
        f"shot - no gaps, no overlaps, in narration order, and the last shot "
        f"ends at cue {n_cues}. CHECK THIS FIRST: read the \"cues\" of the "
        f"LAST shot - if it does not end at cue {n_cues}, the plan stopped "
        f"early; report it as a fault and list every cue number after the "
        f"last covered one.",
        "ASSETS: every shot's \"asset\" has a matching entry in "
        "\"images\" (by \"file\"); no two image prompts are identical.",
        f"MAXIMUM HOLD: no shot holds longer than {cap:g}s. A shot's hold "
        f"is the time from the start of its first cue to the end of its last "
        f"cue. A shot over {briefs.hold_limit(cap, profile.max_hold_tol):.1f}s "
        f"(the {cap:g}s limit plus the accepted tolerance) is a hard fault "
        f"whether or not it has motion. Holds up to that are NOT faults: do "
        f"not report them.",
    ]
    if profile.per_cue:       # a cue cannot be split, so a long cue is not a fault here
        lines = [ln for ln in lines if not ln.startswith("MAXIMUM HOLD")]
    if plan.get("allow_refs", True):
        lines.append(
            "RECURRING SUBJECTS: any character, location or object that "
            "appears in 3 or more shots must have a ref in the \"refs\" "
            "registry, attached to every shot that shows it. A subject in 3 "
            "or more shots without a ref is a fault: name the subject and "
            "its shots. A subject in only 1 or 2 shots needs no ref.")
    if profile.text_overlays:
        lines.append(
            "TEXT: this channel's renderer draws the text. An image prompt that "
            "asks for readable text (words, numbers, labels, captions) is a "
            "fault - it belongs in the shot's \"overlays\" (text, style, "
            "position, optional start/end seconds from the shot start, anim). "
            "An overlay must have a position the image prompt keeps empty.")
    if profile.min_hold:
        lines.append(
            f"MINIMUM HOLD: this is a slow-paced channel - shots are meant "
            f"to hold {profile.min_hold:g}-{cap:g}s. More than "
            f"{briefs.pacing_share_cap(profile, 'short_hold') * 100:g}% of the shots (the last "
            f"one is exempt) holding under {profile.min_hold:g}s is a fault. "
            f"This includes {profile.short_hold_tolerance * 100:g} percentage points tolerance "
            f"above the configured {profile.short_hold_share * 100:g}% allowance; up to that is not a fault.")
    if any(briefs._allows(profile, c) for c in ("PU", "PD", "PL", "PR")):
        lines.append(
            "PAN LANDING: every PU/PD/PL/PR shot should carry a numeric \"land\" (0-100: how far "
            "down - PU/PD - or across - PL/PR - the picture its main subject sits). A PU or PL with "
            "land above 45, a PD or PR with land below 55, or a pan or tilt without land is a "
            "fault: say which shots.")
    if profile.allowed:
        only = "/".join(profile.allowed)
        lines.append(f"MOTION CODES: only {only} is allowed on this "
                     f"channel; any other motion code is a fault.")
    motion = []
    if profile.static_long_hold is not None:
        floor = briefs.hold_limit(profile.static_long_hold,
                                  profile.static_hold_tol)
        motion.append(f"a static (ST) shot is never acceptable at "
                      f"{floor:.1f}s or more ({profile.static_long_hold:g}s "
                      f"plus the accepted tolerance; shorter is NOT a fault)")
    if profile.st_max_hold is not None:
        top = briefs.hold_limit(profile.st_max_hold, profile.st_hold_tol)
        motion.append(f"an ST shot may not hold longer than {top:.1f}s "
                      f"({profile.st_max_hold:g}s plus the accepted "
                      f"tolerance; up to that is NOT a fault)")
    if profile.st_max_share is not None and not any(codes == ("ST",) for codes, _ in profile.share_targets):
        motion.append(f"ST may be at most {profile.st_max_share:.0%} of shots")
    if profile.code_max_share is not None:
        caps = ", ".join(f"{c} {v:.0%}" for c, v in
                         sorted(profile.code_share_overrides.items()))
        if profile.code_max_share < 1.0:
            motion.append(f"no single motion code may exceed "
                          f"{profile.code_max_share:.0%} of shots"
                          + (f" ({caps} are capped lower)" if caps else ""))
        elif caps:
            motion.append(f"motion code share caps: {caps}")
    for codes, share_cap in getattr(profile, "group_caps", ()):
        if any(codes == target_codes for target_codes, _ in profile.share_targets):
            continue
        motion.append(f"{'/'.join(codes)} together may be at most "
                      f"{share_cap:.0%} of shots")
    if motion:
        lines.append("MOTION MIX: " + "; ".join(motion) + ". Every share "
                     f"limit here has a tolerance of {profile.share_tolerance * 100:g}"
                     f" percentage points: a share up to "
                     f"{profile.share_tolerance * 100:g} percentage points over its cap is NOT a "
                     f"fault. Do not report "
                     f"it.")
    mix = briefs.mix_text(profile)
    if mix:
        lines.append(mix)
    kinds = briefs.types_text(profile.types)
    if kinds:
        lines.append(kinds)
    if profile.per_cue:
        lines.append(
            "EVERY CUE: this channel gives every cue its own shot and its own "
            "new image. Exactly one shot per cue, every \"cues\" a single "
            "number, no image used twice. Shots spanning several cues, a reused "
            "image, or a missing cue is a fault; one image per cue is NOT "
            "fragmentation here.")
    elif profile.single_cue_share is not None:
        lines.append(
            f"FRAGMENTATION: more than {briefs.pacing_share_cap(profile, 'single_cue') * 100:g}% of "
            f"the shots spanning a single cue is a fault - images should change "
            f"when the idea changes, not at every cue. This includes "
            f"{profile.single_cue_tolerance * 100:g} percentage points tolerance above "
            f"the configured {profile.single_cue_share * 100:g}% cap; up to that is not a fault.")
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
            f"one image, and a character's ref is attached only to images that "
            f"show that character; a name with no supplied path and no refPrompts "
            f"entry is a fault."
            + (f" Supplied files that exist: "
               f"{', '.join(supplied)}." if supplied else ""))
    lines.append(
        f"DETAIL: every shot must have an image prompt that is complete for "
        f"its narration (verdict \"ok\" in the audit below); any weak or "
        f"missing shot is sent back to be fixed.")
    return "\n".join(f"{i}. {line}" for i, line in enumerate(lines, 1))


_ALIGN_REPLY_MARK = "\n\nReply with ONLY a JSON object:"


def _alignment_rules(style_guide: str) -> str:
    """The built-in judge's completeness instructions, minus its reply format
    (this judge replies once for the whole plan, in its own format)."""
    head = studio.alignment_prompt([], {}, style_guide=style_guide)
    cut = head.find(_ALIGN_REPLY_MARK)
    return (head[:cut] if cut != -1 else head).rstrip()


_REVEAL_HARD_RULES = (
    "REVEAL SHOTS AND SOUND EFFECTS (only for shots that carry \"reveal\" or "
    "\"sfx\"; a reveal shot is exempt from the static-hold, ST-share and "
    "motion-code-share limits above but still bound by the maximum hold): "
    "\"reveal\" is 2-4 cue numbers (a list, or {\"cues\": [...]}), strictly "
    "increasing, the first equal to the shot's first cue, all inside the "
    "shot's own cues; the shot is static (motion ST and the file name ends "
    "_ST); an optional \"layout\" is \"row\" or \"grid\" and a grid has "
    "exactly 4 items; an optional \"assets\" list has one different image "
    "file per cue, the first equal to the shot's asset, every one present in "
    "\"images\" and ending _ST. \"sfx\" is a sound name (letters, digits, "
    "space, _ . -, at most 40 characters) or a list of names; a shot without "
    "a reveal takes ONE name; a reveal list has at most one name per item.")

_REVEAL_RAW_JSON_RULES = (
    "\nIn this JSON a reveal shot is a shot with a \"reveal\" field: its "
    "items are named at the listed cues (item k at the k-th cue; with "
    "\"layout\": \"grid\" the four items are the quadrants top-left, "
    "top-right, bottom-left, bottom-right). If the reveal has \"assets\", "
    "each item is its own picture and the pictures stay on screen side by side "
    "(a row of tall strips, or a 2x2 grid), each centre-cropped to its slot: "
    "judge each listed image's prompt (its entry in \"images\") against the "
    "narration from ITS cue up to the next reveal cue (the last one up to the "
    "shot's last cue), NOT the whole shot, and require ONE clear subject "
    "centered with generous empty space on both sides - a prompt that spreads "
    "subjects across the width or pushes the subject to an edge is weak "
    "(\"missing\": \"one centered subject with empty side margins\"). Report "
    "such a weak image under its own asset name.\n")


def storyboard_judge_prompt(cfg, pid: int,
                            files: list | None = None) -> tuple[str, list[str]]:
    """(prompt, local_faults) for an external judge of the SAVED storyboard (rows, before
    any image prompt exists). `local_faults` are the app's own code checks run on those
    rows right now: shown to the user, not sent to the judge."""
    from . import storyboard as sbmod
    from . import beats as beatsmod
    ctx = _context(cfg, pid)
    plan = _plan_inputs(cfg, pid, ctx)
    sb = sbmod.load(ctx["pdir"])
    if not sb or not sb.get("rows"):
        raise PromptError("There is no storyboard yet - write or import it first")
    rows, cues, profile = sb["rows"], plan["cues"], plan["profile"]
    cap = plan["max_hold"]
    allowed = list(profile.allowed or briefs.MOTION_CODES)
    _st, ranges, faults, warns = sbmod.check_rows(
        rows, cues, allowed, cap, profile.max_hold_tol, profile=profile,
        allow_reveal=profile.reveal)
    local = list(faults) + [f"warning: {w}" for w in warns]
    ids = sbmod.row_ids(rows)
    by = {int(c["index"]): c.get("text", "") for c in cues}
    items = []
    for r, i, (a, z) in zip(rows, ids, ranges):
        items.append({
            "id": i, "cues": (f"{a}" if a == z else f"{a}-{z}") if a else "",
            "seconds": round(beatsmod.hold(cues, a, z), 1) if a else 0,
            "narration": " ".join(by.get(c, "") for c in range(a, z + 1)).strip() if a else "",
            "type": r.get("type"), "motion": r.get("motion"), "who": r.get("who") or [],
            "where": r.get("where") or "", "objects": r.get("objects") or [],
            "status": r.get("status") or "", "action": r.get("action") or ""})
    maps = {"characters": {k: v for k, v in (sb.get("characters") or {}).items() if v},
            "places": {k: v for k, v in (sb.get("locations") or {}).items() if v},
            "objects": {k: v for k, v in (sb.get("objects") or {}).items() if v}}
    rows_json = json.dumps({"rows": items, "reference_maps": maps},
                           ensure_ascii=False, indent=1)
    top = briefs.hold_limit(cap, profile.max_hold_tol)
    rules = [
        f"COVERAGE: the rows follow the narration in order with no gaps; every row has an "
        f"action, a status, who and where.",
        f"MAXIMUM HOLD: no row lasts longer than {cap:g}s (up to {top:.1f}s with tolerance is "
        f"not a fault). Each row's \"seconds\" is given: trust it, do not recompute.",
        f"MOTION CODES: only {', '.join(allowed)} are allowed.",
    ]
    if profile.min_hold:
        rules.append(
            f"MINIMUM HOLD: slow-paced channel, rows are meant to hold {profile.min_hold:g}-"
            f"{cap:g}s; more than "
            f"{briefs.pacing_share_cap(profile, 'short_hold') * 100:g}% of the rows (the last is "
            f"exempt) under {profile.min_hold:g}s is a fault.")
    if getattr(profile, "single_cue_share", None) is not None and not profile.per_cue:
        rules.append(
            f"FRAGMENTATION: at most {briefs.pacing_share_cap(profile, 'single_cue') * 100:g}% "
            f"of the rows may cover a single subtitle line.")
    pacing = storyboard_pacing(profile, plan["total_s"], cap)
    rules.append(pacing) if pacing else None
    context = storyboard_context(cfg, ctx, plan)
    from . import elements
    el = elements.load_text(ctx["pdir"]).strip()
    inputs = ["CHANNEL RULES (what the images should show):\n" + context]
    if ctx["visual_style"].strip():
        inputs.append("CHANNEL VISUAL STYLE:\n" + ctx["visual_style"].strip())
    inputs.append("CHARACTER / REFERENCE BIBLE:\n" + (ctx["bible"].strip() or
                  "(none: the channel has no bible)"))
    if el:
        inputs.append("ELEMENTS (recurring characters, places and objects):\n" + el)
    out_files: list[dict] = []
    if files is not None:
        _add_file(out_files, STORYBOARD_FILE, rows_json,
                  "the storyboard under review: every row with its narration, who, where, "
                  "objects, status and action, plus the reference maps")
        files.extend(out_files)
        rows_block = (f"STORYBOARD: in the attached {STORYBOARD_FILE} (read all of it).")
    else:
        rows_block = "STORYBOARD (rows in order, each with the narration it covers):\n" + rows_json
    prompt = _gate(out_files) + (
        "You are an independent reviewer of a video STORYBOARD, before any image prompt is "
        "written. Each row is ONE image that stays on screen while its narration is spoken. "
        "You get the channel rules and the storyboard. Judge it in two parts.\n\n"
        "PART A - HARD RULES (objective; list every violation with the row ids):\n- "
        + "\n- ".join(rules) + "\n\n"
        "PART B - EACH ROW AGAINST ITS NARRATION (a row's \"narration\" is what is spoken "
        "while it is on screen). For every row check:\n"
        "- the action is a believable, VISIBLE everyday action that shows what the narration "
        "says, in the place it names; no invented metaphor, symbol or stand-in prop; nothing "
        "the narration does not state (no invented events, meetings or participants); no "
        "thoughts, only what can be seen;\n"
        "- the status is right: `actual` = it happens in the story, `hypothetical` = what "
        "WOULD or COULD happen, `planned` = decided, not yet done, `remembered` = an earlier "
        "event that did happen, `imagined` = a character pictures it;\n"
        "- who has exactly the people who are SEEN in the image (not someone only mentioned or "
        "on the phone), by names from the bible or elements; where is the place the narration "
        "names;\n"
        "- objects lists the recurring objects clearly visible; an object, character or place "
        "that appears in 3 or more rows must be listed there and have a reference in "
        "reference_maps, so name every one that is missing or inconsistent (two spellings "
        "of one thing);\n"
        "- neighbouring rows vary what happens and where (no repeated action or a long run "
        "of people sitting or standing), and no row follows the channel rules badly "
        "(presenter, text on screen, content policy).\n"
        "Rows with a problem only; an acceptable row is simply left out.\n\n"
        + "\n\n".join(inputs) + "\n\n" + rows_block + "\n\n"
        "Reply with ONLY one JSON object, no commentary:\n"
        '{"faults": ["<hard-rule violation, with row ids>", ...], "rows": [{"id": "S01_04", '
        '"verdict": "wrong"|"weak", "problem": "<one short line: what the narration says vs '
        'what the row shows>", "fix": {"who": [...], "where": "...", "objects": [...], '
        '"status": "...", "action": "<replacement action, one sentence>"}}], "pass": <true only '
        'if faults and rows are both empty>, "fixes": ["<the most useful changes, in order>", '
        '...]}\n'
        "In \"fix\" give only the fields that must change. In \"faults\" list only real "
        "violations you are certain of: never list what you checked and found fine, never ask "
        "questions or show your verification trail - the app re-measures seconds and coverage "
        "and discards faults its own numbers contradict.")
    return prompt, local


def shotlist_judge_prompt(cfg, pid: int, shotlist_text: str,
                          files: list | None = None) -> tuple[str, list[str]]:
    """(prompt, local_faults). `local_faults` are the app's own code checks
    run on the pasted shotlist right now - shown to the user, not sent to the
    judge, so the external judge stays independent."""
    shotlist_text = (shotlist_text or "").strip()
    # nothing in the box: the prompt asks for the user's own shotlist file
    own_shotlist = not shotlist_text
    data = None
    if not own_shotlist:
        try:
            data, _sheet = studio.parse_shotlist_output(shotlist_text)
        except RuntimeError as exc:
            raise PromptError(f"That is not a usable shotlist: {exc}") from exc
    ctx = _context(cfg, pid)
    eff = ctx["eff"]
    plan = _plan_inputs(cfg, pid, ctx)
    cues, n = plan["cues"], len(plan["cues"])
    local = (None if own_shotlist else
             studio.shotlist_structural_faults(data, n)
             + studio.shotlist_pacing(data, cues, plan["max_hold"],
                                      plan["profile"])[0])
    if local is not None and getattr(cfg, "studio_prompt_fidelity", False):
        from . import fidelity
        local = local + fidelity.faults(data, cues)
    rules = _hard_rules(plan, eff, ctx, n)
    audit = (_alignment_rules(ctx["visual_style"])
             + studio.density_audit_block(ctx["density"]))
    # animated opening (channel setting): the app's own free check needs an
    # "ai_motion" line on every shot in the window; the judge also grades how
    # well each of those stills will animate
    if eff.get("animate_enabled"):
        from . import motion
        seconds = eff["animate_seconds"]
        last = motion.window_last_cue(cues, seconds, studio._srt_seconds)
        rules += (f"\nANIMATED OPENING: every shot that starts in cues 1-{last} "
                  f"(about the first {seconds} s) must have a non-empty "
                  f"\"ai_motion\" field; shots after that must not.")
        audit += motion.JUDGE_RULES + (
            "\nIn this JSON the ANIMATED SHOTs are the shots that carry an "
            "\"ai_motion\" field; that field is their planned motion.")
        if data is not None:
            a_faults, _warn, _assets = motion.window_check(
                data, cues, seconds, studio._srt_seconds)
            local = (local or []) + a_faults
    # reveal shots / sound effects: the built-in judge adds these rules per
    # chunk only when a chunk holds such a shot, and it splits a build-up into
    # one judged image per item. The external judge sees the raw JSON, so it
    # gets the same rules in plain text whenever the plan (or, for an attached
    # file, the channel setting) can contain them.
    shots_in = [x for x in ((data or {}).get("shots") or [])
                if isinstance(x, dict)]
    has_reveal = any(studio.shot_reveal(x) for x in shots_in)
    has_sfx = any(x.get("sfx") not in (None, "") for x in shots_in)
    if has_reveal or has_sfx or (own_shotlist and plan["profile"].reveal):
        rules += "\n" + _REVEAL_HARD_RULES
        audit += (studio.REVEAL_JUDGE_RULES + _REVEAL_RAW_JSON_RULES)
    shot_json = ("" if own_shotlist else
                 json.dumps(data, ensure_ascii=False, indent=1))
    extra = ([{"name": SHOTLIST_FILE, "about": "the shotlist JSON under "
               "review - attach your own shotlist file"}]
             if own_shotlist else [])
    out_files = []
    if files is not None:
        _add_file(out_files, NARRATION_FILE, plan["narration"],
                  "the narration, one line per cue: \"N: text (Ns)\"")
        if not own_shotlist:
            _add_file(out_files, SHOTLIST_FILE, shot_json,
                      "the shotlist JSON under review")
        files.extend(out_files)
        narr_block = (f"NARRATION: in the attached {NARRATION_FILE} (read "
                      f"all of it).")
    else:
        narr_block = f"NARRATION:\n{plan['narration'].strip()}"
    if own_shotlist or files is not None:
        json_block = (f"SHOTLIST JSON: in the attached {SHOTLIST_FILE} "
                      f"(read all of it).")
    else:
        json_block = f"SHOTLIST JSON:\n{shot_json}"
    prompt = _gate(out_files, extra) + (
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
        f"{narr_block}\n\n{json_block}\n\n"
        "Reply with ONLY one JSON object, no commentary:\n"
        '{"faults": ["<hard-rule violation, with assets and cue numbers>", '
        '...], "shots": [{"asset": "<asset>", "verdict": "weak"|"missing", '
        '"missing": ["<element the narration requires that the prompt does '
        'not state>", ...], "reason": "<one short line>"}], '
        '"detailed_ratio": <share of ALL shots whose prompt is complete, '
        '0-1>, "pass": <true only if faults is empty and "shots" is empty>, "fixes": ["<the most useful changes, in '
        'order>", ...]}\n'
        "In \"shots\" list ONLY the weak or missing shots (an ok shot is "
        "simply left out), so the reply stays short. In \"faults\" list only "
        "real hard-rule violations you are certain of: never list something "
        "you checked and found to be fine, never ask questions or show your "
        "verification trail - the app re-measures hold and coverage claims "
        "and discards faults its own numbers contradict.")
    return prompt, local
