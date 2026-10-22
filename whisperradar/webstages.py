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

from . import autorun, beats as beatsmod, briefs, db, external_prompts as ep, handover, studio, webchat

# Above this the prompt is split into attached files (external_prompts' files
# mode). Both chat boxes took 30k+ characters when tried by hand.
INLINE_MAX_CHARS = 60000
# sites that cannot take attachments from the automation: never switch to files.
# ChatGPT: the attached narration.txt was not seen by the chat (it answered
# "Missing: narration.txt" on every run), while the same prompt pasted into the
# message worked - so its prompts always go in the message itself.
INLINE_ONLY_SITES = frozenset({"qwen", "chatgpt"})
# None = no limit: the loop runs until the judge passes the work (or you stop
# it). Only an explicit `rounds=` caps it.
MAX_ROUNDS = None
SOFT_ROUNDS = 6          # an uncapped loop stops here and takes the best
SOFT_MARGIN = 1.0        # ... if it has no blocker and is within this of the bar
MAX_CONTINUES = 8

# A follow-up message carries no attachment, and the prompt tells the chat to
# answer "Missing: <file>" when a required file is not attached to the message
# it is reading - so say plainly that the files are already in this chat.
CONTINUE_MESSAGE = (
    "continue - this is the same task, not a new one: the attached files are "
    "already in this chat, do not check for them again. Output ONLY the "
    "remaining JSON as text in this chat (no file, no download), starting at "
    "the next entry after the last complete one, and close every bracket.")
# the chat asked for the reference bible (the brief's "bible gate") although the
# prompt says the channel has none
BIBLE_MESSAGE = (
    "This channel has NO reference bible, and the inputs above say so - do not ask for "
    "one again. Plan without a bible now, using the narration, the visual style and "
    "any elements listed in the inputs, and reply with the COMPLETE shotlist JSON in "
    "ONE ```json block, as text in this chat.")
_ASKS_FOR_BIBLE = re.compile(r"\bbible\b", re.I)
# the chat answered with a file / download / prose instead of the JSON itself
PASTE_MESSAGE = (
    "I cannot open files or downloads from this chat. Paste the complete "
    "shotlist JSON here as text in ONE ```json code block - not as a file, "
    "canvas, artifact or download link. The attached files are already in "
    "this chat; do not check for them again.")
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
    # the cut can fall before the entry's file name is finished (`{ "file":` or
    # `{ "file": "S09_13_PRO`), so the name cannot be matched: a reply that
    # starts a new entry while the text so far ends inside an unclosed one is
    # a restart of that entry - drop the half entry
    if chunk.startswith("{"):
        last = body.rfind("{")
        if last != -1 and webchat._balanced_end(body, last, "{", "}") is None:
            return body[:last].rstrip() + "\n" + chunk
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
        if "{" in body:
            message = CONTINUE_MESSAGE
        elif _ASKS_FOR_BIBLE.search(body):
            message = BIBLE_MESSAGE
        else:
            message = PASTE_MESSAGE
        reply = transport.ask(site, message, (), new_chat=False, ready=_has_json)
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
    # without a verdict no reviewer was called: the app's own code checks found
    # the problems, and the writer is told so
    lines = ["The reviewer found problems with your shotlist." if verdict else
             "The app's own automatic checks (no reviewer was called) found "
             "problems with your shotlist."]
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


