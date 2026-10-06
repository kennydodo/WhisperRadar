"""Run the script and shotlist stages in web chats (no API).

The prompts are the EXTERNAL-LLM ones from `external_prompts` (the same text
you would copy by hand), so the bar is the one the Studio already states:

    script    writer (z.ai)  -> judge (DeepSeek) -> the judge's feedback goes
              back to the SAME writer chat -> judge again, up to `rounds`
    shotlist  writer plans   -> judge reviews the whole plan -> weak prompts go
              back as a narrow patch request (hard faults as a full re-issue)
              -> judge again

The judge always starts a NEW chat, so it never sees the writer's reasoning.
Results are saved exactly where the Studio's own save routes put them. A run
that never passes still saves its best draft for you to read, but does NOT move
the production to the next stage.

The transport is anything with `ask(site, prompt, files, new_chat)`; the real
one drives the browser (`webchat.py`), tests use a fake.
"""
from __future__ import annotations

import contextlib
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from . import autorun, db, external_prompts as ep, studio, webchat

# Above this the prompt is split into attached files (external_prompts' files
# mode). Both chat boxes took 30k+ characters when tried by hand.
INLINE_MAX_CHARS = 60000
# None = no limit: the loop runs until the judge passes the work (or you stop
# it). Only an explicit `rounds=` caps it.
MAX_ROUNDS = None
MAX_CONTINUES = 8
_CHROME_LINES = {"json", "copy", "download", "copy code", "code"}


class StageFailed(RuntimeError):
    """The loop ran out of rounds; the message says what was still wrong."""


# ---- transport ----------------------------------------------------------------

class WebTransport:
    def __init__(self, chat: "webchat.WebChat", log: Callable[[str], None]):
        self.chat, self.log = chat, log

    def ask(self, site: str, prompt: str, files=(), new_chat: bool = True,
            ready=None):
        return self.chat.ask(site, prompt, list(files), new_chat=new_chat,
                             ready=ready, log=self.log)


@contextlib.contextmanager
def web_transport(cfg, log: Callable[[str], None] = print):
    root = Path(cfg.db_path).parent / "webchat"
    with webchat.WebChat(root) as chat:
        yield WebTransport(chat, log)


def _send(transport, site: str, build: Callable, log,
          new_chat: bool = True, ready=None):
    """Build the prompt inline; switch to attached files when it is too big."""
    text = build(None)
    files = None
    if len(text) > INLINE_MAX_CHARS:
        files = []
        text = build(files)
    tmp, paths = None, []
    try:
        if files:
            tmp = tempfile.mkdtemp(prefix="wr_webchat_")
            for f in files:
                p = Path(tmp) / f["name"]
                p.write_text(f["text"], encoding="utf-8")
                paths.append(str(p))
        log(f"{site}: sending {len(text):,} characters"
            + (f" + {len(paths)} attached file(s)" if paths else ""))
        return transport.ask(site, text, paths, new_chat=new_chat,
                             ready=ready)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def _is_json_verdict(text: str) -> bool:
    return bool(studio._parse_json_object(text))


def _has_json(text: str) -> bool:
    return "{" in (text or "")


# ---- reading replies -------------------------------------------------------------

