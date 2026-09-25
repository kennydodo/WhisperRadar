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
import re
import shutil
import time

from pathlib import Path

from . import db, services, settings, studio, transcribe
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


def _default_provider(cfg, pid: int) -> str | None:
    """Which LLM a production's style/script/shots use: the production's own
    choice, then its channel's producer preference, then the global default.

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
    return cfg.studio_llm_default


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
        (pdir / "style.md").write_text(text + "\n", encoding="utf-8")
        db.add_step(conn, pid, "style", "auto",
                    detail=f"{provider}, source ~{word_count} words")
    finally:
        conn.close()


RESEARCH_NOTES_FILE = "research_notes.md"

# Flow's "still busy" wave leaves some cards unrendered. Rather than failing the
# stage, pause (the account settles) and resume: both engines skip what already
# exists, so a round only attempts the missing cards.
IMAGE_RESUME_PAUSE_SECONDS = 300
IMAGE_RESUME_ROUNDS = 3


def _research_notes(cfg, pdir: Path, title: str, genre: str,
                    source_text: str, provider: str | None) -> str:
    """Cached fact-notes for this production, generated once by the writer's
    provider. The script is composed FROM these rather than from the transcript
    prose, so it stops echoing the source: the first live run measured 93.5%
    overlap with the transcript and the copycat gate rejects over 20%."""
    pdir = Path(pdir)
    path = pdir / RESEARCH_NOTES_FILE
    try:
        cached = path.read_text(encoding="utf-8").strip()
        if cached:
            _log_line(f"using cached {RESEARCH_NOTES_FILE} "
                      f"({len(cached.split())} words)")
            return cached
    except OSError:
        pass
    try:
        notes = studio.llm_generate(
            cfg, studio.notes_prompt(title, genre, source_text),
            provider=provider, max_tokens=studio.NOTES_MAX_TOKENS)
    except Exception as exc:  # noqa: BLE001 - notes are an optimisation
        _log_line(f"could not build research notes ({exc}); writing from the "
                  f"transcript")
        return source_text
    notes = (notes or "").strip()
    if not notes:
        _log_line("research notes came back empty; writing from the transcript")
        return source_text
    try:
        path.write_text(notes + "\n", encoding="utf-8")
    except OSError:
        pass
    _log_line(f"built {RESEARCH_NOTES_FILE} ({len(notes.split())} words)")
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
    too_short = words < int(target_words * 0.6)
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
    style = studio.find_style(pdir)
    style_guide = style.read_text(encoding="utf-8") if style else ""
    source_words = len(re.findall(r"\w+", source_text)) if source_text else 0
    target_words = studio.script_target_words(cfg.studio_script_words,
                                              source_words)
    # Compose from NEUTRAL NOTES, not the transcript prose - otherwise the writer
    # echoes the source and every attempt fails the copycat gate. Cached once.
    facts = (_research_notes(cfg, pdir, prod["title"], prod["genre"],
                             source_text, provider)
             if source_text.strip() else "")
    min_rating = eff["script_min_rating"]
    max_overlap = eff["script_max_overlap"]
    hard_overlap = eff["script_hard_overlap"]
    attempts_allowed = max(1, int(eff["script_max_attempts"]))
    judge = studio.judge_provider(cfg, provider, eff["script_judge_provider"])
    script_path = pdir / "script.md"
    auto_dir = pdir / "versions" / "script"
    auto_dir.mkdir(parents=True, exist_ok=True)

    attempts: list[dict] = []
    previous: dict | None = None
    text = ""
    for attempt in range(1, attempts_allowed + 1):
        variation = studio.variation_nudge(
            attempt=attempt,
            overlap=previous["overlap"] if previous else None,
            runs=previous["runs"] if previous else None,
            feedback=previous["feedback"] if previous else None)
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
        words = len(re.findall(r"\w+", text))
        overlap = studio.overlap_ratio(text, source_text)
        runs = studio.overlap_runs(text, source_text) if overlap > 0 else []
        rating = studio.rate_script(cfg, prod["title"], prod["genre"], text,
                                    source_text, style_guide, judge)
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

    # settle for the best draft rather than shipping a rejected one blindly
    best = max(attempts, key=lambda a: ((a["score"] or 0), -a["overlap"]))
    text = best["text"]
    passed = best["passed"]
    if script_path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy(script_path, auto_dir / f"auto-{stamp}.md")
    script_path.write_text(text + "\n", encoding="utf-8")
    # Persist the judge's per-attempt scores, criteria and feedback. Without
    # this the only trace of WHY a draft was rejected is the log line, and a
    # "rating 5.5 vs min 9.4" warning is unactionable.
    (auto_dir / "review.json").write_text(
        json.dumps([{"attempt": a["attempt"], "score": a["score"],
                     "overlap": round(a["overlap"], 4), "passed": a["passed"],
                     "words": a.get("words"), "too_long": a.get("too_long"),
                     "criteria": a["rating"].get("criteria") or {},
                     "feedback": a["rating"].get("feedback") or [],
                     "weak_spans": a["rating"].get("weak_spans") or [],
                     "judge_error": a["rating"].get("error")}
                    for a in attempts], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")

    conn = _connect(cfg)
    try:
        detail = (f"{provider}, best of {len(attempts)} attempt(s): rating "
                  f"{best['score'] if best['score'] is not None else 'n/a'}, "
                  f"overlap {best['overlap']:.1%}, target {target_words} words "
                  f"(wrote ~{best.get('words', '?')}), "
                  f"judged by {judge}, "
                  f"took {format_duration(time.monotonic() - t0)}")
        db.update_production(conn, pid, llm_provider=provider)
        if passed:
            db.add_step(conn, pid, "script", "auto", detail=detail)
        else:
            # "accept best and continue" means the draft IS this stage's
            # artifact, so the step counts as done. Recording it as failed
            # would make a later resume regenerate the script - and the audio
            # and subtitles were already built from this one. The warning
            # column is what flags it to the user.
            warning = (f"script gate failed after {len(attempts)} attempt(s) - "
                       f"kept the best: rating "
                       f"{best['score'] if best['score'] is not None else 'n/a'} "
                       f"(min {min_rating}), overlap {best['overlap']:.1%} "
                       f"(target {max_overlap:.0%})")
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
        studio.run_hook(cfg.studio_tts_command,
                        {"script": script, "out": out, "voice": voice or ""})
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


def _run_shots(cfg, pid: int, provider: str | None = None) -> None:
    t0 = time.monotonic()
    conn = _connect(cfg)
    try:
        prod = db.get_production(conn, pid)
        pdir = studio.prod_dir(cfg, pid)
        srt = studio.find_srt(pdir)
        if not srt:
            raise RuntimeError("Generate or upload the subtitles first")
        bible = studio.find_bible(pdir)
        if not bible:
            # seed from the own channel / per-genre seed folder first, so an
            # unattended run does not have to pause at the bible gate
            seeded = studio.seed_production(cfg, conn, prod)
            if seeded["source"]:
                bible = studio.find_bible(pdir)
        if not bible:
            # the manifest-authoring brief's bible gate: the LLM will refuse
            # to plan without one (it just asks for the bible instead)
            raise _Paused("no character/reference bible - the planning brief "
                          "requires one before planning - write it at the "
                          "shots stage, then Resume")
        style = studio.find_style(pdir)
        style_guide = style.read_text(encoding="utf-8") if style else ""
        bible_text = bible.read_text(encoding="utf-8")
        brief = studio.load_manifest_brief(cfg)
        srt_text = srt.read_text(encoding="utf-8")
        cues = studio.parse_srt_cues(srt_text)
        eff = _effective(cfg, pid)
        min_align = eff["shotlist_min_alignment"]
        max_hold = eff["shotlist_max_hold_seconds"]
        attempts_allowed = max(1, int(eff["shotlist_max_attempts"]))
        judge = studio.judge_provider(cfg, provider,
                                      eff["shotlist_judge_provider"])
        feedback = ""
        attempts: list[dict] = []
        data: dict = {}
        sheet = ""
        for attempt in range(1, attempts_allowed + 1):
            prompt = studio.shotlist_prompt(
                brief, srt_text, style_guide,
                extra_direction=db.stage_extra(prod, "shots"),
                bible=bible_text, feedback=feedback)
            text = studio.llm_generate(cfg, prompt, provider=provider)
            data, sheet = studio.parse_shotlist_output(text)
            review = studio.review_shotlist(cfg, data, cues, judge,
                                            max_hold_seconds=max_hold)
            passed = (not review["faults"] and review["ratio"] >= min_align)
            attempts.append({**review, "attempt": attempt, "data": data,
                             "sheet": sheet})
            _log_line(f"shotlist attempt {attempt}: "
                      f"{review['matched']}/{review['total']} prompts detailed "
                      f"enough ({review['ratio']:.0%}), "
                      f"{len(review['faults'])} fault(s)"
                      + (f", {len(review['warnings'])} pacing warning(s)"
                         if review.get("warnings") else ""))
            if passed:
                break
            feedback = _shotlist_feedback(review, min_align)

        best = max(attempts, key=lambda a: (not a["faults"], a["ratio"]))
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
        db.update_production(conn, pid, llm_provider=provider)
        pacing = best.get("warnings") or []
        detail = (f"{len(data.get('images', []))} image(s) in "
                  f"{len(data.get('shots', []))} shot(s) via manifest brief, "
                  f"best of {len(attempts)} attempt(s): prompts detailed "
                  f"enough {best['matched']}/{best['total']} "
                  f"({best['ratio']:.0%}, min {min_align:.0%}), "
                  f"{len(best['faults'])} fault(s), "
                  f"judged by {judge}, "
                  f"took {format_duration(time.monotonic() - t0)}")
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
    """The correction list fed into the next shotlist attempt."""
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
        for m in review["weak"][:10]:
            missing = ("; missing: " + ", ".join(m["missing"])
                       if m.get("missing") else "")
            parts.append(f"- {m['asset']} ({m['verdict']}){missing}"
                         + (f" - {m['reason']}" if m.get("reason") else "")
                         + (f" | cue says: {m['narration']}"
                            if m.get("narration") else ""))
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
    upload it to the Flow project under the ref's own name (FlowImagesGen) or
    resolve it as a local file (the Renderly driver)."""
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
    result = studio.run_flowimagesgen_refs(cfg, pdir, pid, todo, log=log,
                                           cancel=cancel)
    made = result["generated"]
    failed = result["missing"] + stranded
    detail = (f"{len(made)} reference image(s) generated "
              f"({', '.join(made[:6])})" if made else "no references generated")
    if failed:
        detail += (f" | {len(failed)} still missing: "
                   f"{', '.join(failed[:6])}")
    detail += f", took {format_duration(time.monotonic() - t0)}"
    _step(detail)


