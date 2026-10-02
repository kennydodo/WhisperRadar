"""Studio auto-run: execute the remaining pipeline stages unattended.

The per-stage runners here are the single implementation used by both the
manual HTTP routes (thin wrappers in webapp.py) and the auto-run pipeline
runner. Each runner either returns quietly (stage done) or raises:

- _Paused: the stage needs manual input before the pipeline can continue
  (e.g. upload audio, write a style guide) - the auto-run stops and the
  button becomes "Resume".
- any other exception: a failure that stops the auto-run with the normal
  job error banner.

The pipeline always stops before the review stage - publishing stays a
human decision.
"""

import json
import logging
import os
import re
import shutil
import time
from collections import Counter

from pathlib import Path

from . import ai33, briefs, db, services, settings, studio, transcribe
from .cli import format_duration


class _Paused(RuntimeError):
    """Stage cannot proceed until the user handles something manually."""


# Stages auto-run may execute. review is never in here: approval is manual.
RUN_STAGES = [s for s in db.STAGES if s != "review"]


# ------------------------------------------------------------- helpers ---

def _connect(cfg):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    return conn


def _get_prod(cfg, pid: int):
    conn = _connect(cfg)
    try:
        return db.get_production(conn, pid)
    finally:
        conn.close()


def _db_llm_default(cfg) -> str | None:
    """The global default LLM saved in the database (Settings > LLM), or None
    when nothing is saved. The database is the only source - there is no
    config.yaml fallback anymore."""
    try:
        return studio.llm_default(cfg)
    except Exception:  # noqa: BLE001 - never block a run on settings storage
        return None


def _default_provider(cfg, pid: int) -> str | None:
    """Which LLM a production's style/script/shots use: the production's own
    choice, then its channel's producer preference, then the database default
    (Settings > LLM).

    Without the channel step a channel set to `deepseek` still ran the pipeline
    on the global `glm-flash`, which stalls on large prompts (the channel's
    `producer_llm_provider` was only used for topic picking)."""
    prod = _get_prod(cfg, pid)
    if prod and prod["llm_provider"]:
        return prod["llm_provider"]
    if prod:
        try:
            pref = (_effective(cfg, pid) or {}).get("producer_llm_provider")
            if pref:
                return pref
        except Exception:  # noqa: BLE001 - never block a run on settings
            pass
    return _db_llm_default(cfg)


def style_bible(cfg, conn, prod, pdir):
    """Resolve the style guide + bible the planner should use.

    The CHANNEL's live DB value is the source of truth, so editing the channel
    propagates to every production of it immediately. A production can opt out
    with `style_override` / `bible_override` - then its own style.md / bible.md
    wins instead. Returns (style_guide, style_src, bible_text, bible_src) where
    *_src is "production" | "channel" | "none" so the UI can show which is in
    force. A file with no channel value still resolves as "production" (the
    seed copy), so a channel-less production keeps working."""
    eff = settings.for_production(conn, prod)

    def pick(override, file_name, channel_val):
        f = pdir / file_name
        if override and f.exists():
            return f.read_text(encoding="utf-8").strip(), "production"
        if (channel_val or "").strip():
            return channel_val.strip(), "channel"
        if f.exists():
            return f.read_text(encoding="utf-8").strip(), "production"
        return "", "none"

    style_guide, style_src = pick(
        bool(settings.row_get(prod, "style_override", 0)), "style.md",
        eff["style"])
    bible_text, bible_src = pick(
        bool(settings.row_get(prod, "bible_override", 0)), "bible.md",
        eff["bible"])
    return style_guide, style_src, bible_text, bible_src


def is_flow_native(eff) -> bool:
    """True when the production's render resolution is 'Flow native'."""
    return str(eff.get("render_resolution") or "").lower() == "flow-native"


def _upscale_for(eff) -> int:
    """The upscale the ENGINE applies while downloading. 'Flow native' render
    resolution means NONE (0): the stills are downloaded at Flow's master
    size (1376x768) - any upscaling happens afterwards, as one local pass
    (post_download_tier). Otherwise the channel/global upscale setting."""
    if is_flow_native(eff):
        return 0
    return eff["upscale"]


def post_download_tier(eff) -> str:
    """The FlowBatch tier ("1k" | "2k" | "4k") of the ONE local Real-ESRGAN
    pass that runs after the stills are downloaded, or "off".

    Only a Flow native production has one: it is the level picked next to
    Render resolution (channel, else Settings). Every other resolution keeps
    upscaling inside the engine, exactly as before."""
    if not is_flow_native(eff):
        return "off"
    tier = str(eff.get("flow_native_upscale") or "off").strip().lower()
    return tier if tier in ("1k", "2k", "4k") else "off"


def manual_upscale_tier(eff) -> str:
    """The tier the IMAGES stage's manual "Upscale images (local)" button
    uses: the Flow native level, else the channel's upscale tier."""
    if is_flow_native(eff):
        return post_download_tier(eff)
    return studio.FLOWBATCH_TIERS.get(int(eff.get("upscale") or 0), "off")


def _upscale_text(eff) -> str:
    """How the images step is described in a plan/step detail."""
    post = post_download_tier(eff)
    if post != "off":
        return f"native size, then local Real-ESRGAN {post}"
    return f"upscale {_upscale_for(eff)}"


def _stage_provider(cfg, pid: int, stage: str,
                    override: str | None = None) -> str | None:
    """Which LLM one stage uses: an explicit choice for THIS action, else the
    production's saved pick for that stage (only when it still exists and is
    ready - a deleted provider must not come back), else the Default LLM."""
    if override:
        return override
    prod = _get_prod(cfg, pid)
    if prod is not None:
        saved = db.stage_provider(prod, stage)
        if saved and studio.provider_ready(cfg, saved):
            return saved
    return _default_provider(cfg, pid)


def _set_stage(cfg, pid: int, stage: str) -> None:
    conn = _connect(cfg)
    try:
        db.update_production(conn, pid, stage=stage)
    finally:
        conn.close()


def _advance(cfg, pid: int, completed: str) -> None:
    """Move the production pointer past a completed (or already done) stage."""
    idx = db.STAGES.index(completed)
    if idx + 1 < len(db.STAGES):
        _set_stage(cfg, pid, db.STAGES[idx + 1])


def _source_transcript_text(cfg, pid: int) -> str:
    """Source transcript for the style stage: the copied artifact in the
    production folder, or the source video's transcript from storage."""
    f = studio.find_source_transcript(studio.prod_dir(cfg, pid))
    if f:
        try:
            return f.read_text(encoding="utf-8")
        except OSError:
            pass
    prod = _get_prod(cfg, pid)
    if not prod or not prod["source_video_id"]:
        return ""
    conn = _connect(cfg)
    try:
        row = db.get_video(conn, prod["source_video_id"])
    finally:
        conn.close()
    if row and row["transcript_path"] and Path(row["transcript_path"]).exists():
        return Path(row["transcript_path"]).read_text(encoding="utf-8")
    return ""


def _effective(cfg, pid: int) -> dict:
    """Effective production/auto-run values for a production: global settings
    <- its own channel <- the production itself (see settings.for_production)."""
    conn = _connect(cfg)
    try:
        return settings.for_production(conn, db.get_production(conn, pid))
    finally:
        conn.close()


def _default_render_mode(cfg, prod) -> str:
    """Resolve the image source to 'flow' or 'api': the production's saved
    choice wins, then its own channel's default, then the global setting.
    'auto' (the global default) means Flow when its driver is installed,
    otherwise the Renderly API."""
    if prod is not None and settings.row_get(prod, "render_mode"):
        mode = prod["render_mode"]
    else:
        conn = _connect(cfg)
        try:
            mode = settings.for_production(conn, prod)["render_mode"]
        finally:
            conn.close()
    if mode not in ("flow", "api"):
        mode = "flow" if studio.flow_driver_ready(cfg) else "api"
    return mode


def _missing_images(pdir: Path) -> list[str]:
    """Shotlist image files that have not been rendered yet.

    Matches sanitize_shotlist semantics: entries are compared by
    case-folded stem (NTFS is case-insensitive, drivers may save a
    different extension), and promptless entries are skipped because they
    can never be rendered."""
    path = pdir / "shotlist.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    img_dir = pdir / "images"
    existing = ({p.stem.lower() for p in img_dir.iterdir() if p.is_file()}
                if img_dir.exists() else set())
    out = []
    for i in data.get("images", []):
        if not (isinstance(i, dict) and i.get("file") and i.get("prompt")):
            continue
        if Path(i["file"]).stem.lower() not in existing:
            out.append(i["file"])
    return out


def _shotlist_image_count(pdir: Path) -> int:
    path = pdir / "shotlist.json"
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return len(data.get("images", []))
    except (ValueError, OSError):
        return 0


def _merge_pause_reason(cfg, pid: int) -> str | None:
    """Why auto-run must not merge yet, or None. An unattended merge must
    never silently sanitize unrendered shots away (that permanently drops
    them from the plan) - a human decides instead."""
    pdir = studio.prod_dir(cfg, pid)
    total = _shotlist_image_count(pdir)
    missing = len(_missing_images(pdir))
    if total and missing:
        return (f"only {total - missing} of {total} shotlist images rendered "
                f"- resume the images stage first (or mark it done by hand "
                f"to merge with what exists)")
    return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return len(data.get("images", []))
    except (ValueError, OSError):
        return 0


# ------------------------------------------------------- stage runners ---
# One function per stage; the manual HTTP routes and the auto-run runner
# both go through these so single-stage and pipeline behavior match.


def _run_style(cfg, pid: int, provider: str | None = None) -> None:
    conn = _connect(cfg)
    try:
        prod = db.get_production(conn, pid)
        if not prod:
            return
        source_text = _source_transcript_text(cfg, pid)
        if not source_text:
            raise _Paused(
                "No source transcript - write the style guide manually")
        word_count = len(re.findall(r"\w+", source_text))
        prompt = studio.style_prompt(prod["title"], prod["genre"],
                                     source_text,
                                     extra_direction=db.stage_extra(
                                         prod, "style"))
        text = studio.llm_generate(cfg, prompt, provider=provider)
        if not text:
            raise RuntimeError("LLM returned an empty style guide")
        pdir = studio.prod_dir(cfg, pid)
        (pdir / "writing_style.md").write_text(text + "\n", encoding="utf-8")
        db.add_step(conn, pid, "style", "auto",
                    detail=f"{provider}, source ~{word_count} words")
    finally:
        conn.close()


RESEARCH_NOTES_FILE = "research_notes.md"
# Records what the cached notes were built from. Notes made before this existed
# (or from a different source) are re-checked instead of trusted blindly: an old
# one-shot build could stop partway through the transcript.
RESEARCH_NOTES_META = "research_notes.json"
# A notes file with no meta that is shorter than this share of the source's
# word count is treated as cut off and rebuilt once.
NOTES_LEGACY_MIN_RATIO = 0.35


def save_manual_notes(pdir: Path, text: str) -> None:
    """Store user-supplied research notes and mark them manual, so the cache
    check keeps them instead of rebuilding them from the source."""
    pdir = Path(pdir)
    (pdir / RESEARCH_NOTES_FILE).write_text(text.strip() + "\n",
                                            encoding="utf-8")
    (pdir / RESEARCH_NOTES_META).write_text(
        json.dumps({"manual": True}), encoding="utf-8")