def _fix_in_place(cfg, pid: int, transport, writer: str, data: dict, hard: list,
                  weak: list, cues: list, plan: dict, ctx: dict, log, dump,
                  rnd: int) -> dict | None:
    """Ask the writer for a DELTA - only the shots and images that change - and
    apply it in code, so the writer never has to re-send (and quietly repeat)
    the whole plan. Returns the corrected plan, or None when the writer's reply
    is not usable or changes nothing (the caller then asks for the whole plan,
    as before)."""
    cue_text = {c["index"]: c["text"] for c in cues}
    prompt = studio.shotlist_fix_prompt(
        data, list(hard), list(weak), cue_text, len(cues), plan["max_hold"],
        style_guide=ctx["visual_style"], bible=ctx["bible"],
        allow_refs=plan["allow_refs"])
    try:
        reply = transport.ask(writer, prompt, (), new_chat=False,
                              ready=_has_json)
    except webchat.ChatLost:
        return None                  # the caller starts a new chat with the plan
    tag = f"fix_round{rnd}"
    dump(f"{tag}_1", reply)
    body = _json_chunk(reply, True)
    fix = None
    for i in range(MAX_CONTINUES + 1):
        try:
            fix = studio.parse_shotlist_fix(body)
            break
        except RuntimeError as exc:
            if i == MAX_CONTINUES or "{" not in body:
                log(f"{writer}: the fix reply is not usable ({str(exc)[:100]})")
                return None
            log(f"{writer}: the fix reply is cut off - sending 'continue' "
                f"({i + 1}/{MAX_CONTINUES})")
            more = transport.ask(writer, CONTINUE_MESSAGE, (), new_chat=False,
                                 ready=_has_json)
            dump(f"{tag}_{i + 2}", more)
            body = _join_chunks(body, _json_chunk(more, False))
    new, changed, summary = studio.apply_shotlist_fix(data, fix)
    if not changed:
        log(f"{writer}: the fix changed nothing")
        return None
    log(f"{writer}: fix applied in place - {summary}")
    return new


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
            passed = verdict.get("pass") is True and not hard
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
            fixed = _fix_in_place(cfg, pid, transport, writer, data, hard, weak,
                                  cues, plan, ctx, log, dump, rnd)
            if fixed is not None:
                data = fixed
                continue
            log(f"asking {writer} for the whole corrected plan instead")
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

def run_elements(cfg, pid: int, transport, writer: str = "zai",
                 log: Callable[[str], None] = print) -> list[dict]:
    """Ask the writer chat for the production's element list (recurring
    characters, places, objects with one fixed look) and save it as
    elements.json / elements.md. One retry when the reply has no usable list."""
    from . import elements
    ctx = ep._context(cfg, pid)
    pdir = ctx["pdir"]
    script = ""
    try:
        script = (pdir / "script.md").read_text(encoding="utf-8")
    except OSError:
        pass
    from . import planning_audit
    element_source = "script"
    srt = studio.find_srt(pdir)
    if srt:
        narration = "\n".join(c["text"] for c in studio.parse_srt_cues(srt.read_text(encoding="utf-8-sig")))
        if narration.strip():
            for warning in planning_audit.source_report(pdir, narration)["warnings"]:
                log("WARNING: " + warning)
            script = narration
            element_source = "subtitles"
            log("Elements use the recorded subtitle narration so they match the image plan.")
    if not script.strip():
        raise StageFailed("There is no script or subtitle narration yet - write the script first.")
    reply = _send(transport, writer,
                  lambda f: elements.prompt(script, ctx["visual_style"]), log,
                  ready=_has_json)
    _dump(cfg, pid, "elements_reply_1", reply)
    found = elements.parse(reply)
    if not found:
        log(f"{writer}: no usable element list in the reply - asking once more")
        reply = transport.ask(
            writer,
            "That was not the JSON I asked for. Reply with ONLY the JSON object "
            '{"elements": [{"name": ..., "kind": "character|place|object", '
            '"description": ...}]} in one ```json block, as text in this chat '
            "(no file, no download).", (), new_chat=False, ready=_has_json)
        _dump(cfg, pid, "elements_reply_2", reply)
        found = elements.parse(reply)
    if not found:
        raise StageFailed(f"{writer} never gave a readable element list. The raw "
                          f"replies are in the production's webchat_debug folder.")
    elements.save(pdir, found)
    (pdir / "elements_source.json").write_text(json.dumps({
        "fingerprint": planning_audit.fingerprint(script), "source": element_source
    }, indent=2), encoding="utf-8")
    log(f"{len(found)} element(s) saved to elements.md: "
        + ", ".join(e["name"] for e in found[:12])
        + (" ..." if len(found) > 12 else ""))
    return found


def elements_job(cfg, pid: int, writer: str, judge: str, log,
                 should_stop: Callable[[], bool] = lambda: False,
                 options: dict | None = None) -> None:
    with web_transport(cfg, log, options) as t:
        log(f"settings: {t.options}")
        t.set_stop(should_stop)
        try:
            run_elements(cfg, pid, t, writer, log)
        finally:
            save_chats(cfg, pid, t)


# ---- beats (script-first planning) ---------------------------------------------------