def _should_resume_images(exc: Exception, round_no: int) -> bool:
    """True when a failed images round should pause and be resumed: ONLY Flow's
    'N of M image(s) were not produced' wave (its 'still busy' timeout), and only
    while rounds remain. Any other error is a real failure."""
    return (round_no < IMAGE_RESUME_ROUNDS
            and "were not produced" in str(exc))


def _run_images(cfg, pid: int, mode: str | None = None,
                engine: str | None = None,
                flow_channel: str = "whisperradar",
                flow_project: str = "", flow_upscale: int | None = None,
                flow_master: str = "", renderly_channel=None,
                flow_project_url: str | None = None,
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
    conn = _connect(cfg)
    try:
        db.update_production(conn, pid, render_mode=mode)
        managed = bool(settings.load(conn).get("services_managed"))
    finally:
        conn.close()
    # bring up only what this engine/mode needs; a service that already
    # answers is the user's and is left alone
    try:
        services.MANAGER.ensure(cfg, services.services_for(engine, mode),
                                log_fn=log)
        for round_no in range(1, IMAGE_RESUME_ROUNDS + 1):
            try:
                if engine == "flowimagesgen":
                    # FlowImagesGen drives Flow itself and upscales on the way
                    # out, so the Renderly channel/project and the Flow Driver
                    # do not apply.
                    count = studio.run_imagegen_flowimagesgen(
                        cfg, pdir, pid, upscale=flow_upscale, log=log,
                        cancel=cancel, project_url=flow_project_url)
                    source = "FlowImagesGen"
                elif mode == "flow":
                    # per-image refs come from the shotlist's own refs registry,
                    # which flow.js resolves itself; the production refs\ folder
                    # is just the library it resolves names against - attaching
                    # every file globally would blow past Flow's 3-ingredient
                    # limit
                    count = studio.run_imagegen_flow(
                        cfg, pdir, channel=flow_channel, project=flow_project,
                        upscale=flow_upscale, master=flow_master, log=log,
                        cancel=cancel, pid=pid)
                    source = "Flow Driver (Google Flow)"
                else:
                    count = studio.run_imagegen(cfg, pdir,
                                                channel=renderly_channel,
                                                upscale=flow_upscale)
                    source = "Renderly"
                break
            except RuntimeError as exc:
                # Flow gave up on some cards ("still busy"). Pause, then resume:
                # the driver skips what is already on disk, so the next round
                # only attempts the gaps.
                if not _should_resume_images(exc, round_no):
                    raise
                log(f"[auto-run] images: {exc}")
                log(f"[auto-run] images: pausing "
                    f"{IMAGE_RESUME_PAUSE_SECONDS // 60} minutes, then resuming "
                    f"the missing card(s) - round {round_no} of "
                    f"{IMAGE_RESUME_ROUNDS - 1}")
                time.sleep(IMAGE_RESUME_PAUSE_SECONDS)
    finally:
        # stop what we started, if the user opted in; never a service that was
        # already running
        services.MANAGER.release(cfg, managed, log_fn=log)
    conn = _connect(cfg)
    try:
        db.add_step(conn, pid, "images", "auto",
                    detail=f"{count} image(s) via {source}, "
                           f"took {format_duration(time.monotonic() - t0)}")
    finally:
        conn.close()


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
        if studio.find_style(pdir):
            return {"stage": stage, "action": "skip",
                    "detail": "style guide already exists"}
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
            return {"stage": stage, "action": "pause",
                    "detail": "no character/reference bible - the planning "
                              "brief requires one before planning - write it "
                              "at the shots stage, then Resume"}
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
        if eff["engine"] == "flowimagesgen":
            refs = (len([p for p in (pdir / "refs").glob("*") if p.is_file()])
                    if (pdir / "refs").exists() else 0)
            detail = (f"{len(missing)} missing image(s) via FlowImagesGen "
                      f"(upscale {eff['upscale']}"
                      + (f", {refs} ref image(s)" if refs else "") + ")")
        elif mode == "flow":
            refs = (len([p for p in (pdir / "refs").glob("*") if p.is_file()])
                    if (pdir / "refs").exists() else 0)
            detail = (f"{len(missing)} missing image(s) via Flow Driver "
                      f"(channel {eff['renderly_channel_name']}, default "
                      f"project, upscale {eff['upscale']}"
                      + (f", {refs} ref image(s)" if refs else "") + ")")
        else:
            detail = (f"{len(missing)} missing image(s) via Renderly API "
                      f"(channel {eff['renderly_channel_name']}, upscale "
                      f"{eff['upscale']})")
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

def _stage_params(cfg, pid: int, stage: str, log, cancel=None) -> dict:
    """Auto-run parameters per stage: the production's saved LLM provider
    for LLM stages, the saved render mode for images."""
    if stage in ("style", "script", "shots"):
        return {"provider": _default_provider(cfg, pid)}
    if stage == "images":
        eff = _effective(cfg, pid)
        mode = _default_render_mode(cfg, _get_prod(cfg, pid))
        params = {"mode": mode, "engine": eff["engine"], "flow_project": "",
                  "flow_upscale": eff["upscale"], "log": log, "cancel": cancel}
        if eff["engine"] == "flowimagesgen":
            pass  # drives Flow itself; no Renderly channel/project involved
        elif mode == "flow":
            params["flow_channel"] = eff["renderly_channel_name"]
        else:
            params["renderly_channel"] = studio.resolve_renderly_channel(
                cfg, eff["own_channel"], create=True)
        return params
    return {}


def run_pipeline(cfg, pid: int, job=None, log=None,
                 stop_before: str | None = None) -> str:
    """Run every remaining stage in order, skipping the ones that are
    already done and pausing when manual input is missing.

    Each stage's action is re-evaluated when it is reached, so stages that
    become runnable mid-run (e.g. shots created the shotlist) execute
    instead of pausing per an outdated snapshot.

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
                                                cancel=cancel_check))
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
