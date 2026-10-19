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
import time
from pathlib import Path
from typing import Callable

from . import (autorun, briefs, db, external_prompts as ep, handover, skeleton, studio,
               webchat)

# Above this the prompt is split into attached files (external_prompts' files
# mode). Both chat boxes took 30k+ characters when tried by hand.
INLINE_MAX_CHARS = 60000
# sites that cannot take attachments from the automation: never switch to files
INLINE_ONLY_SITES = frozenset({"qwen"})
# None = no limit: the loop runs until the judge passes the work (or you stop
# it). Only an explicit `rounds=` caps it.
MAX_ROUNDS = None
SOFT_ROUNDS = 6          # an uncapped loop stops here and takes the best
SOFT_MARGIN = 1.0        # ... if it has no blocker and is within this of the bar
MAX_CONTINUES = 8
_CHROME_LINES = {"json", "copy", "download", "copy code", "code"}


class StageFailed(RuntimeError):
    """The loop ran out of rounds; the message says what was still wrong."""


# ---- transport ----------------------------------------------------------------

# what each site's mode switches default to when the run does not say
DEFAULT_OPTIONS = {"zai": {"thinking": "Low", "model": "flash"},
                   "deepseek": {"deepthink": True, "search": True},
                   **{key: {"deepthink": True, "search": True}
                      for key in webchat.DEEPSEEK_ACCOUNTS}}


NO_IMAGES_NOTE = ("\n\n(Reply with text only - do not create, draw or generate "
                  "any images.)")


class WebTransport:
    def __init__(self, chat: "webchat.WebChat", log: Callable[[str], None],
                 options: dict | None = None):
        self.chat, self.log = chat, log
        self.options = {k: dict(v) for k, v in DEFAULT_OPTIONS.items()}
        for site, opts in (options or {}).items():
            self.options.setdefault(site, {}).update(opts or {})

    def ask(self, site: str, prompt: str, files=(), new_chat: bool = True,
            ready=None):
        if site not in webchat.BUILTIN_KEYS and (prompt or "").strip():
            # ChatGPT draws an image when a prompt talks about thumbnails and
            # then runs into the free plan's image limit
            prompt = prompt.rstrip() + NO_IMAGES_NOTE
        # a judge's JSON verdict: a reply that stopped without it is given a
        # nudge after seconds, not after the 5 minute default
        extra = ({"ready_wait": JUDGE_READY_WAIT}
                 if ready in (_is_json_verdict, _is_script_verdict,
                              _is_shotlist_verdict) else {})
        t0 = time.monotonic()
        try:
            reply = self.chat.ask(site, prompt, list(files), new_chat=new_chat,
                                  options=self.options.get(site), ready=ready,
                                  log=self.log, **extra)
        except BaseException as exc:
            self.log(f"{site}: round trip failed after "
                     f"{time.monotonic() - t0:.0f}s ({type(exc).__name__})")
            raise
        # one line per exchange: a slow stage can be attributed to a site (or
        # the model's capacity wait) from the production log alone - the dumps
        # only record when a reply LANDED, never how long its ask took
        self.log(f"{site}: reply of {len(reply or ''):,} chars after "
                 f"{time.monotonic() - t0:.0f}s "
                 f"(prompt {len(prompt or ''):,} chars, "
                 f"{'new' if new_chat else 'same'} chat)")
        return reply

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


def _send_in(transport, site: str, build, log, continuing: bool, ready=None,
             force_files: bool = False):
    """_send in the saved chat when `continuing`; a chat that cannot be
    reopened falls back to a new one. Returns (reply, still_continuing)."""
    if continuing:
        try:
            return _send(transport, site, build, log, new_chat=False,
                         ready=ready, force_files=force_files), True
        except webchat.ChatLost:
            log(f"{site}: the previous chat cannot be reopened - starting a "
                f"new one")
    return _send(transport, site, build, log, new_chat=True,
                 ready=ready, force_files=force_files), False


def _send(transport, site: str, build: Callable, log,
          new_chat: bool = True, ready=None, uploads=(),
          force_files: bool = False):
    """Build the prompt inline; switch to attached files when it is too big
    (or when `force_files` asks for it: a style guide longer than the inline
    prompt can carry). `uploads` are extra files (e.g. inspiration images)
    always attached alongside the prompt."""
    text = build(None)
    files = None
    # Qwen's page ignores files set on its upload input (nothing shows up and
    # the run stalls), so everything goes in the message itself
    if ((len(text) > INLINE_MAX_CHARS or force_files)
            and site not in INLINE_ONLY_SITES):
        files = []
        text = build(files)
    tmp, paths = None, list(uploads)
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


def ensure_brief(cfg, pid: int, transport, who: str, log,
                 continuing: bool = False) -> tuple[str, bool]:
    """The production's keyword brief (<=100 words + keywords), asked from
    `who` - the chat that may see the original - when none is cached.
    Returns (brief text, whether a chat of `who` is now open)."""
    ctx = ep._context(cfg, pid)
    pdir = ctx["pdir"]
    source = autorun._source_transcript_text(cfg, pid)
    have = ep._read(pdir / autorun.RESEARCH_NOTES_FILE)
    if have and autorun._notes_cache_valid(pdir, have, source):
        return have, False
    if not source.strip():
        return have, False
    log(f"{who}: writing the keyword brief (topic + keywords, no names or "
        f"plot)")
    first = [True]

    def ask(prompt: str) -> str:
        if first[0]:
            first[0] = False
            return _send_in(transport, who, lambda f: prompt, log,
                            continuing)[0]
        return _send(transport, who, lambda f: prompt, log, new_chat=False)

    brief = autorun.make_brief(ask, source, ctx["prod"]["title"],
                               ctx["prod"]["genre"], log)
    autorun.save_brief(pdir, brief, source)
    log("keyword brief saved")
    return brief, True