def _notes_cache_valid(pdir: Path, notes: str, source_text: str) -> bool:
    try:
        meta = json.loads((Path(pdir) / RESEARCH_NOTES_META)
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = None
    if isinstance(meta, dict) and meta.get("manual"):
        return True  # written or pasted by the user: never rebuilt
    if isinstance(meta, dict) and "source_chars" in meta:
        return meta["source_chars"] == len(source_text or "")
    src_words = len((source_text or "").split())
    return not src_words or (len(notes.split())
                             >= NOTES_LEGACY_MIN_RATIO * src_words)

# Flow's "still busy" wave leaves some cards unrendered. Rather than failing the
# stage, pause (the account settles) and resume: both engines skip what already
# exists, so a round only attempts the missing cards.
IMAGE_RESUME_PAUSE_SECONDS = 300
IMAGE_RESUME_ROUNDS = 3

# A harder stop: 3 cards failed in a row (studio.py's run_imagegen_flow) means
# Flow is actively refusing the session (usually a reCAPTCHA score dip after
# ~80-100 generations), not just a "still busy" timeout - the same skip-what-
# exists resume works, but it needs longer to clear, so it gets its own pause.
FLOW_REFUSAL_PAUSE_SECONDS = 600


def _research_notes(cfg, pdir: Path, title: str, genre: str,
                    source_text: str, provider: str | None,
                    refresh: bool = False) -> str:
    """Cached fact-notes for this production, generated once by the writer's
    provider. The script is composed FROM these rather than from the transcript
    prose, so it stops echoing the source: the first live run measured 93.5%
    overlap with the transcript and the copycat gate rejects over 20%.

    `refresh` re-derives the notes from the source transcript instead of using
    the cache - a manual regenerate wants a different factual phrasing so the
    writer does not reproduce the previous script verbatim."""
    pdir = Path(pdir)
    path = pdir / RESEARCH_NOTES_FILE
    if not refresh:
        try:
            cached = path.read_text(encoding="utf-8").strip()
            if cached and _notes_cache_valid(pdir, cached, source_text):
                _log_line(f"using cached {RESEARCH_NOTES_FILE} "
                          f"({len(cached.split())} words)")
                return cached
            if cached:
                _log_line(f"cached {RESEARCH_NOTES_FILE} looks incomplete "
                          f"({len(cached.split())} words for a "
                          f"{len((source_text or '').split())}-word source) - "
                          f"rebuilding it")
        except OSError:
            pass
    elif path.exists():
        _log_line(f"regenerate: rebuilding {RESEARCH_NOTES_FILE} for a fresh take")
    parts = studio.split_for_notes(source_text)
    pieces: list[str] = []
    try:
        for i, chunk in enumerate(parts, 1):
            if len(parts) > 1:
                _log_line(f"building research notes: part {i}/{len(parts)}")
            out = studio.llm_generate(
                cfg, studio.notes_prompt(title, genre, chunk,
                                         part=(i, len(parts))),
                provider=provider, max_tokens=studio.NOTES_MAX_TOKENS)
            out = (out or "").strip()
            if not out:
                raise RuntimeError(f"part {i}/{len(parts)} came back empty")
            chunk_words = len(chunk.split())
            if chunk_words and len(out.split()) < 0.08 * chunk_words:
                _log_line(f"warning: notes for part {i}/{len(parts)} are very "
                          f"short ({len(out.split())} words for {chunk_words})")
            pieces.append(out)
    except Exception as exc:  # noqa: BLE001 - notes are an optimisation
        _log_line(f"could not build research notes ({exc}); writing from the "
                  f"transcript")
        return source_text
    notes = "\n\n".join(pieces).strip()
    try:
        path.write_text(notes + "\n", encoding="utf-8")
        (pdir / RESEARCH_NOTES_META).write_text(
            json.dumps({"source_chars": len(source_text or ""),
                        "parts": len(parts),
                        "words": len(notes.split())}) + "\n",
            encoding="utf-8")
    except OSError:
        pass
    _log_line(f"built {RESEARCH_NOTES_FILE} ({len(notes.split())} words, "
              f"{len(parts)} part(s))")
    return notes


def _script_gate(words: int, target_words: int, overlap: float,
                 score: float | None, min_rating: float, max_overlap: float,
                 hard_overlap: float,
                 judge_error: str | None = None) -> tuple[bool, list[str], bool, bool]:
    """(passed, reasons, too_long, too_short) for one script attempt.

    A stub must not slip through: a judge that cannot rate returns score None,
    which used to reject with an EMPTY reason, and nothing rejected a draft far
    under the target (a 1067-word answer to a 3531-word target was only rejected
    because the judge was down)."""
    too_long = words > int(target_words * 1.15)
    # 0.6 used to let a 67-71%-of-target draft win "best of attempts" purely
    # on judge score - confirmed on two real productions where a shorter
    # draft outscored a fuller one because it had less material for the
    # judge to find repetition/overreach in (see the ranking comment in
    # _run_script). 0.8 was chosen directly by the channel owner after
    # seeing those real word counts, to make the selection favor length
    # more than the judge's rating alone does.
    too_short = words < int(target_words * 0.8)
    passed = (overlap <= max_overlap and overlap <= hard_overlap
              and not too_short
              and score is not None and score >= min_rating)
    reasons: list[str] = []
    if too_short:
        reasons.append(f"~{words} words is well under the {target_words}-word "
                       f"target")
    if score is None:
        reasons.append("the judge could not rate the draft"
                       + (f" ({judge_error})" if judge_error else ""))
    elif score < min_rating:
        reasons.append(f"rating {score} < {min_rating}")
    if overlap > hard_overlap:
        reasons.append(f"overlap {overlap:.1%} over the hard "
                       f"{hard_overlap:.0%} limit")
    elif overlap > max_overlap:
        reasons.append(f"overlap {overlap:.1%} over the {max_overlap:.0%} "
                       f"target")
    return passed, reasons, too_long, too_short


def _script_criteria_line(score: float | None, min_rating: float,
                          overlap: float, max_overlap: float,
                          hard_overlap: float, words: int,
                          target_words: int, too_short: bool) -> str:
    """A one-line ✓/✗ checklist for the History panel - the plain numbers
    ("rating 6.9 (min 7.5)") made someone re-derive pass/fail from the
    thresholds every time; this states it directly per criterion."""
    rating_ok = score is not None and score >= min_rating
    overlap_ok = overlap <= max_overlap and overlap <= hard_overlap
    length_ok = not too_short
    rating_mark = "✓" if rating_ok else "✗"
    overlap_mark = "✓" if overlap_ok else "✗"
    length_mark = "✓" if length_ok else "✗"
    rating_text = score if score is not None else "n/a"
    pct = f"{words / target_words:.0%}" if target_words else "?"
    return (f"{rating_mark} rating {rating_text} (min {min_rating})  "
           f"{overlap_mark} overlap {overlap:.1%} (max {max_overlap:.0%})  "
           f"{length_mark} length {words}/{target_words} words ({pct})")


def _run_script(cfg, pid: int, provider: str | None = None) -> None:
    t0 = time.monotonic()
    conn = _connect(cfg)
    try:
        prod = db.get_production(conn, pid)
        if not prod:
            raise RuntimeError("Unknown production")
        source_text = _source_transcript_text(cfg, pid)
        eff = _effective(cfg, pid)
    finally:
        conn.close()
    pdir = studio.prod_dir(cfg, pid)
    # The script is written and judged against the WRITING style guide from
    # stage 1 (writing_style.md). The channel's art style (style.md) is for
    # images only and must never be handed to the writer/judge as a voice guide.
    style = studio.find_writing_style(pdir)
    style_guide = style.read_text(encoding="utf-8") if style else ""
    if not style_guide.strip():
        _log_line("no writing style guide (run the style stage first) - "
                  "writing without one")
    source_words = len(re.findall(r"\w+", source_text)) if source_text else 0
    target_words = studio.script_target_words(cfg.studio_script_words,
                                              source_words)
    script_path = pdir / "script.md"
    auto_dir = pdir / "versions" / "script"
    auto_dir.mkdir(parents=True, exist_ok=True)
    # How many scripts already exist drives the variation rotation: the first
    # run starts at angle 0, each manual regenerate advances it, so two
    # regenerates never ask the writer for the same angle (and this run is a
    # regenerate, so the notes are rebuilt too).
    try:
        archived = len(list(auto_dir.glob("auto-*.md")))
    except OSError:
        archived = 0
    run_index = archived + (1 if script_path.exists() else 0)
    regenerating = run_index > 0
    if regenerating:
        _log_line(f"regenerate: {run_index} script version(s) already exist - "
                  f"asking for a substantially different take")
    # Compose from NEUTRAL NOTES, not the transcript prose - otherwise the writer
    # echoes the source and every attempt fails the copycat gate. Cached once;
    # a regenerate rebuilds them for a fresh factual phrasing.
    facts = (_research_notes(cfg, pdir, prod["title"], prod["genre"],
                             source_text, provider, refresh=regenerating)
             if source_text.strip() else "")
    # rate_script() below is given `facts`, not `source_text`. The writer is
    # told "use ONLY these facts" (facts), so that has to be the judge's
    # ground truth too - grading against the raw transcript instead silently
    # disagreed with the writer on what the source even says. rating_prompt()
    # further caps whatever it's handed at JUDGE_SOURCE_CHARS=12000 chars; on
    # a transcript over that (raw source_text often is, facts rarely is) the
    # judge only ever saw the first ~half of it and flagged every real fact
    # past that cutoff as "absent from the SOURCE FACTS" - confirmed on
    # production 19, where items 8-12 of a 12-item list sit past char 16,500
    # of the raw transcript and were rejected on every attempt despite being
    # in `facts` (and in the script) all along.
    min_rating = eff["script_min_rating"]
    max_overlap = eff["script_max_overlap"]
    hard_overlap = eff["script_hard_overlap"]
    attempts_allowed = max(1, int(eff["script_max_attempts"]))
    # Pin the judge for this production once resolved, and reuse it on every
    # later resume/regenerate - same reasoning as shots_judge below. Without
    # this, "best of attempts" (and "does a regenerate beat the existing
    # script") silently compared scores from DIFFERENT judge models that do
    # not agree on the same scale, since judge_provider() re-resolves from
    # whichever provider happens to be ready each time it is called.
    judge = db.stage_provider(prod, "script_judge")
    if not judge:
        judge = studio.judge_provider(cfg, provider, eff["script_judge_provider"])
        if judge:
            conn = _connect(cfg)
            try:
                db.set_stage_provider(conn, pid, "script_judge", judge)
            finally:
                conn.close()

    # A regenerate must never silently make things worse: rate the script
    # that is ALREADY there (if any) as a baseline this run's attempts have
    # to beat. Without this, "best of this run's attempts" only ever compares
    # new drafts against each other - a run whose attempts all score worse
    # than what was already accepted would still overwrite it, and if the
    # judge could not score anything (score None on every attempt, as the
    # JSON-parsing bug used to cause on nearly every real call), "best" fell
    # through to "lowest overlap wins", a tiebreak with no relationship to
    # writing quality at all.
    baseline = None
    if script_path.exists():
        existing_text = script_path.read_text(encoding="utf-8")
        existing_overlap = studio.overlap_ratio(existing_text, source_text)
        existing_words = len(re.findall(r"\w+", existing_text))
        existing_rating = studio.rate_script(
            cfg, prod["title"], prod["genre"], existing_text, facts,
            style_guide, judge, temperature=eff["script_judge_temperature"],
            extra_direction=db.stage_extra(prod, "script"))
        existing_passed, _why, _tl, _ts = _script_gate(
            existing_words, target_words, existing_overlap,
            existing_rating["score"], min_rating, max_overlap, hard_overlap,
            existing_rating.get("error"))
        baseline = {"attempt": 0, "text": existing_text,
                   "overlap": existing_overlap, "runs": [],
                   "score": existing_rating["score"], "rating": existing_rating,
                   "passed": existing_passed, "words": existing_words,
                   "too_long": False, "too_short": False, "is_baseline": True}
        _log_line(
            f"regenerate: existing script rates "
            f"{existing_rating['score'] if existing_rating['score'] is not None else 'n/a'} "
            f"(overlap {existing_overlap:.1%}) - a new attempt must beat this "
            "to replace it")

    attempts: list[dict] = []
    previous: dict | None = None
    text = ""
    for attempt in range(1, attempts_allowed + 1):
        variation = studio.variation_nudge(
            attempt=attempt,
            overlap=previous["overlap"] if previous else None,
            runs=previous["runs"] if previous else None,
            feedback=previous["feedback"] if previous else None,
            version=run_index)
        if previous and previous.get("too_long"):
            variation = ((variation + "\n") if variation else "") + (
                f"The previous draft was too long. Keep this one at or under "
                f"{target_words} words.")
        if previous and previous.get("too_short"):
            variation = ((variation + "\n") if variation else "") + (
                f"The previous draft was far too short. Write the full "
                f"{target_words} words and cover every fact in the notes.")
        prompt = studio.script_prompt(
            prod["title"], prod["genre"], facts, style_guide,
            target_words=target_words, variation=variation,
            extra_direction=db.stage_extra(prod, "script"))
        text = studio.llm_generate(
            cfg, prompt, provider=provider,
            max_tokens=studio.script_max_tokens(target_words))
        if not text:
            raise RuntimeError("LLM returned an empty script")
        # A draft that stops mid-sentence/mid-word tanks the judge's `ending`
        # criterion to 1 regardless of how good everything before the cutoff
        # is - confirmed on real attempts that cut off well under their token
        # budget. Ask it to finish rather than score (and burn the whole
        # attempt on) a draft that was never actually done.
        for cont in range(studio.SCRIPT_CONTINUE_ROUNDS):
            if not studio.script_looks_truncated(text):
                break
            _log_line(f"attempt {attempt}: output looks cut off mid-sentence "
                      f"- continuing ({cont + 1}/"
                      f"{studio.SCRIPT_CONTINUE_ROUNDS})")
            more = studio.llm_generate(
                cfg, studio.script_continuation_prompt(prompt, text),
                provider=provider,
                max_tokens=studio.script_max_tokens(target_words))
            if not more:
                break
            # No separator inserted here on purpose - script_continuation_prompt
            # tells the model whether a leading space belongs at the seam (it
            # knows whether ITS OWN reply continues a word or starts a new
            # one; we don't). A blind "\n\n" here once split the word "rather"
            # into "r" + a paragraph break + "ather" mid-sentence, on a real
            # attempt whose structure/pacing scores cratered as a direct
            # result - see script_continuation_prompt's docstring.
            text = text + more
        words = len(re.findall(r"\w+", text))
        overlap = studio.overlap_ratio(text, source_text)
        runs = studio.overlap_runs(text, source_text) if overlap > 0 else []
        rating = studio.rate_script(cfg, prod["title"], prod["genre"], text,
                                    facts, style_guide, judge,
                                    temperature=eff["script_judge_temperature"],
                                    extra_direction=db.stage_extra(prod, "script"))
        score = rating["score"]
        passed, why, too_long, too_short = _script_gate(
            words, target_words, overlap, score, min_rating, max_overlap,
            hard_overlap, rating.get("error"))
        (auto_dir / f"attempt-{attempt}.md").write_text(text + "\n",
                                                        encoding="utf-8")
        attempts.append({"attempt": attempt, "text": text, "overlap": overlap,
                         "runs": runs, "score": score, "rating": rating,
                         "passed": passed, "words": words,
                         "too_long": too_long, "too_short": too_short})
        log_attempt = (f"attempt {attempt}: rating "
                       f"{score if score is not None else 'n/a'}, overlap "
                       f"{overlap:.1%}, ~{words} words"
                       + (" (too long)" if too_long else "")
                       + (" (too short)" if too_short else ""))
        if passed:
            _log_line(log_attempt + " - accepted")
            break
        _log_line(log_attempt + " - rejected (" + "; ".join(why) + ")")
        previous = {"overlap": overlap, "runs": runs,
                    "feedback": rating["feedback"] or rating["weak_spans"],
                    "too_long": too_long, "too_short": too_short}

    # settle for the best draft rather than shipping a rejected one blindly -
    # but "best" must include the script already on disk (the baseline), or a
    # run whose attempts are all worse would still overwrite it
    candidates = attempts + ([baseline] if baseline else [])
    # A too-short draft has less surface area for the judge to find repetition
    # or overreach in, so it can score HIGHER than a fuller draft purely by
    # having less content to criticize - confirmed on a real run where a
    # 1685-word attempt (44% of a 3827-word target, well under the 60%
    # too_short floor) outscored four longer attempts and was picked as
    # "best" on rating alone. Rank any non-too-short candidate above every
    # too-short one first, and only fall back to a too-short candidate when
    # NOTHING else is available - score (then overlap) still breaks ties
    # within each group.
    best = max(candidates,
              key=lambda a: (not a["too_short"], (a["score"] or 0),
                            -a["overlap"]))
    kept_existing = bool(baseline) and best is baseline
    text = best["text"]
    passed = best["passed"]
    if not kept_existing:
        if script_path.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S")
            shutil.copy(script_path, auto_dir / f"auto-{stamp}.md")
        script_path.write_text(text + "\n", encoding="utf-8")
    # Persist the judge's per-attempt scores, criteria and feedback (plus the
    # baseline's, when rated, so it's visible why it won or lost). Without
    # this the only trace of WHY a draft was rejected is the log line, and a
    # "rating 5.5 vs min 9.4" warning is unactionable.
    (auto_dir / "review.json").write_text(
        json.dumps([{"attempt": a["attempt"], "score": a["score"],
                     "overlap": round(a["overlap"], 4), "passed": a["passed"],
                     "words": a.get("words"), "too_long": a.get("too_long"),
                     "is_baseline": bool(a.get("is_baseline")),
                     "criteria": a["rating"].get("criteria") or {},
                     "feedback": a["rating"].get("feedback") or [],
                     "weak_spans": a["rating"].get("weak_spans") or [],
                     "judge_error": a["rating"].get("error")}
                    for a in candidates], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")

    conn = _connect(cfg)
    try:
        if kept_existing:
            detail = (
                f"regenerate: {len(attempts)} new attempt(s) via {provider}, "
                f"none beat the existing script (rating "
                f"{best['score'] if best['score'] is not None else 'n/a'}, "
                f"overlap {best['overlap']:.1%}) - kept it unchanged, "
                f"judged by {judge}, "
                f"took {format_duration(time.monotonic() - t0)}")
        else:
            criteria_line = _script_criteria_line(
                best["score"], min_rating, best["overlap"], max_overlap,
                hard_overlap, best.get("words", 0), target_words,
                best.get("too_short", False))
            detail = (f"{provider}, best of {len(attempts)} attempt(s) via "
                      f"{judge}, took {format_duration(time.monotonic() - t0)}"
                      f"\n{criteria_line}")
        # the run's provider is NOT persisted onto the production: an implicit
        # pin made later runs ignore a changed Default LLM (a deleted provider
        # kept "coming back" through this row). Which LLM ran is in the step
        # history; the per-run override stays per-run.
        if passed:
            db.add_step(conn, pid, "script", "auto", detail=detail)
        else:
            # "accept best and continue" means the draft IS this stage's
            # artifact, so the step counts as done. Recording it as failed
            # would make a later resume regenerate the script - and the audio
            # and subtitles were already built from this one. The warning
            # column is what flags it to the user.
            if kept_existing:
                warning = (
                    f"regenerate did not beat the existing script (min "
                    f"{min_rating}) - kept it unchanged rather than replace "
                    "it with something worse")
            else:
                criteria_line = _script_criteria_line(
                    best["score"], min_rating, best["overlap"], max_overlap,
                    hard_overlap, best.get("words", 0), target_words,
                    best.get("too_short", False))
                warning = (
                    f"script gate failed after {len(attempts)} attempt(s) - "
                    f"kept the best:\n{criteria_line}")
            judge_err = best["rating"].get("error")
            if best["score"] is None and judge_err:
                # otherwise "rating n/a" hides that the JUDGE failed, not the script
                warning += f" | judge: {judge_err[:200]}"
            db.update_production(conn, pid, warning=warning)
            db.add_step(conn, pid, "script", "auto",
                        detail=detail + " | " + warning)
    finally:
        conn.close()


def _log_line(message: str) -> None:
    """Script-attempt progress goes to the job log and the server log."""
    logging.getLogger("whisperradar").info("[script] %s", message)


def _run_audio(cfg, pid: int) -> None:
    if not cfg.studio_tts_command:
        raise _Paused("No tts_command in config.yaml - upload audio, "
                      "then Resume")
    conn = _connect(cfg)
    try:
        pdir = studio.prod_dir(cfg, pid)
        script = studio.find_script(pdir)
        if not script:
            raise RuntimeError("Write the script first")
        out = pdir / "audio.mp3"
        voice = _effective(cfg, pid)["voice"]
        # Hand the TTS hook the AI33 key resolved from Settings > LLM (or
        # config/env), so it does not depend on the process environment.
        hook_env = None
        ai33_key = ai33.api_key(cfg)
        if ai33_key:
            hook_env = {**os.environ, "WR_AI33_API_KEY": ai33_key}
        studio.run_hook(cfg.studio_tts_command,
                        {"script": script, "out": out, "voice": voice or ""},
                        env=hook_env)
        audio = studio.find_audio(pdir)
        if not audio:
            raise RuntimeError("TTS produced no audio file")
        db.add_step(conn, pid, "audio", "auto",
                    detail=audio.name + (f", voice {voice}" if voice else ""))
    finally:
        conn.close()


def _run_srt(cfg, pid: int) -> None:
    t0 = time.monotonic()
    conn = _connect(cfg)
    try:
        pdir = studio.prod_dir(cfg, pid)
        audio = studio.find_audio(pdir)
        if not audio:
            raise RuntimeError("Upload or generate the audio first")
        meta = transcribe.transcribe_to_srt(
            audio, pdir / "subtitles.srt",
            model_size=cfg.whisper_model,
            language=cfg.whisper_language,
        )
        db.add_step(
            conn, pid, "srt", "auto",
            detail=f"{meta['cues']} cues, {meta['device']}, "
                   f"lang {meta['language']}, "
                   f"took {format_duration(time.monotonic() - t0)}",
        )
    finally:
        conn.close()


_BEST_EVER_FILE = "best_ever.json"


def _rank(entry: dict | None) -> tuple:
    """Sort key for a shotlist attempt: fault-free beats faulty, then higher
    ratio wins. A missing entry ranks below everything real."""
    if not entry:
        return (False, -1.0)
    return (not entry.get("faults"), entry.get("ratio") or 0.0)


def _load_best_ever(pdir) -> dict | None:
    """The best shotlist attempt ever recorded for this production, across
    every past resume - or None if there isn't one yet. Never raises: a
    missing or corrupt file just means no prior best to beat."""
    path = pdir / "versions" / "shotlist" / _BEST_EVER_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_best_ever(pdir, entry: dict) -> None:
    """Persist the best shotlist attempt so a later, worse resume can never
    silently lose it. Never raises - this is a nice-to-have, not load-bearing."""
    path = pdir / "versions" / "shotlist" / _BEST_EVER_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        keep = {k: v for k, v in entry.items() if k != "warnings"}
        path.write_text(json.dumps(keep, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    except OSError as exc:
        logging.getLogger("whisperradar").debug(
            "could not save the best-ever shotlist: %s", exc)


def _shots_criteria_line(faults: list, matched: int, total: int,
                         ratio: float, min_align: float) -> str:
    """A one-line ✓/✗ checklist for the History panel, mirroring
    _script_criteria_line - "0 faults, 93% (min 90%)" still makes someone
    check the threshold themselves; this states pass/fail directly."""
    faults_ok = not faults
    ratio_ok = ratio >= min_align
    faults_mark = "✓" if faults_ok else "✗"
    ratio_mark = "✓" if ratio_ok else "✗"
    return (f"{faults_mark} faults {len(faults)}  "
           f"{ratio_mark} detail {matched}/{total} shots "
           f"({ratio:.0%}, min {min_align:.0%})")


_MOTION_ORDER = ["ZI", "ZO", "PL", "PR", "PU", "PD", "PV", "ST"]


def _motion_breakdown_line(shots: list) -> str:
    """Count each shot's motion code for the History panel. The manifest
    brief caps any single motion at ~40% of shots and ST (static) at ~10% -
    seeing the actual mix at a glance flags a plan leaning on one motion too
    heavily before it ever reaches rendering, instead of only discovering it
    once Renderly/Flow is already working through the batch."""
    counts = Counter(s.get("motion") or "?" for s in shots)
    total = len(shots) or 1
    parts = []
    for code in _MOTION_ORDER:
        n = counts.pop(code, 0)
        if n:
            parts.append(f"{code} {n} ({n / total:.0%})")
    for code, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        parts.append(f"{code} {n} ({n / total:.0%})")
    return "motion: " + ", ".join(parts) if parts else ""


def _run_shots(cfg, pid: int, provider: str | None = None) -> None:
    t0 = time.monotonic()
    conn = _connect(cfg)
    try:
        prod = db.get_production(conn, pid)
        pdir = studio.prod_dir(cfg, pid)
        srt = studio.find_srt(pdir)
        if not srt:
            raise RuntimeError("Generate or upload the subtitles first")
        # Seed from the channel / seed folder FIRST, every run: this copies any
        # new channel refs into the production's refs\ (file-by-file, never
        # overwriting) so the planner's supplied-refs inventory sees them -
        # independent of whether the bible gate needs it. (Skipping this when
        # the channel bible satisfied the gate used to strand channel refs.)
        studio.seed_production(cfg, conn, prod)
        style_guide, style_src, bible_text, bible_src = style_bible(
            cfg, conn, prod, pdir)
        if not bible_text:
            # the manifest-authoring brief's bible gate: the LLM will refuse
            # to plan without one (it just asks for the bible instead)
            raise _Paused("no character/reference bible - the planning brief "
                          "requires one before planning - write it at the "
                          "shots stage, then Resume")
        srt_text = srt.read_text(encoding="utf-8")
        cues = studio.parse_srt_cues(srt_text)
        # The planner never writes timings (the cue ranges carry them), so send
        # `N: text (Ns)` instead of full SRT blocks: cue numbers plus per-cue
        # DURATION, without which the planner cannot pace its shots (a 16-minute
        # narration once came back as 34 shots). The file keeps its timestamps -
        # the pacing gate and the ImgToVideo assembler read them from disk.
        narration = studio.compact_srt(srt_text)
        if len(narration) < len(srt_text):
            _log_line(f"shotlist: narration {len(srt_text)} -> "
                      f"{len(narration)} chars (timestamps dropped, cue "
                      f"numbers and durations kept)")
        eff = _effective(cfg, pid)
        min_align = eff["shotlist_min_alignment"]
        # the channel's planning-brief profile: its motion policy fills the
        # brief's motion/pacing slots AND sets what the review gates enforce
        # (a long-hold profile carries its own hold range, which replaces the
        # global 12s ceiling), its presentation text says who is on screen
        profile = briefs.resolve_profile(
            eff.get("brief_motion"), eff.get("brief_min_hold"),
            eff.get("brief_max_hold"),
            default_max=eff["shotlist_max_hold_seconds"],
            custom=eff.get("brief_custom"))
        presentation = eff.get("brief_presentation") or ""
        max_hold = profile.max_hold or eff["shotlist_max_hold_seconds"]
        brief = studio.load_manifest_brief(cfg, profile, presentation)
        _log_line(f"shotlist: planning brief - motion '{profile.key}'"
                  + (f", hold {profile.min_hold:g}-{max_hold:g}s"
                     if profile.min_hold else f", max hold {max_hold:g}s")
                  + (", presentation set" if presentation.strip() else ""))
        try:  # what this production was planned with (best-effort record)
            used = pdir / "versions" / "shotlist" / "brief_used.md"
            used.parent.mkdir(parents=True, exist_ok=True)
            used.write_text(brief + "\n", encoding="utf-8")
        except OSError:
            pass
        # a channel with references off must get NO references at all - the
        # planner is told, and the gate rejects any plan that still uses refs
        allow_refs = bool(eff["generate_references"])
        attempts_allowed = max(1, int(eff["shotlist_max_attempts"]))
        # Pin the judge for this production once resolved, and reuse it on
        # every later resume. Without this, a production whose effective
        # judge provider drifted between resumes (the setting changed, or an
        # unset provider auto-resolved differently) was graded by a
        # different model run to run - "detailed enough" is not a fixed bar,
        # different judges do not agree on it, so "best of attempts" across
        # resumes was comparing scores that were never on the same scale
        # (observed on this very production: 85% under one judge, 63% under
        # another, on essentially the same shotlist).
        judge = db.stage_provider(prod, "shots_judge")
        if not judge:
            judge = studio.judge_provider(cfg, provider,
                                          eff["shotlist_judge_provider"])
            if judge:
                db.set_stage_provider(conn, pid, "shots_judge", judge)
                prod = db.get_production(conn, pid)
        feedback = ""
        # pacing arithmetic: the narration's total length and the max hold
        # give a hard minimum shot count - without it a planner that cannot
        # see durations under-plans (e.g. 34 shots for ~960s) and the review
        # fails after a very expensive generation
        total_s = (studio._srt_seconds(cues[-1]["end"]) - 0.0
                   if cues else 0.0)
        pacing_note = briefs.pacing_note(profile, total_s, len(cues),
                                         max_hold, min_align)
        attempts: list[dict] = []
        data: dict = {}
        sheet = ""
        # Stop burning attempts once a few in a row fail to beat the best
        # this run has produced - a full re-plan does not reliably improve
        # on a prior attempt (it can swing either way, including sharply
        # worse: one production's attempt 2 came back as 282 shots at 13%
        # detailed after attempt 1 had already reached a healthy ratio with
        # no faults), and `best = max(attempts, key=_rank)` below already
        # keeps whichever attempt was actually best regardless of when the
        # loop stops - so a stalled run loses nothing by stopping early.
        STALL_LIMIT = 2
        best_rank_run = None
        stall = 0
        for attempt in range(1, attempts_allowed + 1):
            prior = attempts[-1] if attempts else None
            # Once a plan has zero STRUCTURAL faults and only falls short on
            # prompt detail, patch just the flagged prompts instead of
            # re-planning from scratch. A full re-plan was found to swing
            # non-monotonically on the very same shotlist (e.g. 85% detailed
            # on one attempt, 63% on the next) because the model does not
            # reliably honor "keep everything that already passed" - a patch
            # call can only touch the assets it is given, so the rest of the
            # plan is safe by construction.
            patch_mode = bool(prior and not prior["faults"]
                              and prior["ratio"] < min_align
                              and prior.get("weak"))
            if patch_mode:
                patch_text = studio.llm_generate(
                    cfg, studio.shotlist_patch_prompt(
                        prior["weak"], style_guide=style_guide,
                        bible=bible_text),
                    provider=provider)
                patches = studio.parse_shotlist_patch(patch_text)
                if patches:
                    data = studio.apply_shotlist_patch(prior["data"], patches)
                    # batch_sheet.txt is a human-readable export, not read by
                    # rendering (which reads shotlist.json) - carrying the
                    # prior text forward means it can go stale after a patch,
                    # which is cosmetic only.
                    sheet = prior["sheet"]
                    _log_line(f"shotlist attempt {attempt}: patched "
                              f"{len(patches)}/{len(prior['weak'])} flagged "
                              f"prompt(s), kept the rest of the plan as-is")
                else:
                    _log_line(f"shotlist attempt {attempt}: patch reply had "
                              f"no usable rewrites - falling back to a full "
                              f"re-plan")
                    patch_mode = False
            if not patch_mode:
                prompt = studio.shotlist_prompt(
                    brief, narration, style_guide,
                    extra_direction=db.stage_extra(prod, "shots"),
                    bible=bible_text, feedback=feedback,
                    supplied_refs=studio.find_supplied_refs(pdir),
                    pacing_note=pacing_note, allow_refs=allow_refs)
                text = studio.llm_generate(
                    cfg, prompt, provider=provider,
                    max_tokens=studio.shotlist_max_tokens(
                        total_s, len(cues), max_hold))
                data = sheet = None
                for cont in range(studio.SHOTLIST_CONTINUE_ROUNDS + 1):
                    try:
                        data, sheet = studio.parse_shotlist_output(text)
                        break
                    except RuntimeError as exc:
                        msg = str(exc)
                        if ("incomplete" in msg
                                and cont < studio.SHOTLIST_CONTINUE_ROUNDS):
                            # the reply was cut off mid-JSON: ask for the rest
                            # and keep appending (brief's Section 11)
                            _log_line(f"shotlist attempt {attempt}: output cut "
                                      f"off - continuing ({cont + 1}/"
                                      f"{studio.SHOTLIST_CONTINUE_ROUNDS})")
                            text += studio.llm_generate(
                                cfg, studio.continuation_prompt(prompt, text),
                                provider=provider)
                            continue
                        # malformed or prose: spend this attempt and re-plan
                        _log_line(f"shotlist attempt {attempt}: reply was not "
                                  f"valid JSON ({msg}) - retrying")
                        feedback = ("Your previous reply was not valid JSON "
                                    f"({msg[:140]}). Output ONLY the two "
                                    "documents, starting with the shotlist "
                                    "JSON, and close every bracket cleanly.")
                        break
                if data is None:
                    continue
                # A plan can close its JSON cleanly and still stop short of
                # the final cue (e.g. 73 shots covering cues 1-221 of 484, no
                # error, nothing planned for the rest) - every re-plan from
                # scratch tends to truncate at roughly the same point, so ask
                # for just the missing tail instead of burning another whole
                # attempt on it.
                for tail_cont in range(studio.SHOTLIST_CONTINUE_ROUNDS):
                    tail_gap = studio.shotlist_tail_gap(data, len(cues))
                    if tail_gap is None:
                        break
                    _log_line(f"shotlist attempt {attempt}: stopped cleanly "
                              f"at cue {tail_gap}/{len(cues)} - continuing "
                              f"tail ({tail_cont + 1}/"
                              f"{studio.SHOTLIST_CONTINUE_ROUNDS})")
                    addition_text = studio.llm_generate(
                        cfg, studio.shotlist_tail_continuation_prompt(
                            prompt, tail_gap, len(cues)),
                        provider=provider)
                    try:
                        addition, _ = studio.parse_shotlist_output(addition_text)
                    except RuntimeError as exc:
                        _log_line(f"shotlist attempt {attempt}: tail "
                                  f"continuation reply was not usable "
                                  f"({exc}) - keeping the plan as-is; review "
                                  "will flag what's missing")
                        break
                    data = studio.merge_shotlist_continuation(data, addition)
            if patch_mode:
                # Only the patched prompts changed - carry forward every
                # other shot's verdict from the review that flagged them
                # instead of re-judging the whole plan again (see
                # review_shotlist_patch's docstring).
                review = studio.review_shotlist_patch(
                    cfg, data, cues, judge,
                    prior_verdicts=(prior.get("verdicts") or {}) if prior else {},
                    patched_assets=set(patches),
                    max_hold_seconds=max_hold, style_guide=style_guide,
                    temperature=eff["shotlist_judge_temperature"],
                    profile=profile)
            else:
                review = studio.review_shotlist(
                    cfg, data, cues, judge, max_hold_seconds=max_hold,
                    style_guide=style_guide,
                    temperature=eff["shotlist_judge_temperature"],
                    profile=profile)
            if review["total"] and review["unreviewed"] == review["total"]:
                # The judge never actually ran on a single shot - unlike a
                # low completeness ratio (real information: some prompts are
                # genuinely thin), this is NO information at all. Faults are
                # computed independently of the judge, so this would
                # otherwise look exactly like a clean pass ("0 faults") and
                # sail through to costly rendering with a shotlist nobody
                # ever checked. Stop and surface it instead of guessing.
                raise _Paused(
                    f"the shotlist judge ('{judge}') could not review any "
                    f"of the {review['total']} shots - {review['error']} - "
                    "check the judge provider/API, then Resume to retry")
            if not allow_refs and studio.shotlist_uses_refs(data):
                review["faults"] = list(review["faults"]) + [
                    "references are DISABLED for this channel: remove the "
                    "\"refs\" registry, every \"refPrompts\" entry and every "
                    "per-image \"refs\" array - describe each character, "
                    "location and object inline in the image prompt instead"]
            passed = (not review["faults"] and review["ratio"] >= min_align)
            attempts.append({**review, "attempt": attempt, "data": data,
                             "sheet": sheet})
            _log_line(f"shotlist attempt {attempt}: "
                      f"{review['matched']}/{review['total']} prompts detailed "
                      f"enough ({review['ratio']:.0%}), "
                      f"{len(review['faults'])} fault(s)"
                      + (f", {len(review['warnings'])} pacing warning(s)"
                         if review.get("warnings") else "")
                      + (f", {review['unreviewed']} shot(s) could not be "
                         f"judged ({review['error']})"
                         if review.get("unreviewed") else ""))
            if passed:
                break
            rank_now = _rank(attempts[-1])
            if best_rank_run is None or rank_now > best_rank_run:
                best_rank_run = rank_now
                stall = 0
            else:
                stall += 1
                if stall >= STALL_LIMIT and attempt < attempts_allowed:
                    _log_line(
                        f"shotlist: stopping early after attempt {attempt} - "
                        f"{stall} attempt(s) in a row did not beat this run's "
                        f"best so far ({best_rank_run[1]:.0%} detailed"
                        + (", fault-free" if best_rank_run[0] else "")
                        + f"); {attempts_allowed - attempt} attempt(s) skipped")
                    break
            feedback = _shotlist_feedback(review, min_align)

        if not attempts:
            raise RuntimeError("the planner returned no valid shotlist JSON in "
                               f"{attempts_allowed} attempt(s)")
        best = max(attempts, key=_rank)
        # Never let a worse resume silently overwrite a better result from an
        # earlier one: each stage-run only gets `attempts_allowed` tries and
        # used to start from scratch every time, so a good pass (e.g. 85%
        # detailed) could be discarded by a later, worse resume (e.g. 63%)
        # with no way back - this production hit exactly that.
        prior_best = _load_best_ever(pdir)
        kept_prior = _rank(prior_best) > _rank(best)
        if kept_prior:
            _log_line(f"shots: keeping the best-ever result from an earlier "
                      f"run ({prior_best['ratio']:.0%} detailed, "
                      f"{len(prior_best['faults'])} fault(s)) over this "
                      f"run's best ({best['ratio']:.0%}, "
                      f"{len(best['faults'])} fault(s))")
            best = prior_best
        elif _rank(best) > _rank(prior_best):
            _save_best_ever(pdir, best)
        data, sheet = best["data"], best["sheet"]
        passed = not best["faults"] and best["ratio"] >= min_align
        (pdir / "shotlist.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        if sheet:
            (pdir / "batch_sheet.txt").write_text(
                sheet + "\n", encoding="utf-8")
        review_dir = pdir / "versions" / "shotlist"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / "review.json").write_text(
            json.dumps([{k: v for k, v in a.items()
                         if k not in ("data", "sheet")} for a in attempts],
                       indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        # no llm_provider persistence here either - see the script stage
        pacing = best.get("warnings") or []
        attempts_desc = ("kept from an earlier run" if kept_prior
                         else f"best of {len(attempts)} attempt(s)")
        criteria_line = _shots_criteria_line(
            best["faults"], best["matched"], best["total"], best["ratio"],
            min_align)
        motion_line = _motion_breakdown_line(data.get("shots", []))
        detail = (f"{len(data.get('images', []))} image(s) in "
                  f"{len(data.get('shots', []))} shot(s) via manifest brief, "
                  f"{attempts_desc}, judged by {judge}, "
                  f"took {format_duration(time.monotonic() - t0)}"
                  f"\n{criteria_line}")
        if motion_line:
            detail += f"\n{motion_line}"
        if best.get("unreviewed"):
            detail += (f" | {best['unreviewed']} shot(s) could not be judged "
                       f"({best.get('error')})")
        if pacing:
            detail += " | " + "; ".join(pacing)
        if passed:
            db.add_step(conn, pid, "shots", "auto", detail=detail)
            if pacing:
                db.update_production(conn, pid, warning="; ".join(pacing)[:900])
        else:
            # same reasoning as the script gate: we keep the best shotlist and
            # carry on to images, so it IS this stage's artifact. Marking it
            # failed would re-plan it on a resume and orphan the rendered
            # images. The warning flags it instead.
            warning = _shotlist_feedback(best, min_align)[:900]
            if pacing:
                warning = (warning + " | " + "; ".join(pacing))[:900]
            db.update_production(conn, pid, warning=warning)
            db.add_step(conn, pid, "shots", "auto",
                        detail=detail + " | " + warning)
    finally:
        conn.close()


def _shotlist_feedback(review: dict, min_align: float) -> str:
    """The correction list fed into the next shotlist attempt. A re-plan is a
    FRESH call, so it carries the rules and the fault list - never the previous
    plan (the model then asks for the exact prompts it was told to preserve)."""
    parts = []
    if review["faults"]:
        parts.append("FAULTS (must be zero):\n"
                     + "\n".join(f"- {f}" for f in review["faults"]))
    if review["ratio"] < min_align:
        parts.append(
            f"Only {review['matched']}/{review['total']} shots "
            f"({review['ratio']:.0%}) have a prompt detailed enough to render "
            f"their scene; {min_align:.0%} is required. Rendering is slow, so "
            f"a prompt that under-specifies its beat means the image has to be "
            f"regenerated by hand. Rewrite the prompts below so each states "
            f"every element its narration requires - who is in frame, what "
            f"they are doing, where they are, the objects or props involved, "
            f"and the specific information the cue conveys:")
        weak = review["weak"]
        for m in weak:
            missing = ("; missing: " + ", ".join(m["missing"])
                       if m.get("missing") else "")
            parts.append(f"- {m['asset']} ({m['verdict']}){missing}"
                         + (f" - {m['reason']}" if m.get("reason") else "")
                         + (f" | cue says: {m['narration']}"
                            if m.get("narration") else ""))
        if len(weak) >= 30:
            parts.append("...and any other prompt that under-specifies its "
                         "beat - apply the same rule.")
    if not parts:
        return ""
    parts.append("Keep everything that already passed; change only what is "
                 "listed. Return the complete shotlist again.")
    return "\n".join(parts)


def _run_refs(cfg, pid: int, log=None, cancel=None) -> None:
    """Generate the reference images the shotlist needs but has no file for.

    Optional per channel. A ref you SUPPLY is used as-is; a ref with no usable
    file is generated from its own prompt and placed in the production's refs\\
    folder, with the shotlist registry path filled in - so the images stage can
    upload it to the Flow project under the ref's own name (FlowBatch) or
    resolve it as a local file (the Renderly driver).

    Engine-aware: a "flowbatch" channel generates refs via FlowBatch; every
    other channel (Renderly, including Flow-mode) generates them via the SAME
    Renderly image engine the images stage itself uses - refs never fall back
    to FlowBatch just because it happens to be configured."""
    t0 = time.monotonic()
    log = log or (lambda m: None)
    pdir = studio.prepare_project_folder(cfg, pid)
    eff = _effective(cfg, pid)

    def _step(detail, status="done"):
        conn = _connect(cfg)
        try:
            db.add_step(conn, pid, "refs", "auto", detail=detail,
                        status=status)
        finally:
            conn.close()

    if not eff["generate_references"]:
        _step("skipped - reference generation is off for this channel")
        return
    used = studio.shotlist_refs(pdir)
    if not used:
        _step("the shotlist declares no references")
        return
    provided = [n for n, r in used.items() if r["provided"]]
    stranded = [n for n, r in used.items()
                if not r["provided"] and not r["prompt"]]
    todo = studio.refs_to_generate(pdir)
    if not todo:
        detail = f"{len(provided)} supplied reference(s), nothing to generate"
        if stranded:
            detail += (f" | {len(stranded)} have no file AND no prompt and "
                       f"cannot be attached: {', '.join(stranded[:6])}")
        _step(detail)
        return
    if len(todo) > studio.REFS_ON_THE_FLY_CAP:
        # The shotlist gate should have caught this; the cap is the guard so a
        # bad plan can never burn unbounded generations.
        log(f"[auto-run] refs: capping generation at "
            f"{studio.REFS_ON_THE_FLY_CAP} of {len(todo)} planned reference "
            f"image(s)")
        todo = dict(list(todo.items())[:studio.REFS_ON_THE_FLY_CAP])
    log(f"[auto-run] refs: generating {len(todo)} reference image(s): "
        f"{', '.join(list(todo)[:6])}")
    # One engine per channel: a Renderly channel must never fall back to
    # FlowBatch (and its own Flow login) just to make reference images - it
    # renders refs through the same Renderly path _run_images() uses for the
    # production's real shots. Mirrors the engine branch in _run_images() and
    # the channel/upscale resolution _stage_params() does for the images
    # stage.
    if eff["engine"] == "flowbatch":
        result = studio.run_flowbatch_refs(cfg, pdir, pid, todo, log=log,
                                           cancel=cancel,
                                           upscale=_upscale_for(eff))
    elif _default_render_mode(cfg, _get_prod(cfg, pid)) == "flow":
        # renderly + flow: refs come from the Flow Driver too, so a flow
        # channel never touches the :8022 API - one engine does everything.
        result = studio.run_renderly_refs(cfg, pdir, pid, todo,
                                          upscale=_upscale_for(eff),
                                          log=log, cancel=cancel, flow=True)
    else:
        renderly_channel = studio.resolve_renderly_channel(
            cfg, eff["own_channel"], create=True)
        result = studio.run_renderly_refs(cfg, pdir, pid, todo,
                                          channel=renderly_channel,
                                          upscale=_upscale_for(eff),
                                          log=log, cancel=cancel)
    made = result["generated"]
    failed = result["missing"] + stranded
    detail = (f"{len(made)} reference image(s) generated "
              f"({', '.join(made[:6])})" if made else "no references generated")
    if failed:
        detail += (f" | {len(failed)} still missing: "
                   f"{', '.join(failed[:6])}")
    detail += f", took {format_duration(time.monotonic() - t0)}"
    _step(detail)


_RESUMABLE_IMAGE_ERRORS = ("were not produced", "is refusing this session")


def _should_resume_images(exc: Exception, round_no: int,
                          resume_wait_seconds: int | None = None,
                          still_busy_wait_seconds: int | None = None) -> bool:
    """True when a failed images round should pause and be resumed: ONLY
    Flow's 'N of M image(s) were not produced' wave (its 'still busy'
    timeout) or its 'is refusing this session' hard stop (studio.py's
    configurable consecutive-failures guard, usually a reCAPTCHA score dip),
    and only while rounds remain. Any other error is a real failure.

    `resume_wait_seconds` / `still_busy_wait_seconds` are the production's
    images_resume_wait_minutes / images_still_busy_wait_minutes settings (in
    seconds); a configured 0 disables auto-resume for that case - the operator
    wants to look at it before more credits are spent, so it stops like a plain
    failure and waits for a manual Resume instead."""
    text = str(exc)
    if "is refusing this session" in text:
        if resume_wait_seconds == 0:
            return False
    elif still_busy_wait_seconds == 0:
        return False
    return (round_no < IMAGE_RESUME_ROUNDS
            and any(marker in text for marker in _RESUMABLE_IMAGE_ERRORS))


def _resume_pause_seconds(exc: Exception,
                          resume_wait_seconds: int | None = None,
                          still_busy_wait_seconds: int | None = None) -> int:
    """How long to wait before resuming - the refusal wave needs longer than
    a plain 'still busy' timeout to actually clear.

    `resume_wait_seconds` / `still_busy_wait_seconds` are the production's
    images_resume_wait_minutes / images_still_busy_wait_minutes settings (in
    seconds); each replaces the matching fixed default when given."""
    if "is refusing this session" in str(exc):
        if resume_wait_seconds is not None:
            return resume_wait_seconds
        return FLOW_REFUSAL_PAUSE_SECONDS
    if still_busy_wait_seconds is not None:
        return still_busy_wait_seconds
    return IMAGE_RESUME_PAUSE_SECONDS


def _run_images(cfg, pid: int, mode: str | None = None,
                engine: str | None = None,
                flow_channel: str = "whisperradar",
                flow_project: str = "", flow_upscale: int | None = None,
                flow_master: str = "", renderly_channel=None,
                flow_project_url: str | None = None,
                flow_local_upscale: bool = False,
                log=None, cancel=None) -> None:
    t0 = time.monotonic()
    pdir = studio.prepare_project_folder(cfg, pid)
    if not (pdir / "shotlist.json").exists():
        raise _Paused("shotlist.json is missing - plan or save a shotlist, "
                      "then Resume")
    eff = _effective(cfg, pid)
    if engine is None:
        engine = eff["engine"]
    if mode is None:
        mode = _default_render_mode(cfg, _get_prod(cfg, pid))
    if mode not in ("api", "flow"):
        mode = "api"
    if log is None:
        log = lambda m: None
    # Flow native: both engines download the stills at Flow's own size and,
    # when a level is picked next to Render resolution, ONE local Real-ESRGAN
    # pass upscales them once the batch has finished (below) - so the download
    # phase is never stalled by upscaling, the Renderly backend upscaler is
    # never involved, and the pass can be re-run from the images stage.
    post_tier = post_download_tier(eff)
    upscale_after_download = post_tier != "off"
    if is_flow_native(eff) and flow_upscale is None:
        flow_upscale = 0       # never fall back to the engine's own tier
    if upscale_after_download:
        flow_upscale = 0
        flow_local_upscale = True   # the Flow Driver never imports to Renderly
    conn = _connect(cfg)
    try:
        db.update_production(conn, pid, render_mode=mode)
        managed = bool(settings.load(conn).get("services_managed"))
    finally:
        conn.close()
    # bring up only what this engine/mode needs; a service that already
    # answers is the user's and is left alone
    # the consecutive-failure stop (studio.py) and the wait before an
    # auto-resume after it are both the images_max_consecutive_failures /
    # images_resume_wait_minutes settings, shared by manual render and
    # auto-run and by both engines
    (_, _, _, refusal_wait_seconds,
     still_busy_wait_seconds) = studio.image_batch_limits(cfg, pid)
    try:
        services.MANAGER.ensure(cfg, services.services_for(engine, mode),
                                log_fn=log)
        for round_no in range(1, IMAGE_RESUME_ROUNDS + 1):
            try:
                if engine == "flowbatch":
                    # FlowBatch drives Flow itself and upscales on the way
                    # out, so the Renderly channel/project and the Flow Driver
                    # do not apply.
                    count = studio.run_imagegen_flowbatch(
                        cfg, pdir, pid, upscale=flow_upscale, log=log,
                        cancel=cancel, project_url=flow_project_url)
                    source = "FlowBatch"
                elif mode == "flow":
                    # per-image refs come from the shotlist's own refs registry,
                    # which flow.js resolves itself; the production refs\ folder
                    # is just the library it resolves names against - attaching
                    # every file globally would blow past Flow's 3-ingredient
                    # limit
                    count = studio.run_imagegen_flow(
                        cfg, pdir, channel=flow_channel, project=flow_project,
                        upscale=flow_upscale, master=flow_master, log=log,
                        cancel=cancel, pid=pid, local_upscale=flow_local_upscale)
                    source = "Flow Driver (Google Flow)"
                else:
                    # Renderly engine, API mode: only PL/PR are worth a paid
                    # API call (they need a canvas wider than 16:9, and the
                    # API is the only path that can request 21:9). Everything
                    # else in the shotlist - ST/ZI/ZO/PU/PD/PV - gets the same
                    # or a strictly worse aspect from the API than it already
                    # gets for free through Renderly's own Flow Driver, so it
                    # is never sent to the API at all; one engine ("renderly")
                    # still does the whole batch, it just always splits the
                    # work between its two free/paid halves rather than
                    # mixing only on quota failure.
                    api_count = 0
                    quota_hit = False
                    try:
                        api_count = studio.run_imagegen(
                            cfg, pdir, channel=renderly_channel,
                            upscale=flow_upscale, motion_filter=("PL", "PR"))
                    except studio.RenderlyQuotaExhausted as exc:
                        # Even the PL/PR slice hit a quota/billing wall.
                        # api_count carries whatever it got through before
                        # stopping; the Flow Driver pass below still picks up
                        # the rest of PL/PR (as 16:9 push-ins, same tradeoff
                        # as always) plus every other motion code.
                        api_count = exc.generated
                        quota_hit = True
                        log(f"[auto-run] images: {exc} - the remaining PL/PR "
                            "shots will fall through to the Flow Driver too")
                    # Whatever the API pass above left untouched - the rest of
                    # PL/PR on a quota failure, and ST/ZI/ZO/PU/PD/PV always -
                    # goes through the Flow Driver. It reads shotlist.json
                    # itself and skips any file already on disk, so this is
                    # safe to call even when the API pass finished everything
                    # it was asked for (it just finds nothing left to do).
                    flow_count = studio.run_imagegen_flow(
                        cfg, pdir, channel=flow_channel, project=flow_project,
                        upscale=flow_upscale, master=flow_master, log=log,
                        cancel=cancel, pid=pid, local_upscale=flow_local_upscale)
                    count = api_count + flow_count
                    if quota_hit:
                        source = "Renderly API (PL/PR, quota-limited) + Flow Driver (rest)"
                    elif api_count:
                        source = "Renderly API (PL/PR) + Flow Driver (rest)"
                    else:
                        source = "Flow Driver"
                break
            except RuntimeError as exc:
                # Flow gave up on some cards ("still busy"). Pause, then resume:
                # the driver skips what is already on disk, so the next round
                # only attempts the gaps.
                if not _should_resume_images(exc, round_no,
                                             refusal_wait_seconds,
                                             still_busy_wait_seconds):
                    raise
                pause = _resume_pause_seconds(exc, refusal_wait_seconds,
                                              still_busy_wait_seconds)
                log(f"[auto-run] images: {exc}")
                log(f"[auto-run] images: pausing {pause // 60} minutes, then "
                    f"resuming the missing card(s) - round {round_no} of "
                    f"{IMAGE_RESUME_ROUNDS - 1}")
                time.sleep(pause)
    finally:
        # stop what we started, if the user opted in; never a service that was
        # already running
        services.MANAGER.release(cfg, managed, log_fn=log)
    upscale_note = ""
    if upscale_after_download:
        try:
            up = studio.upscale_images_locally(cfg, pdir, tier=post_tier,
                                               log=log, cancel=cancel)
            upscale_note = (f"; local upscale to {up['tier']}: "
                            f"{up.get('upscaled', 0)} upscaled, "
                            f"{up.get('skipped', 0)} already at tier"
                            + (f", {up['lanczos']} via CPU Lanczos fallback"
                               if up.get("lanczos") else ""))
        except Exception as exc:  # noqa: BLE001 - never lose the downloads
            log(f"[auto-run] images: local upscale failed - {exc}")
            upscale_note = f"; local upscale FAILED ({exc})"
    conn = _connect(cfg)
    try:
        db.add_step(conn, pid, "images", "auto",
                    detail=f"{count} image(s) via {source}, "
                           f"took {format_duration(time.monotonic() - t0)}"
                           + upscale_note)
    finally:
        conn.close()


def recover_images(cfg, pid: int, log=None, cancel=None) -> dict:
    """Manual "Recover from Flow gallery": adopt the images a stopped batch
    already generated into the Flow project's gallery instead of paying to
    regenerate them.

    Never automatic and never part of the pipeline (_RUNNERS) - only the
    IMAGES stage button triggers it, and it NEVER generates anything.
    Dispatch by the SAME engine/mode the images stage would use
    (_stage_params' resolution): flowbatch drives the FlowBatch CLI's
    recover command; renderly+flow drives the Flow Driver's /api/recover;
    renderly+api is refused (the Renderly API stores its results itself,
    there is no gallery to recover from). A partial adoption is honest in
    the step: done only when nothing is left missing. Returns
    {recovered, still_missing} as shotlist file names."""
    t0 = time.monotonic()
    log = log or (lambda m: None)
    pdir = studio.prepare_project_folder(cfg, pid)
    if not (pdir / "shotlist.json").exists():
        raise _Paused("shotlist.json is missing - plan or save a shotlist "
                      "first")
    eff = _effective(cfg, pid)
    engine = eff["engine"]
    mode = _default_render_mode(cfg, _get_prod(cfg, pid))
    if mode not in ("api", "flow"):
        mode = "api"
    if engine == "renderly" and mode == "api":
        raise RuntimeError("The Renderly API has no gallery to recover from "
                           "- its results live in Renderly itself. Switch the "
                           "image source to Flow, or the engine to "
                           "FlowBatch.")
    conn = _connect(cfg)
    try:
        managed = bool(settings.load(conn).get("services_managed"))
    finally:
        conn.close()
    # Flow native: recovery adopts the gallery's masters and the same single
    # local pass runs at the end, exactly like the images stage - so the two
    # download paths behave identically.
    post_tier = post_download_tier(eff)
    download_then_upscale = post_tier != "off"
    try:
        services.MANAGER.ensure(cfg, services.services_for(engine, mode),
                                log_fn=log)
        if engine == "flowbatch":
            result = studio.run_flowbatch_recover(
                cfg, pdir, pid,
                upscale=_upscale_for(eff), log=log, cancel=cancel)
            source = "FlowBatch"
        else:
            result = studio.run_flowdriver_recover(cfg, pdir, pid, log=log,
                                                   cancel=cancel)
            source = "Flow Driver"
    finally:
        services.MANAGER.release(cfg, managed, log_fn=log)
    upscale_note = ""
    if download_then_upscale:
        try:
            up = studio.upscale_images_locally(cfg, pdir, tier=post_tier,
                                               log=log, cancel=cancel)
            upscale_note = (f"; local upscale to {up['tier']}: "
                            f"{up.get('upscaled', 0)} upscaled, "
                            f"{up.get('skipped', 0)} already at tier"
                            + (f", {up['lanczos']} via CPU Lanczos fallback"
                               if up.get("lanczos") else ""))
        except Exception as exc:  # noqa: BLE001 - the adoption still stands
            log(f"[auto-run] images: local upscale failed - {exc}")
            upscale_note = f"; local upscale FAILED ({exc})"
    recovered = result["recovered"]
    still_missing = result["still_missing"]
    detail = (f"gallery recovery via {source}: {len(recovered)} image(s) "
              f"adopted"
              + (f" ({', '.join(recovered[:6])}"
                 + (f" +{len(recovered) - 6} more"
                    if len(recovered) > 6 else "") + ")"
                 if recovered else "")
              + f", {len(still_missing)} still missing"
              + f", took {format_duration(time.monotonic() - t0)}"
              + upscale_note)
    conn = _connect(cfg)
    try:
        db.add_step(conn, pid, "images", "manual", detail=detail,
                    status="done" if not still_missing else "failed")
    finally:
        conn.close()
    return result


def upscale_images(cfg, pid: int, log=None, cancel=None) -> dict:
    """Manual IMAGES-stage action: upscale the stills already in images\\
    in place with the LOCAL Real-ESRGAN engine - the same pass a Flow native
    production runs at the end of a batch.

    No Flow, no download, no re-render, and safe to re-run (files already at
    the tier are skipped). Records an images step, done only when the
    shotlist has no missing images, so a partial upscale can never make the
    stage look complete."""
    t0 = time.monotonic()
    pdir = studio.prepare_project_folder(cfg, pid)
    if not (pdir / "shotlist.json").exists():
        raise _Paused("shotlist.json is missing - plan or save a shotlist "
                      "first")
    eff = _effective(cfg, pid)
    tier = manual_upscale_tier(eff)
    if tier == "off":
        raise RuntimeError(
            "No upscale level is set - pick one under Render resolution: "
            "Flow native (My Channels or Settings), or set an upscale tier")
    result = studio.upscale_images_locally(cfg, pdir, tier=tier, log=log,
                                           cancel=cancel)
    try:
        missing = len(studio.shotlist_missing_images(pdir))
    except RuntimeError:
        missing = 0
    detail = (f"local upscale to {result['tier']} via {result['engine']}: "
              f"{result.get('upscaled', 0)} upscaled, "
              f"{result.get('skipped', 0)} already at tier, "
              f"{result.get('failed', 0)} failed"
              + (f", {missing} image(s) still missing" if missing else "")
              + f", took {format_duration(time.monotonic() - t0)}")
    conn = _connect(cfg)
    try:
        db.add_step(conn, pid, "images", "manual", detail=detail,
                    status="done" if not missing else "failed")
    finally:
        conn.close()
    return result


def _run_merge(cfg, pid: int, mode: str | None = None) -> None:
    """mode: 'hook' = studio.merge_command only (merge route),
    'cli' = ImgToVideo.Cli preview build + NLE export (video/render route),
    None = whatever is configured (auto-run)."""
    pdir = studio.prepare_project_folder(cfg, pid)
    audio = studio.find_audio(pdir)
    srt = studio.find_srt(pdir)
    images = studio.find_images(pdir)
    if not audio or not srt or not images:
        raise RuntimeError("Need audio, subtitles and images first")
    if mode is None:
        # unattended merge must never silently render a sanitized video
        # (the batch may have been stopped mid-way) - pause instead, on ANY
        # missing image: a sanitized gap can never be filled afterwards
        reason = _merge_pause_reason(cfg, pid)
        if reason:
            raise _Paused(reason)
    if mode == "hook":
        if not cfg.studio_merge_command:
            raise RuntimeError("No merge_command in config.yaml")
    use_hook = mode == "hook" or (mode is None and cfg.studio_merge_command)
    if use_hook:
        out = pdir / "final.mp4"
        studio.run_hook(cfg.studio_merge_command, {
            "images": pdir / "images", "audio": audio, "srt": srt,
            "out": out,
        }, timeout=7200)
        if not out.exists():
            raise RuntimeError("merge produced no video")
        detail = "final.mp4"
    else:
        # preview draft + NLE project for the global render target
        target = _effective(cfg, pid)["render_target"]
        result = studio.run_merge_render(cfg, pdir, target)
        detail = (f"preview + {studio.RENDER_TARGET_LABELS[target]} project")
    conn = _connect(cfg)
    try:
        db.add_step(conn, pid, "merge", "auto", detail=detail)
    finally:
        conn.close()


_RUNNERS = {
    "style": _run_style,
    "script": _run_script,
    "audio": _run_audio,
    "srt": _run_srt,
    "shots": _run_shots,
    "refs": _run_refs,
    "images": _run_images,
    "merge": _run_merge,
}


def run_stage(cfg, pid: int, stage: str, params: dict | None = None) -> str:
    """Execute one stage. Returns 'ok', 'paused:<reason>', 'stopped' or
    'failed:<error>' - failures are caught here so the pipeline runner can
    log them and stop cleanly."""
    runner = _RUNNERS.get(stage)
    if runner is None:
        return f"failed:unknown stage '{stage}'"
    try:
        runner(cfg, pid, **(params or {}))
        return "ok"
    except _Paused as exc:
        return f"paused:{exc}"
    except studio.BatchCancelled:
        return "stopped"
    except Exception as exc:
        return f"failed:{exc}"


def run_stage_and_advance(cfg, pid: int, stage: str,
                          params: dict | None = None) -> str:
    """Like run_stage(), but also moves prod.stage past `stage` on success.

    run_stage() alone only ever gets called two ways: from HERE (nothing
    below advanced it) or from the auto-run driver's loop, which calls
    _advance() itself after a successful stage. Every single-stage HTTP
    route (the Studio page's Generate/Save buttons) called run_stage()
    directly and never advanced - so prod.stage stayed wherever it was left
    (often the first stage, if it was ever completed by hand) no matter how
    many later stages were generated by hand afterward, and the Studio page
    (stage = request.args.get("stage") or prod["stage"]) kept reopening on
    that stale stage every time the production was revisited. Confirmed on
    production 19: script completed successfully (twice) via the Generate
    Script button while prod.stage remained "style"."""
    result = run_stage(cfg, pid, stage, params)
    if result == "ok":
        _advance(cfg, pid, stage)
    return result


def raise_result(result: str) -> None:
    """Single-stage HTTP routes surface pause/fail results as job errors,
    exactly like the closures did before the auto-run refactor."""
    if result.startswith(("paused:", "failed:")):
        raise RuntimeError(result.split(":", 1)[1])


# ------------------------------------------------------- plan / action ---

def stage_action(cfg, pid: int, stage: str) -> dict:
    """What auto-run would do for one stage right now:
    {stage, action: 'skip'|'run'|'pause', detail}."""
    pdir = studio.prod_dir(cfg, pid)
    if stage == "style":
        if studio.find_writing_style(pdir):
            return {"stage": stage, "action": "skip",
                    "detail": "writing style guide already exists"}
        if not _source_transcript_text(cfg, pid):
            return {"stage": stage, "action": "pause",
                    "detail": "no source transcript - write the style "
                              "guide manually, then Resume"}
        provider = _default_provider(cfg, pid)
        return {"stage": stage, "action": "run",
                "detail": f"style guide via LLM "
                          f"({studio.llm_label(cfg, provider)})"}
    if stage == "script":
        if studio.find_script(pdir):
            return {"stage": stage, "action": "skip",
                    "detail": "script.md already exists (manual scripts "
                              "count as done)"}
        provider = _default_provider(cfg, pid)
        return {"stage": stage, "action": "run",
                "detail": f"script via LLM ({studio.llm_label(cfg, provider)})"}
    if stage == "audio":
        if studio.find_audio(pdir):
            return {"stage": stage, "action": "skip",
                    "detail": "audio file already exists"}
        if cfg.studio_tts_command:
            eff = _effective(cfg, pid)
            voice = eff["voice"]
            source = ("production" if _get_prod(cfg, pid) is not None
                      and settings.row_get(_get_prod(cfg, pid), "voice")
                      else (eff["own_channel_name"] or "global"))
            return {"stage": stage, "action": "run",
                    "detail": "narration via the configured TTS hook"
                              + (f" (voice {voice} from {source})" if voice
                                 else " (default voice)")}
        return {"stage": stage, "action": "pause",
                "detail": "no audio and no TTS hook configured - upload "
                          "audio, then Resume"}
    if stage == "srt":
        if studio.find_srt(pdir):
            return {"stage": stage, "action": "skip",
                    "detail": "subtitles already exist"}
        return {"stage": stage, "action": "run",
                "detail": f"Whisper alignment ({cfg.whisper_model})"}
    if stage == "shots":
        if (pdir / "shotlist.json").exists():
            return {"stage": stage, "action": "skip",
                    "detail": "shotlist already exists"}
        if not studio.find_bible(pdir):
            # the channel may have a bible TEXT that was filled in after the
            # production was created - seed it before declaring a pause
            prod = _get_prod(cfg, pid)
            if prod is not None:
                conn = _connect(cfg)
                try:
                    studio.seed_production(cfg, conn, prod)
                finally:
                    conn.close()
            if not studio.find_bible(pdir):
                return {"stage": stage, "action": "pause",
                        "detail": "no character/reference bible - the "
                                  "planning brief requires one before "
                                  "planning - write it at the shots stage, "
                                  "then Resume"}
        provider = _default_provider(cfg, pid)
        return {"stage": stage, "action": "run",
                "detail": f"shotlist planned via LLM "
                          f"({studio.llm_label(cfg, provider)})"}
    if stage == "refs":
        eff = _effective(cfg, pid)
        if not eff["generate_references"]:
            return {"stage": stage, "action": "skip",
                    "detail": "reference generation is off for this channel"}
        used = studio.shotlist_refs(pdir)
        if not used:
            return {"stage": stage, "action": "skip",
                    "detail": "the shotlist declares no references"}
        todo = studio.refs_to_generate(pdir)
        supplied = sum(1 for r in used.values() if r["provided"])
        if not todo:
            return {"stage": stage, "action": "skip",
                    "detail": f"all {len(used)} reference(s) are supplied"}
        return {"stage": stage, "action": "run",
                "detail": f"generate {len(todo)} reference image(s) "
                          f"({', '.join(list(todo)[:5])}); {supplied} supplied"}
    if stage == "images":
        if not (pdir / "shotlist.json").exists():
            return {"stage": stage, "action": "pause",
                    "detail": "shotlist.json is missing - plan or save a "
                              "shotlist, then Resume"}
        missing = _missing_images(pdir)
        if not missing:
            return {"stage": stage, "action": "skip",
                    "detail": "all shotlist images already rendered"}
        prod = _get_prod(cfg, pid)
        mode = _default_render_mode(cfg, prod)
        eff = _effective(cfg, pid)
        if eff["engine"] == "flowbatch":
            refs = (len([p for p in (pdir / "refs").glob("*") if p.is_file()])
                    if (pdir / "refs").exists() else 0)
            detail = (f"{len(missing)} missing image(s) via FlowBatch "
                      f"({_upscale_text(eff)}"
                      + (f", {refs} ref image(s)" if refs else "") + ")")
        elif mode == "flow":
            refs = (len([p for p in (pdir / "refs").glob("*") if p.is_file()])
                    if (pdir / "refs").exists() else 0)
            detail = (f"{len(missing)} missing image(s) via Flow Driver "
                      f"(channel {eff['renderly_channel_name']}, default "
                      f"project, {_upscale_text(eff)}"
                      + (f", {refs} ref image(s)" if refs else "") + ")")
        else:
            detail = (f"{len(missing)} missing image(s) via Renderly API "
                      f"(channel {eff['renderly_channel_name']}, "
                      f"{_upscale_text(eff)})")
        return {"stage": stage, "action": "run", "detail": detail}
    if stage == "merge":
        if studio.merge_done(pdir):
            return {"stage": stage, "action": "skip",
                    "detail": "preview/NLE export already exists"}
        if cfg.studio_merge_command:
            return {"stage": stage, "action": "run",
                    "detail": "final render via the configured merge hook "
                              "- can take a long time"}
        target = _effective(cfg, pid)["render_target"]
        return {"stage": stage, "action": "run",
                "detail": f"preview build + "
                          f"{studio.RENDER_TARGET_LABELS[target]} export via "
                          f"ImgToVideo - can take a long time"}
    return {"stage": stage, "action": "pause",
            "detail": f"unknown stage '{stage}'"}


def build_plan(cfg, pid: int) -> list[dict]:
    """Stages auto-run would execute from the current state, ending at the
    first pause. review is never included - approval stays manual."""
    plan = []
    for stage in RUN_STAGES:
        entry = stage_action(cfg, pid, stage)
        plan.append(entry)
        if entry["action"] == "pause":
            break
    return plan


# --------------------------------------------------------- the runner ---

def _stage_params(cfg, pid: int, stage: str, log, cancel=None,
                  provider: str | None = None) -> dict:
    """Auto-run parameters per stage: the run's chosen LLM provider (the
    override a run was started with, else that stage's saved pick, else the
    Default LLM) for LLM stages, the saved render mode for images."""
    if stage in ("style", "script", "shots"):
        return {"provider": _stage_provider(cfg, pid, stage, override=provider)}
    if stage == "images":
        eff = _effective(cfg, pid)
        mode = _default_render_mode(cfg, _get_prod(cfg, pid))
        params = {"mode": mode, "engine": eff["engine"], "flow_project": "",
                  "flow_upscale": _upscale_for(eff), "log": log, "cancel": cancel}
        if eff["engine"] == "flowbatch":
            pass  # drives Flow itself; no Renderly channel/project involved
        elif mode == "flow":
            # Renderly engine + flow mode (Google Flow via the Flow Driver):
            # never import/upload the results into a Renderly channel - so
            # there is no channel to auto-create here either (that earlier
            # fix is superseded; nothing is ever sent for Flow-mode autorun).
            # Renderly's own upscale needs an imported generation's id, which
            # is exactly the round-trip being avoided, so instead the Flow
            # Driver is told to upscale the raw Flow output itself by calling
            # Renderly's own backend upscaler module directly (Real-ESRGAN,
            # no import, no HTTP round-trip) - see extension-v2's
            # --local-upscale flag, wired through
            # run_imagegen_flow(local_upscale=True). This is the manual
            # images-stage UI's own choice to make, not autorun's - it keeps
            # using a real Renderly channel by default.
            params["flow_channel"] = ""
            params["flow_local_upscale"] = True
            # flow_upscale (set above from eff["upscale"]) still selects the
            # tier for that local engine.
        else:
            params["renderly_channel"] = studio.resolve_renderly_channel(
                cfg, eff["own_channel"], create=True)
            # The split's Flow Driver half imports into the SAME Renderly
            # channel as the API half so it also upscales through Renderly. The
            # driver matches channels by NAME, not id, so the default
            # "whisperradar" would fail on any production whose channel is
            # named after the production.
            params["flow_channel"] = studio._own_channel_name(eff["own_channel"])
        return params
    return {}


def run_pipeline(cfg, pid: int, job=None, log=None,
                 stop_before: str | None = None,
                 provider: str | None = None) -> str:
    """Run every remaining stage in order, skipping the ones that are
    already done and pausing when manual input is missing.

    Each stage's action is re-evaluated when it is reached, so stages that
    become runnable mid-run (e.g. shots created the shotlist) execute
    instead of pausing per an outdated snapshot. `provider` overrides the
    saved LLM for every text stage of this run (style, script, shots).

    Returns 'ok', 'stopped', 'paused:<reason>' or 'failed:<error>'.
    Never touches the review stage: the production pointer ends at review.
    """
    if log is None:
        log = lambda m: None
    snapshot = build_plan(cfg, pid)
    if job is not None:
        job.pipeline = True
        job.autorun_plan = snapshot
    total = len(RUN_STAGES)
    ran = 0
    cancel_check = lambda: job is not None and bool(job.cancel)
    for i, stage in enumerate(RUN_STAGES, 1):
        if stop_before and stage == stop_before:
            # a deliberate halt (e.g. "plan the script and shotlist, but do
            # not spend image credits yet") - not a failure, so the run is
            # resumable and no problem notification is sent
            log(f"[auto-run] stopping before stage {i}/{total}: {stage} "
                f"(requested)")
            _set_stage(cfg, pid, stage)
            if job is not None:
                job.resume = True
            return "stopped"
        if job is not None and job.cancel:
            log(f"[auto-run] stopped by user before stage {i}/{total}: "
                f"{stage}")
            if job is not None:
                job.resume = True
            return "stopped"
        entry = stage_action(cfg, pid, stage)
        action, detail = entry["action"], entry["detail"]
        if action == "pause":
            log(f"[auto-run] paused at stage {i}/{total}: {stage} - "
                f"{detail}")
            _set_stage(cfg, pid, stage)
            if job is not None:
                job.pause_reason = detail
                job.resume = True
            return f"paused:{detail}"
        if action == "skip":
            log(f"[auto-run] stage {i}/{total}: {stage} - already done, "
                f"skipping")
            _advance(cfg, pid, stage)
            continue
        log(f"[auto-run] stage {i}/{total}: {stage} - started ({detail})")
        if job is not None:
            job.stage = stage
        result = run_stage(cfg, pid, stage,
                           params=_stage_params(cfg, pid, stage, log,
                                                cancel=cancel_check,
                                                provider=provider))
        if result == "stopped":
            log(f"[auto-run] stopped by user during stage {i}/{total}: "
                f"{stage} - rendered images are kept, re-run to fill the gaps")
            if job is not None:
                job.resume = True
            return "stopped"
        if result.startswith("paused:"):
            reason = result.split(":", 1)[1]
            log(f"[auto-run] paused at stage {i}/{total}: {stage} - "
                f"{reason}")
            _set_stage(cfg, pid, stage)
            if job is not None:
                job.pause_reason = reason
                job.resume = True
            return result
        if result.startswith("failed:"):
            error = result.split(":", 1)[1]
            log(f"[auto-run] stage {i}/{total}: {stage} - FAILED: {error}")
            # persist it: a CLI/auto-run failure used to leave no trace once
            # the process exited, so the reason vanished with the log
            conn = _connect(cfg)
            try:
                db.add_step(conn, pid, stage, "auto", status="failed",
                            detail=f"FAILED: {error}"[:500])
                db.update_production(conn, pid, status="failed")
            finally:
                conn.close()
            if job is not None:
                job.resume = True
            return result
        ran += 1
        _advance(cfg, pid, stage)
        # A stage that failed on an earlier attempt leaves the production
        # marked failed; once it succeeds here that is stale, and the
        # production would look broken after a fully successful run.
        conn = _connect(cfg)
        try:
            prod = db.get_production(conn, pid)
            if prod and prod["status"] == "failed":
                db.update_production(conn, pid, status="active")
        finally:
            conn.close()
        log(f"[auto-run] stage {i}/{total}: {stage} - done")
    summary = (f"Auto-run complete: {ran} stage(s) executed, "
               f"{total - ran} skipped - ready for review")
    log(f"[auto-run] pipeline finished - {summary}")
    if job is not None:
        job.summary = summary
        job.resume = False  # a completed run needs no Resume offer
    return "ok"