BEATS_CONTINUE = (
    "continue - same task. Output ONLY the remaining beats as more entries "
    "({...}, {...}) in a new ```json block, as text in this chat (no file), starting at "
    "the next beat after the last complete one, then close the list and the object "
    "with ] }.")
BEATS_MAX_ROUNDS = 12


def _collect_beats(transport, site: str, reply: str, log, dump, allowed) -> dict:
    """The beats object from a reply, sending 'continue' only while the JSON is
    really cut off."""
    dump("beats_reply_1", reply)
    body = _json_chunk(reply, True)
    for i in range(MAX_CONTINUES + 1):
        got = beatsmod.parse(body, allowed)
        if got:
            return got
        if i == MAX_CONTINUES:
            break
        log(f"{site}: no complete beat list in the reply yet ({len(body):,} characters "
            f"read) - sending 'continue' ({i + 1}/{MAX_CONTINUES})")
        if "{" in body:
            message = BEATS_CONTINUE
        elif _ASKS_FOR_BIBLE.search(body):
            message = BIBLE_MESSAGE
        else:
            message = PASTE_MESSAGE
        reply = transport.ask(site, message, (), new_chat=False, ready=_has_json)
        dump(f"beats_reply_{i + 2}", reply)
        body = _join_chunks(body, _json_chunk(reply, False))
    raise StageFailed(f"{site} never gave a readable beat list ({len(body):,} characters "
                      f"read). The raw replies are in the production's webchat_debug folder.")