def _clean_script(reply: str) -> str:
    text = (reply or "").strip()
    text = re.sub(r"^```[a-z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _json_chunk(reply: str, first: bool) -> str:
    """The JSON text inside a reply: fences and the chat's 'json / Copy /
    Download' chrome removed. The first chunk starts at its first '{'."""
    lines = []
    for ln in (reply or "").splitlines():
        if ln.strip().startswith("```"):
            continue
        if ln.strip().lower() in _CHROME_LINES:
            continue
        lines.append(ln)
    text = "\n".join(lines).strip()
    if first and "{" in text:
        text = text[text.index("{"):]
    return text


def _plan_from_text(body: str) -> dict | None:
    """The shotlist object in `body`: the normal parser first, then the
    biggest balanced {...} that holds both a shots[] and an images[] list
    (a reply may carry commentary, or a stray small object before the plan)."""
    try:
        return studio.parse_shotlist_output(body)[0]
    except RuntimeError:
        pass
    best, n = None, len(body)
    for i, ch in enumerate(body):
        if ch != "{":
            continue
        end = webchat._balanced_end(body, i, "{", "}")
        if end is None or (best is not None and end - i <= best[0]):
            continue
        try:
            obj = json.loads(body[i:end])
        except ValueError:
            continue
        if (isinstance(obj, dict) and isinstance(obj.get("shots"), list)
                and obj["shots"] and isinstance(obj.get("images"), list)
                and obj["images"]):
            best = (end - i, obj)
    return best[1] if best else None


def _dump(cfg, pid: int, name: str, text: str) -> None:
    """Keep every raw chat reply in the production folder, so a parsing
    problem can be looked at afterwards (webchat_debug/<name>.txt)."""
    try:
        d = studio.prod_dir(cfg, pid) / "webchat_debug"
        d.mkdir(exist_ok=True)
        (d / f"{name}.txt").write_text(text or "", encoding="utf-8")
    except OSError:
        pass


def _collect_plan(transport, site: str, reply: str, log,
                  dump: Callable[[str, str], None] = lambda n, t: None,
                  tag: str = "plan_reply") -> dict:
    """The shotlist JSON from a reply, sending 'continue' only while the JSON
    is really cut off (the planner prompt's own convention)."""
    dump(f"{tag}_1", reply)
    body = _json_chunk(reply, True)
    for i in range(MAX_CONTINUES + 1):
        data = _plan_from_text(body)
        if data is not None:
            return data
        if i == MAX_CONTINUES:
            break
        log(f"{site}: no complete shotlist in the reply yet "
            f"({len(body):,} characters read) - sending 'continue' "
            f"({i + 1}/{MAX_CONTINUES})")
        reply = transport.ask(site, "continue", (), new_chat=False,
                              ready=_has_json)
        dump(f"{tag}_{i + 2}", reply)
        body = body.rstrip() + "\n" + _json_chunk(reply, False)
    raise StageFailed(
        f"{site} never gave a readable shotlist ({len(body):,} characters "
        f"read). The raw replies are in the production's webchat_debug "
        f"folder.")


# ---- saving (the same steps as the Studio's own save routes) -----------------------

def save_script(cfg, pid: int, text: str, detail: str, advance: bool) -> None:
    pdir = studio.prod_dir(cfg, pid)
    (pdir / "script.md").write_text(text.strip() + "\n", encoding="utf-8")
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        db.add_step(conn, pid, "script", "web chat", detail=detail)
    finally:
        conn.close()
    if advance:
        autorun._advance(cfg, pid, "script")


def save_shotlist(cfg, pid: int, data: dict, detail: str,
                  advance: bool) -> str | None:
    """Returns the coverage gap (a plan that stops early), or None."""
    pdir = studio.prod_dir(cfg, pid)
    (pdir / "shotlist.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8")
    gap = autorun.coverage_gap(pdir)
    if gap or not advance:
        return gap
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        db.add_step(conn, pid, "shots", "web chat", detail=detail)
    finally:
        conn.close()
    autorun._advance(cfg, pid, "shots")
    return None


# ---- script -----------------------------------------------------------------------

def _script_feedback(words: int, target: int, reasons: list[str],
                     judged: dict, truncated: bool) -> str:
    lo, hi = ep._length_window(target)
    lines = [f"The editor did NOT accept this draft. Why: "
             f"{'; '.join(reasons) or 'see below'}."]
    if truncated:
        lines.append("The draft looks cut off - it must end on a complete "
                     "sentence.")
    if judged.get("feedback"):
        lines.append("Editor's feedback:")
        lines += [f"- {f}" for f in judged["feedback"]]
    if judged.get("weak_spans"):
        lines.append("Passages the editor flagged as copied or weak:")
        lines += [f"- {w}" for w in judged["weak_spans"]]
    lines.append(
        f"Rewrite the COMPLETE script, fixing every point above and keeping "
        f"what the editor did not criticise. Your draft was {words} words; "
        f"it must be {lo}-{hi} words (target {target}). Use only the facts "
        f"you were given. Same reply format as before: the finished script "
        f"as plain text only, no notes before or after it.")
    return "\n".join(lines)


def run_script(cfg, pid: int, transport, writer: str = "zai",
               judge: str = "deepseek", rounds: int | None = MAX_ROUNDS,
               log: Callable[[str], None] = print, title: str = "",
               should_stop: Callable[[], bool] = lambda: False) -> str:
    ctx = ep._context(cfg, pid)
    eff = ctx["eff"]
    target = ep._target_words(cfg, pid, None)
    source = autorun._source_transcript_text(cfg, pid)
    min_rating = float(eff["script_min_rating"])
    max_overlap = float(eff["script_max_overlap"])
    hard_overlap = float(eff["script_hard_overlap"])

    def long_enough(text: str) -> bool:        # not just "Thinking..." / a stub
        return len(text.split()) >= 0.5 * target

    reply = _send(transport, writer,
                  lambda f: ep.script_writer_prompt(cfg, pid, title, None,
                                                    None, None, f), log,
                  ready=long_enough)
    attempts = []
    rnd = 0
    while True:
        rnd += 1
        script = _clean_script(reply)
        words = len(script.split())
        if words < 20:
            raise StageFailed(
                f"{writer} did not return a usable script (got {words} "
                f"word(s): \"{script[:80]}\"). Nothing was sent to the "
                f"judge.")
        overlap = studio.overlap_ratio(script, source) if source.strip() else 0.0
        log(f"round {rnd}: draft of {words} words (target {target}), "
            f"overlap {overlap:.1%} - asking {judge} to judge it")
        for attempt in (1, 2):      # a judge reply with no score is asked again
            raw = _send(transport, judge,
                        lambda f: ep.script_judge_prompt(
                            cfg, pid, script, title, None, None, None, f),
                        log, ready=_is_json_verdict)
            verdict = studio._parse_json_object(raw)
            try:
                score = round(float(verdict.get("score")), 1)
            except (TypeError, ValueError):
                score = None
            if score is not None:
                break
            log(f"{judge} gave no usable score" + (" - asking once more"
                                                    if attempt == 1 else ""))
        judged = {
            "feedback": [str(x) for x in (verdict.get("feedback") or [])
                         if str(x).strip()],
            "weak_spans": [str(x) for x in (verdict.get("weak_spans") or [])
                           if str(x).strip()]}
        err = None if score is not None else (
            "no usable score in the judge's reply: "
            + " ".join(str(raw or "").split())[:120])
        passed, reasons, _long, too_short = autorun._script_gate(
            words, target, overlap, score, min_rating, max_overlap,
            hard_overlap, err)
        cut = studio.script_looks_truncated(script)
        if cut:
            passed = False
            reasons.append("the script looks cut off")
        log(f"round {rnd}: score {score if score is not None else 'n/a'} "
            f"(min {min_rating:g}) - " + ("PASSED" if passed else
                                          "not accepted: " + "; ".join(reasons)))
        attempts.append({"script": script, "score": score or 0.0,
                         "too_short": too_short, "cut": cut,
                         "reasons": reasons, "words": words})
        if passed:
            save_script(cfg, pid, script, f"web chat {writer}/{judge}: round "
                        f"{rnd}, score {score}, {words} words, overlap "
                        f"{overlap:.1%}", advance=True)
            log("script saved; next stage unlocked")
            return script
        if rounds is not None and rnd >= rounds:
            break
        if should_stop():
            log("stopped by you")
            break
        reply = transport.ask(
            writer, _script_feedback(words, target, reasons, judged, cut), (),
            new_chat=False, ready=long_enough)
    best = max(attempts, key=lambda a: (not a["too_short"], not a["cut"],
                                        a["score"]))
    save_script(cfg, pid, best["script"],
                f"web chat {writer}/{judge}: NOT accepted after {rounds} "
                f"rounds ({'; '.join(best['reasons'])})", advance=False)
    raise StageFailed(
        f"No draft passed in {rounds} rounds. The best one was saved as the "
        f"script for you to read, but the production was not advanced. "
        f"Last problems: {'; '.join(best['reasons'])}")


# ---- shotlist ---------------------------------------------------------------------

def _weak_list(verdict: dict) -> list[dict]:
    out = []
    for w in verdict.get("shots") or []:
        if isinstance(w, dict) and w.get("asset"):
            out.append({"asset": str(w["asset"]),
                        "verdict": str(w.get("verdict") or "weak"),
                        "missing": [str(m) for m in (w.get("missing") or [])],
                        "reason": str(w.get("reason") or "")})
    return out


def _narration_for(data: dict, cues: list[dict], asset: str) -> str:
    for shot in data.get("shots") or []:
        if isinstance(shot, dict) and shot.get("asset") == asset:
            rng = studio.cue_range(shot.get("cues"))
            if rng:
                a, b = rng
                return " ".join(c["text"] for c in cues
                                if a <= c["index"] <= b)[:600]
    return ""


def _plan_feedback(faults: list[str], weak: list[dict], fixes: list[str]) -> str:
    lines = ["The reviewer found problems with your shotlist."]
    if faults:
        lines.append("Hard-rule violations (each one must be fixed):")
        lines += [f"- {f}" for f in faults]
    if weak:
        lines.append("Image prompts that do not state what their narration "
                     "needs:")
        for w in weak:
            lines.append(f"- {w['asset']}"
                         + (f": missing {', '.join(w['missing'])}"
                            if w["missing"] else "")
                         + (f" ({w['reason']})" if w["reason"] else ""))
    if fixes:
        lines.append("Most useful changes, in order:")
        lines += [f"- {x}" for x in fixes[:8]]
    lines.append(
        "Reply with the COMPLETE corrected shotlist JSON under the same "
        "output rules as before (one ```json block; if you run out of room, "
        "stop after the last complete entry and I will say \"continue\"). "
        "Keep every shot and prompt that was not criticised exactly as it "
        "was.")
    return "\n".join(lines)


def run_shotlist(cfg, pid: int, transport, writer: str = "zai",
                 judge: str = "deepseek", rounds: int | None = MAX_ROUNDS,
                 log: Callable[[str], None] = print,
                 should_stop: Callable[[], bool] = lambda: False) -> dict:
    ctx = ep._context(cfg, pid)
    plan = ep._plan_inputs(cfg, pid, ctx)
    cues = plan["cues"]

    def dump(name: str, text: str) -> None:
        _dump(cfg, pid, name, text)

    reply = _send(transport, writer,
                  lambda f: ep.shotlist_planner_prompt(cfg, pid, f), log,
                  ready=_has_json)
    data = _collect_plan(transport, writer, reply, log, dump)
    log(f"{writer}: plan received ({len(data.get('shots') or [])} shots, "
        f"{len(data.get('images') or [])} images)")

    last_problem = ""
    best = None                     # (badness, data, problem): the plan to keep
    rnd = 0
    while True:
        rnd += 1
        text = json.dumps(data, ensure_ascii=False, indent=1)
        local: list[str] = []

        def build(f):
            prompt, loc = ep.shotlist_judge_prompt(cfg, pid, text, f)
            local[:] = list(loc or [])
            return prompt

        log(f"round {rnd}: asking {judge} to review the whole plan")
        raw = _send(transport, judge, build, log, ready=_is_json_verdict)
        dump(f"judge_reply_round{rnd}", raw)
        verdict = studio._parse_json_object(raw)
        if not verdict:
            log(f"{judge} gave no usable verdict - asking once more")
            raw = _send(transport, judge, build, log, ready=_is_json_verdict)
            verdict = studio._parse_json_object(raw)
        if not verdict:
            last_problem = f"{judge} never returned a usable verdict"
            break
        faults = [str(x) for x in (verdict.get("faults") or [])]
        hard = local + [f for f in faults if f not in local]
        weak = _weak_list(verdict)
        fixes = [str(x) for x in (verdict.get("fixes") or [])]
        passed = verdict.get("pass") is True and not hard
        ratio = verdict.get("detailed_ratio")
        log(f"round {rnd}: {len(hard)} fault(s), {len(weak)} weak shot(s), "
            f"detailed {ratio} - " + ("PASSED" if passed else "not accepted"))
        if passed:
            gap = save_shotlist(
                cfg, pid, data, f"web chat {writer}/{judge}: round {rnd}, "
                f"{len(data.get('images') or [])} image(s)", advance=True)
            if gap:
                raise StageFailed("Shotlist saved but NOT accepted: " + gap)
            log("shotlist saved; next stage unlocked")
            return data
        last_problem = "; ".join(hard[:3]) or (
            f"{len(weak)} image prompt(s) still weak")
        # a corrected plan can come back WORSE than the one before; keep the
        # best one seen so a stop or a failure never saves the worst
        badness = len(hard) * 3 + len(weak)
        if best is None or badness < best[0]:
            best = (badness, data, last_problem)
        log(f"best so far: {best[0]} (3 x faults + weak prompts), this "
            f"round: {badness}")
        if rounds is not None and rnd >= rounds:
            break
        if should_stop():
            log("stopped by you")
            break
        if not hard and weak:
            asked = [w["asset"] for w in weak]
            items = studio.with_current_prompts(weak, data)
            for w in items:
                w["narration"] = _narration_for(data, cues, w["asset"])
            prompt = studio.shotlist_patch_prompt(
                items, ctx["visual_style"], ctx["bible"])
            log(f"sending {len(asked)} weak prompt(s) back to {writer}")
            got = transport.ask(writer, prompt, (), new_chat=False,
                                ready=lambda t: bool(
                                    studio.parse_shotlist_patch(t)))
            good, unknown, missing = studio.check_shotlist_patch(
                studio.parse_shotlist_patch(got), asked)
            if unknown or missing:
                log(f"patch check: {len(unknown)} unknown key(s), "
                    f"{len(missing)} asset(s) not answered")
            data = studio.apply_shotlist_patch(data, good)
        else:
            log(f"sending the faults back to {writer} for a corrected plan")
            reply = transport.ask(writer, _plan_feedback(hard, weak, fixes),
                                  (), new_chat=False, ready=_has_json)
            data = _collect_plan(transport, writer, reply, log, dump,
                                 tag=f"plan_round{rnd + 1}")
    if best is not None:
        _bad, data, last_problem = best
    save_shotlist(cfg, pid, data,
                  f"web chat {writer}/{judge}: NOT accepted ({last_problem})",
                  advance=False)
    raise StageFailed(
        f"The plan did not pass after {rnd} round(s). The best plan seen "
        f"was saved as shotlist.json for you to read, but the production "
        f"was not advanced. Still wrong: {last_problem}")


# ---- entry points the web app calls --------------------------------------------------

def script_job(cfg, pid: int, writer: str, judge: str, log,
               should_stop: Callable[[], bool] = lambda: False) -> None:
    with web_transport(cfg, log) as t:
        run_script(cfg, pid, t, writer, judge, log=log,
                   should_stop=should_stop)


def shotlist_job(cfg, pid: int, writer: str, judge: str, log,
                 should_stop: Callable[[], bool] = lambda: False) -> None:
    with web_transport(cfg, log) as t:
        run_shotlist(cfg, pid, t, writer, judge, log=log,
                     should_stop=should_stop)
