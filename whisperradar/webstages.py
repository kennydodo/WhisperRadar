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

# what each site's mode switches default to when the run does not say
DEFAULT_OPTIONS = {"zai": {"thinking": "Low", "model": "flash"},
                   "deepseek": {"deepthink": True, "search": False}}


class WebTransport:
    def __init__(self, chat: "webchat.WebChat", log: Callable[[str], None],
                 options: dict | None = None):
        self.chat, self.log = chat, log
        self.options = {k: dict(v) for k, v in DEFAULT_OPTIONS.items()}
        for site, opts in (options or {}).items():
            self.options.setdefault(site, {}).update(opts or {})

    def ask(self, site: str, prompt: str, files=(), new_chat: bool = True,
            ready=None):
        return self.chat.ask(site, prompt, list(files), new_chat=new_chat,
                             options=self.options.get(site), ready=ready,
                             log=self.log)

    def set_stop(self, should_stop) -> None:
        """Let a wait for model capacity end when the user stops the run."""
        self.chat.should_stop = should_stop

    def adopt(self, site: str, url: str) -> None:
        self.chat.adopt(site, url)

    def urls(self) -> dict:
        return self.chat.urls()


@contextlib.contextmanager
def web_transport(cfg, log: Callable[[str], None] = print,
                  options: dict | None = None):
    root = Path(cfg.db_path).parent / "webchat"
    with webchat.WebChat(root) as chat:
        yield WebTransport(chat, log, options)


_CHATS_FILE = "webchat_chats.json"


def _chats_path(cfg, pid: int) -> Path:
    return studio.prod_dir(cfg, pid) / _CHATS_FILE