def run_beats(cfg, pid: int, transport, writer: str = "zai",
              log: Callable[[str], None] = print,
              should_stop: Callable[[], bool] = lambda: False,
              quality_judge: str | None = None) -> dict:
    """Plan the video from the NARRATION: the writer returns beats (one image
    each, with the first words of the narration they begin at); the app finds
    those words in the subtitles, so every cue is covered once by construction,
    builds shotlist.json and runs the usual code checks. Faults go back to the
    writer as small per-beat fixes. No judge round (the code checks are the
    gate)."""
    ctx = ep._context(cfg, pid)
    plan = ep._plan_inputs(cfg, pid, ctx)
    cues, profile, pdir = plan["cues"], plan["profile"], ctx["pdir"]
    cap, tol = plan["max_hold"], profile.max_hold_tol
    allowed = list(profile.allowed or briefs.MOTION_CODES)
    from . import elements
    from . import planning_audit
    from datetime import datetime, timezone
    from uuid import uuid4
    if quality_judge == writer:
        raise StageFailed("Choose a different chat site/account for the quality judge.")
    run_dir = pdir / "webchat_debug" / "beats_runs" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid4().hex[:8])
    run_dir.mkdir(parents=True, exist_ok=True)

    def dump(name: str, text: str) -> None:
        _dump(cfg, pid, name, text)
        (run_dir / (name + ".txt")).write_text(text, encoding="utf-8")

    # the narration is what the audio says (the subtitles), so a quote of it always
    # matches; script.md can be a newer draft than the audio
    narration = "\n".join(c["text"] for c in cues)
    source = planning_audit.source_report(pdir, narration)
    for warning in source["warnings"]:
        log("WARNING: " + warning)
    auto_numbers, renderer_text = planning_audit.number_mode(cfg, ctx)
    log(f"Numerical coverage: automatic renderer values {'on' if auto_numbers else 'off'}; "
        f"renderer text {'enabled' if renderer_text else 'disabled'}.")
    log(f"Full beats diagnostics: {run_dir}")
    judge_open = False
    max_words = beatsmod.words_per_beat(cap, tol, narration, plan["total_s"])
    elements_md = elements.load_text(pdir)
    channel_context = ep.planning_context_prompt(
        cfg, ctx, plan, "Read the NARRATION below; beats use exact opening words instead of cue numbers.")
    supplied = {Path(f).stem: f"refs/{f}" for f in studio.find_supplied_refs(pdir)}
    if not plan["allow_refs"]:
        supplied = {}
    animation_note = ep._animation_note(ctx.get("eff", {}), cues)
    if animation_note:
        from . import motion
        last = motion.window_last_cue(cues, ctx["eff"]["animate_seconds"], studio._srt_seconds)
        boundary = next((c["text"] for c in cues if int(c["index"]) == last), "")
        animation_note = animation_note.replace(
            f"cues 1-{last} ONLY", f"the narration from its beginning through: {boundary!r}")
        animation_note = animation_note.replace(
            f"Shots after cue {last}", "Beats beginning after that narration")
    log(f"{len(cues)} cues, {len(narration.split())} words; a beat is asked to stay under "
        f"about {max_words} words ({cap:g}s limit + {tol:.0%} tolerance); "
        + (f"elements.md used" if elements_md else "no elements.md"))
    def build_beats_prompt(files):
        text = beatsmod.prompt(narration, elements_md, ctx["visual_style"],
                               ctx["bible"], allowed, cap, max_words,
                               animation_note=animation_note, channel_context=channel_context)
        dump("beats_prompt", text)
        return text
    reply = _send(transport, writer, build_beats_prompt, log, ready=_has_json)
    got = _collect_beats(transport, writer, reply, log, dump, allowed)
    beats, style = got["beats"], got["style"] or ctx["visual_style"]
    plan_meta = beatsmod.metadata(got)
    log(f"{writer}: {len(beats)} beat(s) received")

    prev_faults, same = None, 0
    for rnd in range(1, BEATS_MAX_ROUNDS + 2):
        if should_stop():
            raise StageFailed("Stopped by you.")
        if supplied:
            plan_meta.setdefault("refs", {}).update(supplied)
            for name in supplied:
                plan_meta.get("refPrompts", {}).pop(name, None)
        starts, ranges, faults = beatsmod.check(beats, cues, allowed, cap, tol,
                                              allow_reveal=profile.reveal)
        data, names = beatsmod.build_shotlist(beats, ranges, style, plan_meta, cues)
        if not plan["allow_refs"] and studio.shotlist_uses_refs(data):
            faults.append("References are disabled for this channel: remove refs/refPrompts "
                          "registries and refs on every beat and reveal image.")
        numbers = planning_audit.numerical_report(data, cues, auto=auto_numbers,
                                                  overlays_enabled=renderer_text)
        faults.extend(numbers["faults"])
        pacing = planning_audit.pacing_report(data, cues, profile)
        if not faults:
            try:
                _, loc = ep.shotlist_judge_prompt(
                    cfg, pid, json.dumps(data, ensure_ascii=False, indent=1), [])
                faults = [str(x) for x in (loc or [])]
            except (ep.PromptError, RuntimeError, ValueError) as e:
                log(f"code checks could not run: {e}")
                faults.append(f"Code checks could not run: {e}")
        verdict = None
        judge_unavailable = False
        if not faults and quality_judge:
            if should_stop():
                raise StageFailed("Stopped by you.")
            log(f"round {rnd}: code checks passed; asking {quality_judge} for a quality review")
            judge_prompt, _ = ep.shotlist_judge_prompt(
                cfg, pid, json.dumps(data, ensure_ascii=False, indent=1), [])
            judge_prompt += ("\nReview visual specificity, concrete props, financial comparisons and ONE "
                             "depictable moment per still. Check current channel rules. Automatic numeric "
                             "overlays are already audited by code; do not demand duplicate baked-in text.\n"
                             + json.dumps(numbers, ensure_ascii=False))
            dump(f"beats_checks_round{rnd}", json.dumps({
                "round": rnd, "source": source, "pacing": pacing, "numbers": numbers,
                "faults": faults, "status": "awaiting_quality_review"}, ensure_ascii=False, indent=2))
            dump(f"beats_judge_prompt_round{rnd}", judge_prompt)
            raw = _send(transport, quality_judge, lambda f: judge_prompt, log,
                        new_chat=not judge_open, ready=_is_shotlist_verdict)
            dump(f"beats_judge_round{rnd}", raw)
            verdict = studio._parse_json_object(raw, want=("pass", "faults"))
            if not verdict:
                raw = _nudge_judge(transport, quality_judge, log, _is_shotlist_verdict)
                dump(f"beats_judge_round{rnd}_retry", raw)
                verdict = studio._parse_json_object(raw, want=("pass", "faults"))
            if not verdict or not isinstance(verdict.get("faults"), list):
                faults.append("Quality judge returned no usable verdict; retry the review.")
                judge_unavailable = True
            else:
                judge_open = True
                faults.extend(str(f) for f in verdict.get("faults", []))
                weak = _weak_list(verdict)
                ratio = verdict.get("detailed_ratio")
                passed = (verdict.get("pass") is True and not faults
                          and isinstance(ratio, (int, float)) and ratio >= plan["min_align"])
                if not passed:
                    faults.extend(f"{w.get('asset')}: {w.get('reason', '')}; "
                                  + "; ".join(w.get("missing") or []) for w in weak)
                    if not faults:
                        faults.append("Quality review did not pass: " + "; ".join(
                            str(x) for x in verdict.get("fixes", [])))
        report = {"round": rnd, "source": source, "pacing": pacing, "numbers": numbers,
                  "faults": faults, "quality_verdict": verdict,
                  "status": "needs_correction" if faults else "checks_passed"}
        dump(f"beats_checks_round{rnd}", json.dumps(report, ensure_ascii=False, indent=2))
        for warning in pacing["warnings"]:
            log("WARNING: " + warning)
        beatsmod.save(pdir, beats, ranges, names, style, plan_meta)
        if judge_unavailable:
            raise StageFailed("Quality judge returned no usable verdict; previous shotlist preserved. "
                              f"See {run_dir} for the full replies.")
        if not faults:
            old = pdir / "shotlist.json"
            if old.exists():
                shutil.copyfile(old, pdir / "shotlist_before_beats.json")
            gap = save_shotlist(cfg, pid, data,
                                f"web chat {writer}: {len(beats)} beats from the script, "
                                f"round {rnd}", advance=True)
            log(f"round {rnd}: no faults - shotlist saved ({len(data['shots'])} shots)"
                + (f"; coverage gap: {gap}" if gap else "; next stage unlocked"))
            dump("beats_result", json.dumps({"status": "accepted" if not gap else "not_advanced",
                                              "round": rnd, "coverage_gap": gap}, indent=2))
            return data
        log(f"round {rnd}: {len(faults)} fault(s): " + " | ".join(f[:140] for f in faults[:3]))
        key = "\n".join(faults)
        same = same + 1 if key == prev_faults else 0
        prev_faults = key
        if same >= 2 or rnd > BEATS_MAX_ROUNDS:
            break
        msg = beatsmod.fix_prompt(faults, beats, ranges, cues, names, allowed, cap,
                                 plan_meta=plan_meta,
                                 channel_context=channel_context + animation_note)
        got_fix = None
        for attempt in (1, 2):
            dump(f"beats_fix_prompt_round{rnd}_{attempt}", msg)
            fixed = transport.ask(writer, msg,
                                  (), new_chat=False, ready=_has_json)
            dump(f"beats_fix_round{rnd}_{attempt}", fixed)
            got_fix = beatsmod.parse_fix(fixed, allowed)
            if got_fix:
                try:
                    updated = beatsmod.apply_fix(beats, got_fix)
                except ValueError as e:
                    log(f"fix rejected: {e}")
                    msg = f"Your correction was rejected: {e}\n\n" + msg
                    got_fix = None
                else:
                    break
            else:
                msg = 'Reply with ONLY the {"replace": [...]} JSON object.\n\n' + msg
        if not got_fix:
            log(f"{writer} gave no usable fix")
            break
        before = len(beats)
        beats = updated
        for key, entries in beatsmod.metadata(got_fix).items():
            if entries:
                plan_meta.setdefault(key, {}).update(entries)
            else:
                plan_meta[key] = {}
        if got_fix["style"]:
            style = got_fix["style"]
        log(f"fix applied: {len(got_fix['replace'])} beat(s) replaced, {before} -> {len(beats)} beats")
    dump("beats_result", json.dumps({"status": "failed", "round": rnd,
                                      "last_faults": prev_faults}, ensure_ascii=False, indent=2))
    raise StageFailed(
        f"The beat plan did not pass the code checks. beats.json holds the last try; "
        f"shotlist.json was not changed. Still wrong: {(prev_faults or '')[:300]}")


def beats_job(cfg, pid: int, writer: str, judge: str, log,
              should_stop: Callable[[], bool] = lambda: False,
              options: dict | None = None) -> None:
    options = dict(options or {})
    quality = bool(options.pop("beats_quality_judge", False))
    with web_transport(cfg, log, options) as t:
        log(f"settings: {t.options}")
        t.set_stop(should_stop)
        try:
            run_beats(cfg, pid, t, writer, log, should_stop,
                      quality_judge=judge if quality else None)
        finally:
            save_chats(cfg, pid, t)



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