# A judge that announces an audit ("I'll audit the key claims before
# scoring.") and then ends its turn has not answered. Give it a short nudge in
# the SAME chat instead of waiting minutes and re-sending the whole prompt.
JUDGE_READY_WAIT = 45.0
JUDGE_NUDGE = ("Your last reply had no JSON verdict. Do any checking you "
               "still need NOW, in this same reply, and finish with ONLY the "
               "JSON verdict in the format you were given.")


def _nudge_judge(transport, judge: str, log, ready=None) -> str:
    log(f"{judge}: no verdict yet - asking for it in the same chat")
    return _send(transport, judge, lambda f: JUDGE_NUDGE, log,
                 new_chat=False, ready=ready or _is_json_verdict)


def _is_json_verdict(text: str) -> bool:
    return bool(studio._parse_json_object(text))


def _is_script_verdict(text: str) -> bool:
    """A script judge's verdict: a JSON object with a score. Any other JSON
    on the page (an older answer, the format example) is not one."""
    d = studio._parse_json_object(text)
    return bool(d) and "score" in d


def _is_shotlist_verdict(text: str) -> bool:
    """A shotlist judge's verdict: a JSON object with faults and pass."""
    d = studio._parse_json_object(text)
    return bool(d) and "faults" in d and "pass" in d


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
    from . import textfiles
    pdir = studio.prod_dir(cfg, pid)
    textfiles.write_text(pdir / "script.md", text.strip() + "\n")
    from . import script_readiness
    script_readiness.ensure(pdir)
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

def _wc(text: str) -> int:
    """Word count exactly as the manual external prompts measure it."""
    return len(re.findall(r"\w+", text or ""))


def _length_delta(words: int, target: int) -> str:
    lo, hi = ep._length_window(target)
    if words > hi:
        return (f"Your draft was {words} words - {words - hi} over the "
                f"maximum of {hi}. Cut at least {words - target} words "
                f"(drop repetition and side points, keep the brief's keywords).")
    if words < lo:
        return (f"Your draft was {words} words - {lo - words} under the "
                f"minimum of {lo}. Add about {target - words} words using "
                f"depth on points you have not unpacked yet.")
    return f"Your draft was {words} words, inside {lo}-{hi}."