def load_chats(cfg, pid: int) -> dict:
    """The chat URLs the last run of this production kept (site -> url)."""
    try:
        data = json.loads(_chats_path(cfg, pid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return ({k: v for k, v in data.items() if isinstance(v, str) and v}
            if isinstance(data, dict) else {})


def save_chats(cfg, pid: int, transport) -> None:
    """Remember where each chat is, so the NEXT stage can continue in it (the
    LLMs then know the script when they plan the shotlist, as when you do it
    by hand in one chat)."""
    urls = getattr(transport, "urls", None)
    if not callable(urls):
        return
    try:
        found = {k: v for k, v in urls().items() if v}
        if not found:
            return
        merged = load_chats(cfg, pid)
        merged.update(found)
        _chats_path(cfg, pid).write_text(
            json.dumps(merged, indent=1), encoding="utf-8")
    except OSError:
        pass


def adopt_chats(cfg, pid: int, transport, sites, log) -> set:
    """Point the transport at the saved chats of `sites`; returns the sites
    that have one to continue."""
    adopt = getattr(transport, "adopt", None)
    saved = load_chats(cfg, pid)
    out = set()
    if not callable(adopt):
        return out
    for site in sites:
        if saved.get(site):
            adopt(site, saved[site])
            out.add(site)
    if out:
        log("continuing in the chats of the previous stage: "
            + ", ".join(sorted(out)))
    return out


def _send_in(transport, site: str, build, log, continuing: bool, ready=None):
    """_send in the saved chat when `continuing`; a chat that cannot be
    reopened falls back to a new one. Returns (reply, still_continuing)."""
    if continuing:
        try:
            return _send(transport, site, build, log, new_chat=False,
                         ready=ready), True
        except webchat.ChatLost:
            log(f"{site}: the previous chat cannot be reopened - starting a "
                f"new one")
    return _send(transport, site, build, log, new_chat=True,
                 ready=ready), False


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


_FILE_KEY = re.compile(r'"file"\s*:\s*"([^"]+)"')


def _join_chunks(body: str, chunk: str) -> str:
    """Append a 'continue' reply to the JSON read so far. A chat often
    restarts the entry it was cut in the middle of (the second reply begins
    with `{ "file": "S12_04..."` although the first one already held a
    half-written copy of it). Drop that half entry, then append."""
    body, chunk = body.rstrip(), chunk.lstrip()
    m = _FILE_KEY.search(chunk[:400])
    if m:
        pat = re.compile(r'"file"\s*:\s*"' + re.escape(m.group(1)) + '"')
        hits = list(pat.finditer(body))
        if hits:
            cut = body.rfind("{", 0, hits[-1].start() + 1)
            if cut != -1:
                return body[:cut].rstrip() + "\n" + chunk
    return body + "\n" + chunk


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


def _saved_plan(cfg, pid: int, tag: str = "plan_reply") -> dict | None:
    """The plan in the raw replies a previous run kept (plan_reply_1.txt,
    plan_reply_2.txt, ...), joined the way the loop joins them."""
    d = studio.prod_dir(cfg, pid) / "webchat_debug"
    body = ""
    for i in range(1, MAX_CONTINUES + 2):
        f = d / f"{tag}_{i}.txt"
        if not f.exists():
            break
        try:
            chunk = _json_chunk(f.read_text(encoding="utf-8"), i == 1)
        except OSError:
            break
        body = chunk if i == 1 else _join_chunks(body, chunk)
        data = _plan_from_text(body)
        if data is not None:
            return data
    return None


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
        body = _join_chunks(body, _json_chunk(reply, False))
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
    judge_open = False          # the judge keeps ONE chat, like the writer
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
            if judge_open:
                raw = _send(transport, judge,
                            lambda f: _script_followup(script), log,
                            new_chat=False, ready=_is_json_verdict)
            else:
                raw = _send(transport, judge,
                            lambda f: ep.script_judge_prompt(
                                cfg, pid, script, title, None, None, None, f),
                            log, ready=_is_json_verdict)
            verdict = studio._parse_json_object(raw)
            if verdict:
                judge_open = True
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
        passed, reasons, too_long, too_short = autorun._script_gate(
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
                         "judge_score": score,
                         "too_short": too_short, "cut": cut, "too_long": too_long,
                         "reasons": reasons, "words": words,
                         "overlap": overlap, "passed": passed,
                         "criteria": verdict.get("criteria") or {},
                         "feedback": judged["feedback"],
                         "weak_spans": judged["weak_spans"],
                         "error": err})
        _keep_attempts(cfg, pid, attempts)
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


def _plan_followup(text: str, files) -> str:
    """Short re-review request for a judge chat that has already seen the
    narration, the rules and its own earlier verdict."""
    head = (
        "The writer corrected the shotlist after your review. Review it "
        "again under the SAME rules and reply in exactly the SAME JSON format "
        "as before. First check that every point you raised earlier is now "
        "fixed, then check that nothing else regressed. Do not invent new "
        "faults for things that are correct. The narration is unchanged "
        "(use narration.txt from earlier in this chat).\n\n")
    if files is None:
        return head + "The revised shotlist:\n\n" + text
    files.append({"name": "shotlist.json", "text": text.strip() + "\n",
                  "about": "the revised shotlist"})
    return head + ("The revised shotlist is attached as shotlist.json - "
                   "REQUIRED: if it is not attached or you cannot read it in "
                   "full, reply with ONLY `Missing: shotlist.json` and stop.")


def _keep_attempts(cfg, pid: int, attempts: list[dict]) -> None:
    """Every judged round as versions/script/attempt-N.md + review.json (the
    same files the API loop writes), so the Studio's saved versions list and
    the reason a draft was rejected survive the run."""
    try:
        d = studio.prod_dir(cfg, pid) / "versions" / "script"
        d.mkdir(parents=True, exist_ok=True)
        for n, a in enumerate(attempts, 1):
            (d / f"attempt-{n}.md").write_text(a["script"] + "\n",
                                               encoding="utf-8")
        (d / "review.json").write_text(json.dumps([
            {"attempt": n, "score": a.get("judge_score"),
             "overlap": round(a.get("overlap", 0.0), 4),
             "passed": bool(a.get("passed")), "words": a["words"],
             "too_long": bool(a.get("too_long")), "is_baseline": False,
             "reasons": a.get("reasons") or [],
             "criteria": a.get("criteria") or {},
             "feedback": a.get("feedback") or [],
             "weak_spans": a.get("weak_spans") or [],
             "judge_error": a.get("error")}
            for n, a in enumerate(attempts, 1)], indent=2,
            ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass


def _script_followup(script: str) -> str:
    return (
        "The writer revised the script after your review. Judge it again "
        "under the SAME rules and reply in exactly the SAME JSON format as "
        "before. First check whether each point you raised earlier is now "
        "addressed, then check that nothing got worse. Source material and "
        "rules are unchanged (use the ones from earlier in this chat).\n\n"
        "The revised script:\n\n" + script)


def _plan_feedback(faults: list[str], weak: list[dict], fixes: list[str],
                   verdict: dict | None = None,
                   local: list[str] | None = None) -> str:
    """What goes back to the writer. With the judge's `verdict` the writer
    gets it VERBATIM (the JSON, as you would paste it by hand); without one
    (recovery, tests) the points are listed from `faults` / `weak` / `fixes`."""
    lines = ["The reviewer found problems with your shotlist."]
    if verdict:
        if local:
            lines.append("Rule checks run by code on your plan (fix these "
                         "too):")
            lines += [f"- {f}" for f in local]
        lines.append("The reviewer's verdict, verbatim (JSON):")
        lines.append(json.dumps(verdict, ensure_ascii=False, indent=1)[:40000])
    else:
        if faults:
            lines.append("Hard-rule violations (each one must be fixed):")
            lines += [f"- {f}" for f in faults]
        if weak:
            lines.append("Image prompts that do not state what their "
                         "narration needs:")
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
        "Change ONLY the shots and image prompts the points above name. "
        "Every other shot and prompt must be copied exactly, character for "
        "character: do not reword, renumber or reflow anything that was not "
        "criticised. If a fix renames an image file, change it in BOTH "
        "shots and images.")
    return "\n".join(lines)


def _changed_prompts(old: dict, new: dict) -> tuple[int, int]:
    """(images whose prompt changed or are new, images in the new plan)."""
    before = {i.get("file"): i.get("prompt")
              for i in (old.get("images") or []) if isinstance(i, dict)}
    imgs = [i for i in (new.get("images") or []) if isinstance(i, dict)]
    changed = sum(1 for i in imgs if before.get(i.get("file")) != i.get("prompt"))
    return changed, len(imgs)


def _corrected_plan(transport, writer: str, prev: dict, reply: str,
                    log, dump, rnd: int) -> dict:
    """A corrected plan from `reply`, logged with how much the writer moved
    (on every path, including after a lost-chat recovery)."""
    data = _collect_plan(transport, writer, reply, log, dump,
                         tag=f"plan_round{rnd + 1}")
    changed, total = _changed_prompts(prev, data)
    log(f"{writer} changed {changed} of {total} image prompts"
        + (" - a large rewrite" if total and changed > 0.25 * total else ""))
    return data


def _recovery_prompt(cfg, pid: int, plan_text: str, feedback: str):
    """A fresh writer chat that continues the work: the original planner
    prompt, the plan so far, and the reviewer's points."""
    def build(files):
        base = ep.shotlist_planner_prompt(cfg, pid, files)
        head = (
            "\n\n---\nCONTEXT: this is a continuation. You already wrote a "
            "shotlist for the task above (included below), and a reviewer "
            "found problems with it. Do NOT start from scratch: fix the "
            "problems and keep everything that was not criticised exactly as "
            "it was.\n\n")
        if files is None:
            body = "YOUR PREVIOUS SHOTLIST:\n" + plan_text + "\n\n"
        else:
            files.append({"name": "previous_shotlist.json",
                          "text": plan_text.strip() + "\n",
                          "about": "your previous shotlist"})
            body = ("YOUR PREVIOUS SHOTLIST is attached as "
                    "previous_shotlist.json.\n\n")
        return base + head + body + feedback
    return build


def run_shotlist(cfg, pid: int, transport, writer: str = "zai",
                 judge: str = "deepseek", rounds: int | None = MAX_ROUNDS,
                 log: Callable[[str], None] = print,
                 should_stop: Callable[[], bool] = lambda: False,
                 resume: bool = False, same_chats: bool = False) -> dict:
    ctx = ep._context(cfg, pid)
    plan = ep._plan_inputs(cfg, pid, ctx)
    cues = plan["cues"]

    def dump(name: str, text: str) -> None:
        _dump(cfg, pid, name, text)

    kept = (adopt_chats(cfg, pid, transport, {writer, judge}, log)
            if same_chats else set())
    data = _saved_plan(cfg, pid) if resume else None
    if data is not None:
        log(f"reusing the plan from the saved {writer} replies "
            f"(the writer is not asked again)")
    else:
        if resume:
            log("no readable saved plan - asking the writer as usual")
        reply, w_cont = _send_in(
            transport, writer,
            lambda f: ep.shotlist_planner_prompt(cfg, pid, f), log,
            writer in kept, ready=_has_json)
        data = _collect_plan(transport, writer, reply, log, dump)
    log(f"{writer}: plan received ({len(data.get('shots') or [])} shots, "
        f"{len(data.get('images') or [])} images)")

    last_problem = ""
    judge_open = False          # the judge keeps ONE chat, like the writer
    judge_cont = judge in kept  # ...and may start in the previous stage's
    best = None                     # (badness, data, problem): the plan to keep
    rnd = 0
    while True:
        rnd += 1
        text = json.dumps(data, ensure_ascii=False, indent=1)
        local: list[str] = []

        def build(f, follow=judge_open):
            # always run the full builder: it also yields the local checks
            prompt, loc = ep.shotlist_judge_prompt(
                cfg, pid, text, [] if follow else f)
            local[:] = list(loc or [])
            return _plan_followup(text, f) if follow else prompt

        log(f"round {rnd}: asking {judge} to review the whole plan"
            + (" (same chat)" if judge_open else ""))
        def send_judge():
            nonlocal judge_cont
            fresh = not (judge_open or judge_cont)
            try:
                return _send(transport, judge, build, log, new_chat=fresh,
                             ready=_is_json_verdict)
            except webchat.ChatLost:
                if judge_open or not judge_cont:
                    raise
                log(f"{judge}: the previous chat cannot be reopened - "
                    f"starting a new one")
                judge_cont = False
                return _send(transport, judge, build, log, new_chat=True,
                             ready=_is_json_verdict)

        raw = send_judge()
        dump(f"judge_reply_round{rnd}", raw)
        verdict = studio._parse_json_object(raw)
        if not verdict:
            log(f"{judge} gave no usable verdict - asking once more")
            raw = send_judge()
            verdict = studio._parse_json_object(raw)
        if verdict:
            judge_open = True
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
            try:
                got = transport.ask(writer, prompt, (), new_chat=False,
                                    ready=lambda t: bool(
                                        studio.parse_shotlist_patch(t)))
            except webchat.ChatLost:
                got = None
            if got is None:
                log(f"the {writer} chat is gone - starting a new one with "
                    f"the plan and the points to fix")
                reply = _send(transport, writer, _recovery_prompt(
                    cfg, pid, text, _plan_feedback(hard, weak, fixes, verdict, local)), log,
                    ready=_has_json)
                data = _corrected_plan(transport, writer, data, reply,
                                       log, dump, rnd)
                continue
            good, unknown, missing = studio.check_shotlist_patch(
                studio.parse_shotlist_patch(got), asked)
            if unknown or missing:
                log(f"patch check: {len(unknown)} unknown key(s), "
                    f"{len(missing)} asset(s) not answered")
            data = studio.apply_shotlist_patch(data, good)
        else:
            log(f"sending the faults back to {writer} for a corrected plan")
            prev = data
            try:
                reply = transport.ask(
                    writer, _plan_feedback(hard, weak, fixes, verdict, local),
                    (), new_chat=False, ready=_has_json)
            except webchat.ChatLost:
                log(f"the {writer} chat is gone - starting a new one with "
                    f"the plan and the points to fix")
                reply = _send(transport, writer, _recovery_prompt(
                    cfg, pid, text, _plan_feedback(hard, weak, fixes, verdict, local)), log,
                    ready=_has_json)
            data = _corrected_plan(transport, writer, prev, reply, log, dump,
                                   rnd)
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
               should_stop: Callable[[], bool] = lambda: False,
               options: dict | None = None) -> None:
    with web_transport(cfg, log, options) as t:
        log(f"settings: {t.options}")
        t.set_stop(should_stop)
        try:
            run_script(cfg, pid, t, writer, judge, log=log,
                       should_stop=should_stop)
        finally:
            save_chats(cfg, pid, t)


def shotlist_job(cfg, pid: int, writer: str, judge: str, log,
                 should_stop: Callable[[], bool] = lambda: False,
                 options: dict | None = None, resume: bool = False,
                 same_chats: bool = True) -> None:
    with web_transport(cfg, log, options) as t:
        log(f"settings: {t.options}")
        t.set_stop(should_stop)
        try:
            run_shotlist(cfg, pid, t, writer, judge, log=log,
                         should_stop=should_stop, resume=resume,
                         same_chats=same_chats)
        finally:
            save_chats(cfg, pid, t)
