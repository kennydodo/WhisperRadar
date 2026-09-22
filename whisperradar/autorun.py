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
import re
import shutil
import time

from pathlib import Path

from . import db, settings, studio, transcribe
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
    prod = _get_prod(cfg, pid)
    return (prod["llm_provider"] if prod else None) or cfg.studio_llm_default


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


def _run_script(cfg, pid: int, provider: str | None = None) -> None:
    t0 = time.monotonic()
    conn = _connect(cfg)
    try:
        prod = db.get_production(conn, pid)
        if not prod:
            raise RuntimeError("Unknown production")
        source_text = _source_transcript_text(cfg, pid)
    finally:
        conn.close()
    pdir = studio.prod_dir(cfg, pid)
    style = studio.find_style(pdir)
    style_guide = style.read_text(encoding="utf-8") if style else ""
    source_words = len(re.findall(r"\w+", source_text)) if source_text else 0
    target_words = cfg.studio_script_words or source_words or 1200
    variation = studio.variation_nudge()
    prompt = studio.script_prompt(prod["title"], prod["genre"], source_text,
                                  style_guide, target_words=target_words,
                                  variation=variation,
                                  extra_direction=db.stage_extra(
                                      prod, "script"))
    conn = _connect(cfg)
    try:
        text = studio.llm_generate(cfg, prompt, provider=provider)
        if not text:
            raise RuntimeError("LLM returned an empty script")
        script_path = pdir / "script.md"
        if script_path.exists():
            auto_dir = pdir / "versions" / "script"
            auto_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            shutil.copy(script_path, auto_dir / f"auto-{stamp}.md")
        script_path.write_text(text + "\n", encoding="utf-8")
        ratio = studio.overlap_ratio(text, source_text)
        warn = " | WARNING: high overlap with source" if ratio > 0.2 else ""
        db.update_production(conn, pid, llm_provider=provider)
        db.add_step(conn, pid, "script", "auto",
                    detail=f"{provider}, overlap {ratio:.1%}, "
                           f"target {target_words} words, "
                           f"took {format_duration(time.monotonic() - t0)}"
                           f"{warn}")
    finally:
        conn.close()


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
        prompt = studio.shotlist_prompt(
            brief, srt.read_text(encoding="utf-8"), style_guide,
            extra_direction=db.stage_extra(prod, "shots"),
            bible=bible_text)
        text = studio.llm_generate(cfg, prompt, provider=provider)
        data, sheet = studio.parse_shotlist_output(text)
        (pdir / "shotlist.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        if sheet:
            (pdir / "batch_sheet.txt").write_text(
                sheet + "\n", encoding="utf-8")
        db.update_production(conn, pid, llm_provider=provider)
        db.add_step(conn, pid, "shots", "auto",
                    detail=f"{len(data.get('images', []))} image(s) in "
                           f"{len(data.get('shots', []))} shot(s) via "
                           f"manifest brief, took "
                           f"{format_duration(time.monotonic() - t0)}")
    finally:
        conn.close()


def _run_images(cfg, pid: int, mode: str | None = None,
                flow_channel: str = "whisperradar",
                flow_project: str = "", flow_upscale: int | None = None,
                flow_master: str = "", renderly_channel=None,
                log=None, cancel=None) -> None:
    t0 = time.monotonic()
    pdir = studio.prepare_project_folder(cfg, pid)
    if not (pdir / "shotlist.json").exists():
        raise _Paused("shotlist.json is missing - plan or save a shotlist, "
                      "then Resume")
    if mode is None:
        mode = _default_render_mode(cfg, _get_prod(cfg, pid))
    if mode not in ("api", "flow"):
        mode = "api"
    if log is None:
        log = lambda m: None
    conn = _connect(cfg)
    try:
        db.update_production(conn, pid, render_mode=mode)
    finally:
        conn.close()
    if mode == "flow":
        # per-image refs come from the shotlist (resolved through its refs
        # registry by prepare_flow_batch); the production refs\ folder is
        # just the library flow.js resolves names against - attaching every
        # file globally would blow past Flow's 3-ingredient limit
        count = studio.run_imagegen_flow(
            cfg, pdir, channel=flow_channel, project=flow_project,
            upscale=flow_upscale, master=flow_master, log=log,
            cancel=cancel)
        source = "Flow Driver (Google Flow)"
    else:
        count = studio.run_imagegen(cfg, pdir, channel=renderly_channel,
                                    upscale=flow_upscale)
        source = "Renderly"
    conn = _connect(cfg)
    try:
        db.add_step(conn, pid, "images", "auto",
                    detail=f"{count} image(s) via {source}, "
                           f"took {format_duration(time.monotonic() - t0)}")
    finally:
        conn.close()


def _run_merge(cfg, pid: int, mode: str | None = None) -> None:
    """mode: 'hook' = studio.merge_command only (merge route),
    'cli' = ImgToVideo.Cli headless render (video/render route),
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
        final = studio.run_merge_render(cfg, pdir)
        detail = final.name
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
            conn = _connect(cfg)
            try:
                prod = db.get_production(conn, pid)
                voice = (prod["voice"] if prod else None) or None
            finally:
                conn.close()
            return {"stage": stage, "action": "run",
                    "detail": "narration via the configured TTS hook"
                              + (f" (voice {voice})" if voice
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
        if mode == "flow":
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
        if studio.find_final(pdir):
            return {"stage": stage, "action": "skip",
                    "detail": "final video already exists"}
        hook = "the configured merge hook" if cfg.studio_merge_command \
            else "ImgToVideo (headless render)"
        return {"stage": stage, "action": "run",
                "detail": f"final render via {hook} - can take a long time"}
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
        params = {"mode": mode, "flow_project": "",
                  "flow_upscale": eff["upscale"], "log": log, "cancel": cancel}
        if mode == "flow":
            params["flow_channel"] = eff["renderly_channel_name"]
        else:
            params["renderly_channel"] = studio.resolve_renderly_channel(
                cfg, eff["own_channel"], create=True)
        return params
    return {}


def run_pipeline(cfg, pid: int, job=None, log=None) -> str:
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
            if job is not None:
                job.resume = True
            return result
        ran += 1
        _advance(cfg, pid, stage)
        log(f"[auto-run] stage {i}/{total}: {stage} - done")
    summary = (f"Auto-run complete: {ran} stage(s) executed, "
               f"{total - ran} skipped - ready for review")
    log(f"[auto-run] pipeline finished - {summary}")
    if job is not None:
        job.summary = summary
        job.resume = False  # a completed run needs no Resume offer
    return "ok"