def _script_feedback(words: int, target: int, reasons: list[str],
                     judged: dict, truncated: bool,
                     direction: str = "") -> str:
    lo, hi = ep._length_window(target)
    lines = [f"The editor did NOT accept this draft. Why: "
             f"{'; '.join(reasons) or 'see below'}."]
    if truncated:
        lines.append("The draft looks cut off - it must end on a complete "
                     "sentence.")
    if judged.get("must_fix"):
        lines.append("MANDATORY corrections (the script cannot be accepted "
                     "until each is fixed):")
        lines += [f"- {m}" for m in judged["must_fix"]]
    if judged.get("feedback"):
        lines.append("Editor's feedback (optional polish - keep what works):")
        lines += [f"- {f}" for f in judged["feedback"]]
    if judged.get("weak_spans"):
        lines.append("Passages the editor flagged as weak:")
        lines += [f"- {w}" for w in judged["weak_spans"]]
    lines.append(
        f"Rewrite the COMPLETE script, fixing every point above and keeping "
        f"what the editor did not criticise. {_length_delta(words, target)} "
        f"It must be {lo}-{hi} words (target {target}); count before you "
        f"reply. Keep working in the brief's keywords; the story, research "
        f"and wording stay your own. Same reply format as before: the finished script "
        f"as plain text only, no notes before or after it."
        + _creator_block(direction, "YOU, THE WRITER"))
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
    soft = rounds is None
    if soft:
        rounds = SOFT_ROUNDS
    hand = handover.for_production(cfg, ctx["prod"], "script")
    log(f"hand-over template for the script: {hand['name']}")
    writer_dir = handover.instruction(cfg, ctx["prod"], "script")
    judge_dir = handover.instruction(cfg, ctx["prod"], "script_judge")
    if writer_dir or judge_dir:
        log(f"instructions in force - writer: {len(writer_dir)} chars, "
            f"judge: {len(judge_dir)} chars (repeated every round)")

    def long_enough(text: str) -> bool:        # not just "Thinking..." / a stub
        return _wc(text) >= 0.5 * target

    brief_text, _chat = ensure_brief(cfg, pid, transport, judge, log)

    # a style guide over the inline limit is attached whole, never cut
    attach_style = ep.style_needs_file(cfg, pid)
    if attach_style:
        size = len(ep._writing_style(ctx))
        for who, what in ((writer, "writer"), (judge, "judge")):
            log(f"the style guide is {size:,} characters: "
                + (f"written in full into the {what}'s message ({who} takes "
                   f"no attachments)" if who in INLINE_ONLY_SITES else
                   f"attached as a file for the {what} ({who})"))

    reply = _send(transport, writer,
                  lambda f: ep.script_writer_prompt(cfg, pid, title, None,
                                                    None, None, f,
                                                    full_style=writer in INLINE_ONLY_SITES), log,
                  ready=long_enough, force_files=attach_style)
    attempts = []
    rnd = 0
    judge_open = False          # the judge keeps ONE chat, like the writer
    while True:
        rnd += 1
        script = _clean_script(reply)
        words = _wc(script)
        if words < 20:
            raise StageFailed(
                f"{writer} did not return a usable script (got {words} "
                f"word(s): \"{script[:80]}\"). Nothing was sent to the "
                f"judge.")
        overlap = studio.overlap_ratio(script, source) if source.strip() else 0.0
        _dump(cfg, pid, f"script_draft_round{rnd}", script)
        log(f"round {rnd}: draft of {words} words (target {target}), "
            f"overlap {overlap:.1%} - asking {judge} to judge it")
        struct0 = (studio.structure_copy(script, source) if source.strip()
                   else None)
        for attempt in (1, 2):      # a judge reply with no score is asked again
            if attempt == 2:
                raw = _nudge_judge(transport, judge, log, _is_script_verdict)
            elif judge_open:
                raw = _send(transport, judge,
                            lambda f: _script_followup(script, words, target, overlap,
                                                       studio.script_looks_truncated(script),
                                                       struct0, judge_dir), log,
                            new_chat=False, ready=_is_script_verdict)
            else:
                raw = _send(transport, judge,
                            lambda f: ep.script_judge_prompt(
                                cfg, pid, script, title, None, None, None, f,
                                full_style=judge in INLINE_ONLY_SITES),
                            log, ready=_is_script_verdict,
                            force_files=attach_style)
            _dump(cfg, pid, f"script_judge_reply_round{rnd}", raw)
            verdict = studio._parse_json_object(raw, want=("score",))
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
        # what reaches the writer follows the production's hand-over template
        must, unver = studio.verdict_blockers(verdict)
        (judged["must_fix"], judged["feedback"], judged["weak_spans"],
         gone) = handover.script_notes(hand, must, judged["feedback"],
                                       judged["weak_spans"], script, source,
                                       allow=brief_text)
        if gone:
            log(f"dropped {gone} judge note(s) that carried details of the "
                f"original")
        sent = (judged["must_fix"] + judged["feedback"]
                + judged["weak_spans"])
        if sent:
            log(f"round {rnd}: points for the writer, in this order: "
                + " | ".join(f"{i}. {' '.join(n.split())[:60]}"
                             for i, n in enumerate(sent, 1)))
        if unver:
            log(f"{judge} left {unver} claim(s) unverified")
        err = None if score is not None else (
            "no usable score in the judge's reply: "
            + " ".join(str(raw or "").split())[:120])
        passed, reasons, too_long, too_short = autorun._script_gate(
            words, target, overlap, score, min_rating, max_overlap,
            hard_overlap, err)
        cut = studio.script_looks_truncated(script)
        struct = (studio.structure_copy(script, source) if source.strip()
                  else {"flag": False})
        if source.strip():
            log(f"round {rnd}: structure vs source - "
                f"{struct.get('matched', 0):.0%} of sentences echo the "
                f"source, order {struct.get('order', 0):.0%}"
                + (" - TOO CLOSE" if struct["flag"] else ""))
        if struct["flag"]:
            passed = False
            reasons.append(studio.structure_reason(struct))
            judged["feedback"].append(studio.structure_reason(struct))
        if judged.get("must_fix"):
            passed = False
            reasons.append(f"{len(judged['must_fix'])} factual correction(s) "
                           f"required by the judge")
        from . import plan as _plan
        meta = _plan.meta_mentions(script)
        if meta:
            passed = False
            reasons.append("the script mentions the video's packaging ("
                           + ", ".join(meta) + ") - remove every mention; "
                           "narration never refers to the thumbnail or title")
            judged["feedback"].append(
                "Remove every mention of the " + "/".join(meta)
                + ": the script is spoken narration and must never refer to "
                "the thumbnail, cover or title.")
        promo = _plan.promo_mentions(script)
        if promo:
            passed = False
            reasons.append("the script contains a sponsor/ad/promotion ("
                           + ", ".join(promo) + ") - remove it; no sponsor "
                           "read, ad, course or product promotion, code, QR "
                           "code or link at any point of the video")
            judged["feedback"].append(
                "Rewrite so the narration does not read as an ad: no sponsor read, "
                "course or product promotion, discount code, QR code or link "
                "(found: " + ", ".join(promo)
                + "). The only call to action is the closing "
                "subscribe/like/next-video one.")
        if cut:
            passed = False
            reasons.append("the script looks cut off")
        if too_long:
            passed = False
            reasons.append(f"{words} words is over the "
                           f"{ep._length_window(target)[1]}-word maximum "
                           f"(target {target})")
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
                         "must_fix": judged.get("must_fix") or [],
                         "struct": bool(struct.get("flag")),
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
        to_writer = _script_feedback(words, target, reasons, judged, cut,
                                     writer_dir)
        _dump(cfg, pid, f"script_to_writer_round{rnd}", to_writer)
        reply = transport.ask(
            writer, to_writer, (),
            new_chat=False, ready=long_enough)
    best = max(attempts, key=lambda a: (not a["too_short"],
                                        not a["too_long"], not a["cut"],
                                        a["score"]))
    if soft and attempts and not should_stop():
        # the round cap was reached: a draft with no mandatory fix, a sound
        # length and ending, no copying and a score near the bar is taken
        ok = [a for a in attempts
              if not a["must_fix"] and not a["cut"] and not a["too_long"]
              and not a["too_short"] and not a.get("struct")
              and a["overlap"] <= hard_overlap and a["score"]
              and a["score"] >= min_rating - SOFT_MARGIN]
        if ok:
            pick = max(ok, key=lambda a: a["score"])
            save_script(cfg, pid, pick["script"],
                        f"web chat {writer}/{judge}: best of {rounds} "
                        f"rounds, score {pick['score']} (bar "
                        f"{min_rating:g}), no mandatory fixes",
                        advance=True)
            log(f"round cap reached: took the best draft (score "
                f"{pick['score']}, no mandatory fixes); next stage unlocked")
            return pick["script"]
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


def _creator_block(text: str, who: str) -> str:
    """The creator's typed instructions, repeated in every later message so a
    long chat does not drift away from them."""
    text = (text or "").strip()
    return (f"\n\nCREATOR'S INSTRUCTIONS FOR {who} (still in force - follow "
            f"them to the letter, they win over the defaults on what you "
            f"write, how it is worded and the order of your points):\n"
            f"{text}" if text else "")


def _script_followup(script: str, words: int | None = None,
                     target: int | None = None, overlap: float | None = None,
                     cut: bool = False, struct: dict | None = None,
                     direction: str = "") -> str:
    measured = ""
    if words is not None and target:
        lo, hi = ep._length_window(target)
        measured = (f"Measured by software, do not re-estimate: length "
                    f"{words} words (the bar is {lo}-{hi}, target {target}"
                    f") - "
                    f"{'INSIDE' if lo <= words <= hi else 'OUTSIDE'} the "
                    f"window"
                    + (f"; overlap {overlap:.1%}" if overlap is not None
                       else "")
                    + f"; ending {'LOOKS CUT OFF' if cut else 'is complete'}"
                    f". A script outside the length window fails the bar "
                    f"however well it reads - list it among the failed "
                    f"items.\n"
                    + (f"Story order vs the source (measured): "
                       f"{struct['matched']:.0%} of the script's sentences "
                       f"echo a source sentence, {struct['order']:.0%} of "
                       f"those in the source's order"
                       f"{' - TOO CLOSE, fail it' if struct['flag'] else ''}"
                       f". Judge beat by beat whether the story order is "
                       f"the writer's own.\n" if struct else "")
                    + "\n")
    return measured + (
        "The writer revised the script after your review. Judge it again "
        "under the SAME rules and reply in exactly the SAME JSON format as "
        "before. First check whether each point you raised earlier is now "
        "addressed, then re-read the COMPLETE revised script for material "
        "errors (do not carry the earlier approval forward blindly). Keep "
        "the same severity rules: a NEW must_fix only for a real error that "
        "would mislead the viewer or contradict the script, at most 3 in "
        "total; new polish goes in feedback and never blocks. Fill "
        "\"claims\" and \"must_fix\" again. "
        "Source material and "
        "rules are unchanged (use the ones from earlier in this chat), "
        "including: the script must never mention the thumbnail, cover, "
        "title or packaging - fail it and quote the sentence if it does."
        + _creator_block(direction, "YOU, THE JUDGE") + "\n\n"
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


def _skeleton_plan(cfg, pid: int, ctx: dict, plan: dict, transport,
                   writer: str, log, dump, continuing: bool, judge: str = "",
                   should_stop: Callable[[], bool] = lambda: False,
                   judge_state: dict | None = None) -> dict:
    """Group raw narration cues, get semantic approval, then write pictures.

    The app expands each picture into timed camera shots and assembles JSON.
    Unsupported modes may use the legacy planner; rejected groupings stop.
    """
    eff, profile, cues = ctx["eff"], plan["profile"], plan["cues"]
    why = skeleton.unsupported_reason(eff, profile)
    if why:
        raise skeleton.SkeletonUnsupported(why)
    template_text = skeleton._template(cfg)
    note = ep._animation_note(eff, cues)
    anim_last = 0
    if note:
        from . import motion
        anim_last = motion.window_last_cue(
            cues, eff["animate_seconds"], studio._srt_seconds)
    shots = skeleton.build(cues, profile, plan["max_hold"], anim_last)
    if not shots:
        raise skeleton.SkeletonError("no shots could be drafted")
    allow_refs = plan["allow_refs"]
    supplied = studio.find_supplied_refs(ctx["pdir"]) if allow_refs else []
    stems = {n.rsplit(".", 1)[0] for n in supplied}
    density = studio.visual_density(cfg, pid=pid)
    style_text = ctx["visual_style"]
    limit = skeleton.prompt_limit(style_text)
    rules = skeleton.rules_text(profile, allow_refs, supplied, note,
                                density=density, limit=limit,
                                template_text=template_text, cues=cues)
    # the presentation text gets the same framing the brief gives it (it only
    # decides WHO is on screen; the bible supplies the named characters), so
    # the line-by-line writer reads it exactly like the whole-JSON writer
    direction = "\n\n".join(x.strip() for x in (
        briefs.presentation_block(eff.get("brief_presentation") or ""),
        db.stage_extra(ctx["prod"], "shots"))
        if x.strip())
    # a beat (one picture) may hold what a shot may: the channel's max hold
    # with its tolerance, the same limit the code checks use
    beat_cap = briefs.hold_limit(plan["max_hold"], profile.max_hold_tol)
    judge_state = judge_state if judge_state is not None else {}
    # Group raw cues by meaning, review them, then ask for pictures.
    chat_open, grouped = False, False
    pictures = shots
    grouping_cues = skeleton.groupable(cues, anim_last)
    if skeleton.GROUPING and grouping_cues:
        if not judge or judge == writer:
            raise skeleton.SkeletonUnsupported(
                "cue grouping needs a separate judge chat "
                "(writer and judge are the same site)")
        try:
            reply, _ = _send_in(
                transport, writer,
                lambda f: skeleton.group_prompt(
                    ctx["prod"]["title"], ctx["prod"]["genre"], grouping_cues,
                    plan["max_hold"], anim_last, direction,
                    hold_note=skeleton.hold_range_note(profile, cues)),
                log, continuing, ready=lambda t: bool(skeleton.parse_groups(t)))
            chat_open = True
            if should_stop():
                raise StageFailed("Cue grouping stopped by user")
            dump("skeleton_groups_1", reply)
            written, beat_starts = skeleton.parse_grouping(reply)
            left_out = skeleton.uncovered_share(grouping_cues, written)
            if left_out > skeleton.REPAIR_MAX_SHARE:
                raise StageFailed(
                    f"Cue grouping needs attention: the writer left "
                    f"{left_out:.0%} of the cues out of its groups")
            groups, bad = skeleton.repair_groups(grouping_cues, written, beat_cap)
            if bad:
                raise StageFailed("Cue grouping needs attention: " + "; ".join(bad[:3]))
            beat_starts, moved = skeleton.snap_beat_starts(beat_starts, groups)
            if moved:
                log(f"grouping: the repair moved {moved} chapter marker(s) - "
                    f"snapped to the nearest group start")
            if groups != written:
                log(f"grouping: the app repaired the writer's groups "
                    f"({len(set(groups) - set(written))} group(s) changed, "
                    f"{left_out:.0%} of the cues were not covered)")
            raw, _ = _send_in(
                transport, judge,
                lambda f: skeleton.group_judge_prompt(
                    grouping_cues, groups, plan["max_hold"], direction,
                    hold_note=skeleton.hold_range_note(profile, cues)),
                log, bool(judge_state.get("continuing")),
                ready=lambda t: bool(skeleton.parse_groups(t)))
            judge_state["continuing"] = True
            dump("skeleton_groups_judge_1", raw)
            if should_stop():
                raise StageFailed("Cue grouping stopped by user")
            judged, judged_beats = skeleton.parse_grouping(raw)
            judge_bad = skeleton.group_problems(grouping_cues, judged, beat_cap)
            if judge_bad:
                log(f"grouping: the judge's list is not usable ({judge_bad[0]}) - "
                    f"keeping the writer's grouping")
            else:
                changed = len(set(judged) - set(groups))
                groups = judged
                if judged_beats:
                    judged_beats, moved = skeleton.snap_beat_starts(
                        judged_beats, groups)
                    if moved:
                        log(f"grouping: the judge moved {moved} chapter "
                            f"marker(s) off a group start - snapped to the "
                            f"nearest group start")
                    beat_starts = judged_beats
                log(f"grouping: the judge's list changed {changed} group(s)"
                    if changed else "grouping: the judge approved the groups")
            # main beats come from the grouping's BEAT markers; without any
            # marker the scenes fall back to the writer's own BEAT lines in
            # the picture replies (the old optional-instruction behaviour)
            starts = {a for a, _b in groups}
            beat_starts = {s for s in beat_starts if s in starts}
            # the first group starts the first chapter implicitly - writers
            # sometimes mark it anyway, which would count as a second
            # chapter and start the files at S02
            beat_starts.discard(groups[0][0])
            dump("skeleton_groups_applied", "\n".join(f"{a}-{b}" for a, b in groups))
            shots, pictures = skeleton.regroup(cues, groups, profile,
                                               plan["max_hold"], anim_last)
            picture_scenes = None
            if beat_starts:
                ordered = sorted(beat_starts)
                # every picture belongs to the chapter of the last BEAT
                # marker at or before it; the first chapter starts at the
                # first picture (count + 1)
                scene_of_picture = {
                    p.n: 1 + sum(1 for s in ordered if s <= p.first)
                    for p in pictures}
                picture_scenes = {m: sc for p in pictures
                                  for m in p.members
                                  for sc in (scene_of_picture[p.n],)}
                log(f"main beats: {len(ordered) + 1} scene(s) from the "
                    f"grouping")
            else:
                # no markers: the old BEAT-line instruction in the picture
                # prompt is what produced S13_01...S13_54, so the app splits
                # the pictures into even chapters instead and the picture
                # prompt keeps its "do not write BEAT lines" rule
                chunk = max(1, skeleton.MAX_SUBBEATS_PER_SCENE)
                picture_scenes = {}
                for p in pictures:
                    sc = 1 + (p.n - 1) // chunk
                    for m in p.members:
                        picture_scenes[m] = sc
                n_scenes = 1 + (len(pictures) - 1) // chunk
                log(f"no chapter markers in the grouping - the app split the "
                    f"{len(pictures)} pictures into {n_scenes} even scene(s) "
                    f"(writer BEAT lines are not asked for)")
            grouped = True
            rules = skeleton.rules_text(profile, allow_refs, supplied, note,
                                        density=density, limit=limit,
                                        grouped=True,
                                        template_text=template_text,
                                        cues=cues,
                                        beats_from_grouping=bool(picture_scenes))
            log(f"{writer}: grouped {len(grouping_cues)} cues into "
                f"{len(groups)} picture groups; {len(shots)} camera shots")
        except webchat.ChatLost as exc:
            raise StageFailed("Chat lost during cue grouping; no unreviewed grouping was used") from exc
    batches = skeleton.chunks(pictures)
    total = len(pictures)
    log(f"{writer}: asking for the pictures - {total} pictures, "
        f"{len(shots)} camera shots in {len(batches)} part(s)")
    ans = skeleton.Answers()
    for i, batch in enumerate(batches):
        ready = (lambda t, b=batch: skeleton.covers(t, b))
        try:
            if i == 0 and chat_open:
                reply = _send(
                    transport, writer,
                    lambda f: skeleton.first_prompt(
                        rules, ctx["visual_style"], ctx["bible"], direction,
                        batch, total, grouped),
                    log, new_chat=False, ready=ready)
            elif i == 0:
                reply, _ = _send_in(
                    transport, writer,
                    lambda f: skeleton.first_prompt(
                        rules, ctx["visual_style"], ctx["bible"], direction,
                        batch, total),
                    log, continuing, ready=ready)
            else:
                reply = _send(transport, writer,
                              lambda f: skeleton.next_prompt(batch, total),
                              log, new_chat=False, ready=ready)
            dump(f"skeleton_reply_{i + 1}", reply)
            ans.update(skeleton.parse_reply(reply))
            nums = {s.n for s in batch}
            for attempt in range(2):
                bad = [p for p in skeleton.problems(
                           pictures, ans, profile, allow_refs, stems, only=nums,
                                        style=style_text)]
                if not bad:
                    break
                log(f"{writer}: part {i + 1} - {len(bad)} shot(s) to fix "
                    f"({bad[0]})")
                fixed = _send(
                    transport, writer, lambda f: skeleton.fix_prompt(
                        bad, [s for s in batch
                              if f"shot {s.n}:" in " ".join(bad)]),
                    log, new_chat=False,
                    ready=lambda t: bool(skeleton.parse_reply(t).answered()
                                         or skeleton.parse_reply(t).refs))
                dump(f"skeleton_reply_{i + 1}_fix{attempt + 1}", fixed)
                ans.update(skeleton.parse_reply(fixed))
            else:
                bad = skeleton.problems(pictures, ans, profile, allow_refs,
                                        stems, only=nums,
                                        style=style_text)
            if bad:
                raise skeleton.SkeletonError(
                    f"part {i + 1} still has problems: {'; '.join(bad[:3])}")
        except webchat.ChatLost as exc:
            raise skeleton.SkeletonError(f"the chat was lost ({exc})") from exc
        log(f"{writer}: part {i + 1}/{len(batches)} done "
            f"({len(ans.new)} image(s), {len(ans.reuse)} reused so far)")
    if grouped:
        ans = skeleton.expand(pictures, ans)
    data = skeleton.assemble(shots, ans, ctx["visual_style"], supplied,
                             allow_refs, profile,
                             scenes=picture_scenes if grouped else None)
    dump("plan_reply_1", json.dumps(data, ensure_ascii=False, indent=1))
    return data


# faults the app's own code checks already decide (with the tolerance): when
# those checks came back clean, the judge's arithmetic on them is wrong
# Whatever label the judge puts in front ("MAXIMUM HOLD", "MAXIMUM ST HOLD",
# "MOTION MIX: ... is ST and holds 5s, over the 4.8s limit"), a fault that
# does hold-length arithmetic ("holds/holding N s ... over/exceeds ...
# limit/cap") is one the app's own check already decided. Judges have
# phrased it both ways.
_CODE_DECIDED = re.compile(
    r"^\s*(?:(?:MAXIMUM (?:ST |STATIC )?HOLD|STATIC HOLD|ST HOLD)\b"
    r"|.*?\bhold(?:s|ing)?\s+\d+(?:\.\d+)?\s*s\b[^;]*?"
    r"\b(?:over|exceed\w*|above|>)\b[^;]*?\b(?:limit|cap|maximum)\b)", re.I)


# The pre-check runs the app's whole mechanical battery (coverage, assets,
# holds with tolerance, motion mix and codes) BEFORE the judge sees the plan
# - a judge round only happens when all of that is clean. So a judge fault
# carrying one of these labels re-derives what the code already measured.
# Prod 40: deepseek returned seven in a row ("COVERAGE: shot with cues
# 488-491 is followed by 492-496, but ... No—checking order: ..."), each
# answering its own question, none true - the app's check found nothing.
_MECHANICAL_LABELS = re.compile(
    r"^\s*(?:COVERAGE|ASSETS|MAXIMUM (?:ST |STATIC )?HOLD|STATIC HOLD"
    r"|ST HOLD|MOTION MIX|MOTION CODES)\b", re.I)


def _false_hold_fault(fault: str, data: dict, cues: list[dict],
                      profile) -> bool:
    """True when a judge fault blames a shot's hold length the app measures
    under the limit - the judge summed rounded narration durations instead
    of the cue span (once: 1.68 + 2.74 + 0.26 = 4.68s read as "holding 6.0s,
    exceeding the 5.0s ST hold cap"). The app's own measurement decides: a
    fault that cites a real over-limit hold stays (the return is False), a
    fault that cites a shot the app measures within its limit is dropped."""
    m = re.search(r"\bhold(?:s|ing)?\s+(\d+(?:\.\d+)?)\s*s\b", fault, re.I)
    if not m:
        return False
    shots = [s for s in (data.get("shots") or [])
             if isinstance(s, dict) and s.get("asset")
             and str(s["asset"]) in fault]
    if len(shots) != 1:                      # no / several candidates: unsure
        return False
    rng = studio.cue_range(shots[0].get("cues"))
    by_index = {c["index"]: c for c in cues}
    if not rng or rng[0] not in by_index or rng[1] not in by_index:
        return False
    span = (studio._srt_seconds(by_index[rng[1]]["end"])
            - studio._srt_seconds(by_index[rng[0]]["start"]))
    motion = (shots[0].get("motion") or "").upper()
    if motion == "ST" and profile.st_max_hold is not None:
        limit = briefs.hold_limit(profile.st_max_hold, profile.st_hold_tol)
    elif profile.max_hold is not None:
        limit = briefs.hold_limit(profile.max_hold, profile.max_hold_tol)
    else:
        return False
    return span <= limit + 1e-9


def _auto_split_long(data: dict, cues: list[dict], limit: float) -> int:
    """Split every shot held longer than `limit` seconds in two at the cue
    nearest its middle; both halves keep the picture and motion. Returns how
    many shots were split. Used instead of asking the writer to rewrite a
    plan it has never seen as JSON (line-by-line flow)."""
    by_index = {c["index"]: c for c in cues}
    out, split = [], 0
    for shot in data.get("shots") or []:
        rng = studio.cue_range(shot.get("cues")) if isinstance(shot, dict) else None
        if not rng or rng[0] not in by_index or rng[1] not in by_index:
            out.append(shot)
            continue
        a, b = rng
        start = studio._srt_seconds(by_index[a]["start"])
        hold = studio._srt_seconds(by_index[b]["end"]) - start
        if hold <= limit + 1e-9 or b <= a:
            out.append(shot)
            continue
        mid = start + hold / 2
        at = min((c for c in range(a + 1, b + 1) if c in by_index),
                 key=lambda c: abs(studio._srt_seconds(by_index[c]["start"]) - mid))
        first, second = dict(shot), dict(shot)
        first["cues"] = str(a) if at - 1 == a else f"{a}-{at - 1}"
        second["cues"] = str(at) if at == b else f"{at}-{b}"
        out += [first, second]
        split += 1
    data["shots"] = out
    return split


def run_shotlist(cfg, pid: int, transport, writer: str = "zai",
                 judge: str = "deepseek", rounds: int | None = MAX_ROUNDS,
                 log: Callable[[str], None] = print,
                 should_stop: Callable[[], bool] = lambda: False,
                 resume: bool = False, same_chats: bool = False,
                 use_skeleton: bool = False) -> dict:
    ctx = ep._context(cfg, pid)
    plan = ep._plan_inputs(cfg, pid, ctx)
    cues = plan["cues"]

    def dump(name: str, text: str) -> None:
        _dump(cfg, pid, name, text)

    kept = (adopt_chats(cfg, pid, transport, {writer, judge}, log)
            if same_chats else set())
    data = _saved_plan(cfg, pid) if resume else None
    grouping_judge_state = {"continuing": judge in kept}
    skeleton_plan = False       # the writer only ever saw numbered shots
    if data is not None:
        log(f"reusing the plan from the saved {writer} replies "
            f"(the writer is not asked again)")
    else:
        if resume:
            log("no readable saved plan - asking the writer as usual")
        tried = False
        if use_skeleton:
            tried = True
            try:
                data = _skeleton_plan(cfg, pid, ctx, plan, transport, writer,
                                      log, dump, writer in kept, judge, should_stop,
                                      grouping_judge_state)
                skeleton_plan = True
            except skeleton.SkeletonError as exc:
                # a channel the skeleton cannot plan (reveal, sound effects,
                # text overlays, same writer and judge, a missing custom
                # brief) always falls back to the whole-JSON flow; a failure
                # MID-run only falls back on a reveal channel (its legacy
                # behaviour) - otherwise the run stops rather than paying for
                # a second full plan after the first one already did work
                if isinstance(exc, skeleton.SkeletonUnsupported) \
                        or getattr(plan["profile"], "reveal", False):
                    data = None
                    log(f"line-by-line plan not used ({exc}) - asking for "
                        f"the whole shotlist as before")
                else:
                    raise StageFailed(
                        f"Skeleton planning could not complete: {exc}. "
                        "The full-shotlist planner was not started.") from exc
        if data is None:
            reply, w_cont = _send_in(
                transport, writer,
                lambda f: ep.shotlist_planner_prompt(cfg, pid, f), log,
                writer in kept and not tried, ready=_has_json)
            data = _collect_plan(transport, writer, reply, log, dump)
    log(f"{writer}: plan received ({len(data.get('shots') or [])} shots, "
        f"{len(data.get('images') or [])} images)")

    last_problem = ""
    judge_open = False          # the judge keeps ONE chat, like the writer
    # Send full image-review instructions, but retain the grouping chat.
    judge_cont = bool(grouping_judge_state.get("continuing"))
    best = None                     # (badness, data, problem): the plan to keep
    rnd = 0
    prev_text, same_run = None, 0   # the writer handing back the SAME plan
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

        # CODE CHECKS FIRST: the same mechanical checks the judge prompt
        # carries (cue coverage, holds over the channel cap, motion mix, the
        # animated-opening window, invented refs) run in the app for free.
        # When code already knows the plan is broken, sending it to the judge
        # wastes a ~2-3 minute browser round trip to hear the same thing back;
        # the writer fixes the code faults immediately and the judge only sees
        # a plan that could pass. A pre-check that itself errors falls through
        # to the normal judge path rather than killing the run.
        try:
            _, pre = ep.shotlist_judge_prompt(cfg, pid, text, [])
            pre = [str(x) for x in (pre or [])]
        except (ep.PromptError, RuntimeError, ValueError):
            pre = []
        if pre:
            log(f"round {rnd}: {len(pre)} code-check fault(s) - straight "
                f"back to {writer} without a judge round")
            verdict, faults, weak, fixes = None, pre, [], []
            hard = list(pre)
            last_problem = "; ".join(hard[:3])
            badness = len(hard) * 3
        else:
            log(f"round {rnd}: asking {judge} to review the whole plan"
                + (" (same chat)" if judge_open else ""))
            def send_judge():
                nonlocal judge_cont
                fresh = not (judge_open or judge_cont)
                try:
                    return _send(transport, judge, build, log, new_chat=fresh,
                                 ready=_is_shotlist_verdict)
                except webchat.ChatLost:
                    if judge_open or not judge_cont:
                        raise
                    log(f"{judge}: the previous chat cannot be reopened - "
                        f"starting a new one")
                    judge_cont = False
                    return _send(transport, judge, build, log, new_chat=True,
                                 ready=_is_shotlist_verdict)

            raw = send_judge()
            dump(f"judge_reply_round{rnd}", raw)
            verdict = studio._parse_json_object(raw, want=("pass", "faults"))
            if not verdict:
                log(f"{judge} gave no usable verdict - asking once more")
                try:
                    raw = _nudge_judge(transport, judge, log,
                                       _is_shotlist_verdict)
                except webchat.ChatLost:
                    raw = send_judge()
                verdict = studio._parse_json_object(
                    raw, want=("pass", "faults"))
            if verdict:
                judge_open = True
            if not verdict:
                last_problem = f"{judge} never returned a usable verdict"
                break
            faults = [str(x) for x in (verdict.get("faults") or [])]
            # the code checks above came back clean (with the tolerance), so
            # the judge's own hold arithmetic is the thing that is wrong:
            # recognise the phrasings and re-measure the shot the fault names
            dropped = [f for f in faults if _CODE_DECIDED.match(f)
                       or _false_hold_fault(f, data, cues, plan["profile"])
                       or _MECHANICAL_LABELS.match(f)]
            if dropped:
                faults = [f for f in faults if f not in dropped]
                log(f"ignored {len(dropped)} judge fault(s): the app's own "
                    f"checks (with tolerance) measure the plan clean, and a "
                    f"mechanical category re-derived here cannot beat that")
            hard = local + [f for f in faults if f not in local]
            weak = _weak_list(verdict)
            fixes = [str(x) for x in (verdict.get("fixes") or [])]
            if dropped:
                fixes = [x for x in fixes if not re.match(
                    r"\s*(split|fix static|(change|switch)\b.*\bfrom ST\b)",
                    x, re.I)]
            passed = (verdict.get("pass") is True
                      or (bool(dropped) and not faults and not weak)) and not hard
            ratio = verdict.get("detailed_ratio")
            log(f"round {rnd}: {len(hard)} fault(s), {len(weak)} weak shot(s), "
                f"detailed {ratio} - "
                + ("PASSED" if passed else "not accepted"))
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
        # the writer returning the same plan again and again cannot fix what
        # is still flagged: stop instead of looping for hours
        same_run = same_run + 1 if text == prev_text else 0
        prev_text = text
        if same_run >= 2:
            log(f"{writer} sent back the same plan {same_run + 1} rounds in a "
                f"row - stopping; still flagged: {last_problem}")
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
            if got is None and skeleton_plan:
                raise StageFailed(
                    f"The {writer} chat is gone, so the line-by-line plan "
                    f"cannot be continued (a new chat would start from the "
                    f"beginning). Open the chat again or rerun the shotlist.")
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
        elif skeleton_plan:
            # the writer never saw this plan as JSON: no rewrite request and
            # no new chat. The app cuts over-long shots itself; anything else
            # is reported instead of restarting the work from the beginning.
            n = _auto_split_long(data, cues, briefs.hold_limit(
                plan["max_hold"], plan["profile"].max_hold_tol))
            if not n:
                last_problem = "; ".join(hard[:3])
                log(f"faults the app cannot fix itself: {last_problem}")
                break
            log(f"the app split {n} over-long shot(s) itself - checking again")
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
                 same_chats: bool = True, use_skeleton: bool = True) -> None:
    with web_transport(cfg, log, options) as t:
        log(f"settings: {t.options}")
        t.set_stop(should_stop)
        try:
            run_shotlist(cfg, pid, t, writer, judge, log=log,
                         should_stop=should_stop, resume=resume,
                         same_chats=same_chats, use_skeleton=use_skeleton)
        finally:
            save_chats(cfg, pid, t)
