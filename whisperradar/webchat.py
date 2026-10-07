"""Drive a free web chat (z.ai, DeepSeek) like a person would, so a stage can
run on a chat subscription instead of an API.

This module only moves text: it opens the chat in a persistent browser profile
(you sign in once), pastes a prompt, optionally attaches files, sends it, waits
for the reply to finish - clicking "Continue" when the site asks - and hands
back the reply text. It never builds prompts and never judges anything; the
stage runners build the prompts (external_prompts.py) and read the answers.

Everything that touches a page goes through five small methods (goto,
evaluate, wait_for_timeout, set_input_files, click) so the logic is tested
with a fake page; only `WebChat` itself imports Playwright.

Run `python -m whisperradar.webchat login zai` once per site to sign in, and
`python -m whisperradar.webchat ask zai "hello"` to try it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence


class WebChatError(RuntimeError):
    """The chat could not give an answer; the message says why."""


class NeedsSignIn(WebChatError):
    """The site shows a login page: run `webchat login <site>` once."""


class ChatLost(WebChatError):
    """A follow-up was asked in a chat that is not open any more (the window
    was closed or restarted): sending it would land in an empty page."""


class BotCheck(WebChatError):
    """The site is asking for a captcha / human check. Never solved here."""


class WebChatTimeout(WebChatError):
    """The reply did not finish in time; `partial` holds what arrived."""

    def __init__(self, message: str, partial: str = ""):
        super().__init__(message)
        self.partial = partial


# ---- per-site knowledge -------------------------------------------------------

@dataclass(frozen=True)
class Site:
    key: str
    name: str
    url: str
    box: str                    # CSS selector of the prompt box
    reply: str                  # CSS selector of an assistant message
    send_js: str                # JS that clicks the send control (returns bool)
    generating_js: str          # JS expression: true while the model writes
    login_url_part: tuple = ("sign_in", "login", "auth")
    files: str = "input[type=file]"
    # URL fragment of the site's streaming answer request. When set, the reply
    # is taken from that stream (the page itself only keeps the last ~50 lines
    # of a long code block, so a long shotlist cannot be read from the DOM)
    stream: str = ""
    # JS expression: true once the message has really been sent (a new chat's
    # address changes; DeepSeek / z.ai move to /chat/s/<id> or /c/<id>)
    sent_js: str = "() => false"
    # called once per chat, after the page loaded, before pasting
    prepare: Optional[Callable] = None


# model name in the picker -> (data-value of its menu item, label shown)
ZAI_MODELS = {"flash": ("x-preview-l", "GLM-5.3-Flash"),
              "5.3": ("glm-5.3", "GLM-5.3"),
              "5.2": ("glm-5.2", "GLM-5.2")}


def _zai_pick_model(page, model: str) -> None:
    value, label = ZAI_MODELS.get(model, ZAI_MODELS["flash"])
    res = page.evaluate("""(a) => {
        const b=document.querySelector('button[aria-label="Select a model"]');
        if(!b) return 'missing';
        if((b.innerText||'').trim()===a.label) return 'ok';
        b.click(); return 'opened'; }""", {"label": label})
    if res != "opened":
        return                          # already the right model / no picker
    page.wait_for_timeout(600)
    try:
        page.evaluate("""(v) => { const i=document.querySelector(
            'button[aria-label="model-item"][data-value="'+v+'"]');
            if(i) i.click(); }""", value)
    except Exception:  # noqa: BLE001 - the page navigated: see below
        pass
    page.wait_for_timeout(1500)
    url = str(getattr(page, "url", "") or "")
    if any(p in url for p in SITES["zai"].login_url_part):
        raise NeedsSignIn(
            f"z.ai only offers {label} to a signed-in account (it sent the "
            f"browser to its sign-in page). Sign in once with: python -m "
            f"whisperradar.webchat login zai - or pick GLM-5.3-Flash.")


def _zai_prepare(page, thinking: str = "Low", model: str = "flash") -> None:
    """Pick the model, then z.ai's Deep Think level (it defaults to Max on
    every page load, and Max can spend the whole turn thinking and answer
    nothing, so the default here is Low)."""
    try:
        _zai_pick_model(page, model)
    except NeedsSignIn:
        raise
    except Exception:  # noqa: BLE001 - the picker moved: keep the default
        pass
    opened = page.evaluate("""() => {
        const spans=[...document.querySelectorAll('span')].filter(
          e=>/^(Low|High|Max)$/.test((e.innerText||'').trim())
             && e.previousElementSibling
             && /Deep Think/.test(e.previousElementSibling.innerText||''));
        if(!spans.length) return false; spans[0].click(); return true; }""")
    if not opened:
        return                                   # layout changed: ask anyway
    page.wait_for_timeout(600)
    page.evaluate("""(level) => {
        const el=[...document.querySelectorAll('div,button,li,span')].filter(
          e=>e.children.length===0 && (e.innerText||'').trim()===level);
        if(el.length) el[el.length-1].click(); }""", thinking)
    page.wait_for_timeout(400)
    try:                                         # close the menu overlay
        page.mouse.click(8, 8)
    except Exception:  # noqa: BLE001 - the fake page / old layouts have none
        pass
    page.wait_for_timeout(300)
    try:
        now = page.evaluate("""() => {
          const m=document.querySelector('button[aria-label="Select a model"]');
          const d=[...document.querySelectorAll('span')].find(e=>
            /^(Low|High|Max)$/.test((e.innerText||'').trim())
            && e.previousElementSibling
            && /Deep Think/.test(e.previousElementSibling.innerText||''));
          return (m?m.innerText.trim():'?')+' / Deep Think '
                 +(d?d.innerText.trim():'?'); }""")
        return f"z.ai is using {now} (asked: {model}, {thinking})"
    except Exception:  # noqa: BLE001
        return ""


_DS_TOGGLE_JS = """(args) => {
  const els=[...document.querySelectorAll('.ds-toggle-button')];
  const t=els.find(e=>(e.innerText||'').trim().toLowerCase()
                       .startsWith(args.label.toLowerCase()));
  if(!t) return 'missing';
  const on=t.getAttribute('aria-pressed')==='true';
  if(on===args.want) return 'ok';
  t.click(); return 'clicked'; }"""


def _deepseek_prepare(page, deepthink: bool = True, search: bool = False):
    """DeepSeek remembers its two switches between chats; set both to what
    the run asked for (DeepThink on, Search off by default)."""
    for label, want in (("DeepThink", bool(deepthink)),
                        ("Search", bool(search))):
        res = page.evaluate(_DS_TOGGLE_JS, {"label": label, "want": want})
        if res == "clicked":
            page.wait_for_timeout(400)
            page.evaluate(_DS_TOGGLE_JS, {"label": label, "want": want})
    page.wait_for_timeout(200)


ZAI = Site(
    key="zai", name="z.ai", url="https://chat.z.ai/",
    box="textarea", reply=".chat-assistant",
    send_js="""() => { const b=document.querySelector('#send-message-button');
        if(!b) return false; b.click(); return true; }""",
    # while z.ai thinks or writes, the send arrow is replaced by a round stop
    # button holding a small square; "Stop" at the end of the page text was
    # not shown during the "Thinking..." phase
    generating_js="""() => !!document.querySelector('button > span.size-3')
        || /\\bStop\\s*$/.test(document.body.innerText.trim())""",
    stream="/chat/completions",
    sent_js="""() => /\\/c\\/[0-9a-f-]{8,}/.test(location.pathname)""",
    prepare=_zai_prepare)

DEEPSEEK = Site(
    key="deepseek", name="DeepSeek", url="https://chat.deepseek.com/",
    box="textarea", reply=".ds-assistant-message-main-content",
    send_js="""() => { const b=[...document.querySelectorAll(
          '.ds-button--primary,[role=button].ds-button--primary')].pop();
        if(!b) return false; b.click(); return true; }""",
    generating_js="""() => !!document.querySelector(
          '[class*=stop-generat],[aria-label*=Stop],[title*=Stop]')""",
    sent_js="""() => /\\/chat\\/s\\//.test(location.pathname)""",
    prepare=_deepseek_prepare)

SITES = {s.key: s for s in (ZAI, DEEPSEEK)}

CONTINUE_LABELS = ("continue generating", "continue", "weiter")

_BOT_JS = """() => { const t=(document.body.innerText||'').toLowerCase();
  return /verify (that )?you are (a )?human|are you a robot|complete the captcha|security check|checking your browser/.test(t)
         || !!document.querySelector('iframe[src*=captcha],iframe[src*=turnstile],#cf-challenge-running'); }"""

_PASTE_JS = """(args) => {
  const ta=document.querySelector(args.box); if(!ta) return -1;
  const set=Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,'value').set;
  set.call(ta,args.text); ta.dispatchEvent(new Event('input',{bubbles:true}));
  ta.focus(); return ta.value.length; }"""

_STATE_JS = """(args) => {
  const els=[...document.querySelectorAll(args.reply)];
  const last=els.length?els[els.length-1]:null;
  return {count:els.length, text:last?last.innerText:'',
          url:location.href}; }"""

_CAPTURE_JS = """() => {
  if (window.__wr_hooked) return;
  window.__wr_hooked = true; window.__wr_cap = [];
  const of = window.fetch;
  window.fetch = async function (...a) {
    const resp = await of.apply(this, a);
    try {
      const url = String((a[0] && a[0].url) || a[0]);
      if (resp.body && (resp.headers.get('content-type') || '')
                         .includes('event-stream')) {
        const rec = {url: url, chunks: [], done: false};
        window.__wr_cap.push(rec);
        const rd = resp.clone().body.getReader();
        const dec = new TextDecoder();
        (async () => {
          try { for (;;) { const r = await rd.read(); if (r.done) break;
                rec.chunks.push(dec.decode(r.value, {stream: true})); } }
          catch (e) {}
          rec.done = true; })();
      }
    } catch (e) {}
    return resp; };
}"""
_CAP_RESET_JS = "() => { window.__wr_cap = window.__wr_cap || []; window.__wr_cap.length = 0; }"
_CAP_GET_JS = "() => (window.__wr_cap || []).map(r => ({url: r.url, done: r.done, text: r.chunks.join('')}))"

_TA_LEN_JS = """(sel) => { const t=document.querySelector(sel);
  return t ? t.value.length : -1; }"""
_MSG_COUNT_JS = "() => document.querySelectorAll('.ds-message').length"
_PAGE_TAIL_JS = "() => (document.body.innerText||'').trim().slice(-300)"

_STATUS_LINE = re.compile(
    r"^\s*(thought process|thinking\.*|thinking\s*\.\.\.|deep thinking\.*)\s*$",
    re.I)


def stream_text(page, site: "Site") -> str:
    """The finished answer text from the site's captured stream ('' when the
    site has no stream, nothing was captured, or it is not finished)."""
    if not site.stream:
        return ""
    try:
        recs = page.evaluate(_CAP_GET_JS) or []
    except Exception:  # noqa: BLE001
        return ""
    recs = [r for r in recs if site.stream in str(r.get("url"))]
    if not recs or not recs[-1].get("done"):
        return ""
    out = []
    for line in str(recs[-1].get("text") or "").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            msg = json.loads(line[5:].strip())
        except ValueError:
            continue
        d = msg.get("data") if isinstance(msg, dict) else None
        if (isinstance(d, dict) and d.get("phase") == "answer"
                and isinstance(d.get("delta_content"), str)):
            out.append(d["delta_content"])
    return "".join(out).strip()


def clean_reply(text: str) -> str:
    """A reply without the chat's own status lines ("Thought Process",
    "Thinking...") that sit at the top of the assistant message."""
    lines = (text or "").strip().splitlines()
    while lines and (not lines[0].strip() or _STATUS_LINE.match(lines[0])):
        lines.pop(0)
    return "\n".join(lines).strip()


_CONTINUE_JS = """(labels) => {
  const cands=[...document.querySelectorAll('button,[role=button],.ds-button')]
    .filter(b=>b.offsetParent!==null);
  for (const b of cands.reverse()) {
    const t=(b.innerText||b.getAttribute('aria-label')||'').trim().toLowerCase();
    if (t && t.length<40 && labels.some(l=>t===l||t.startsWith(l))) {
      b.click(); return t; } }
  return ''; }"""


# ---- one conversation -----------------------------------------------------------

def ask(page, site: Site, prompt: str, files: Sequence[str] = (),
        timeout: float = 1800, settle: float = 6.0, poll: float = 2.0,
        continue_max: int = 8, start_wait: float = 90.0,
        new_chat: bool = True, options: Optional[dict] = None,
        ready: Optional[Callable[[str], bool]] = None, ready_wait: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = lambda m: None) -> str:
    """Send `prompt` (+ `files`, local paths) and return the finished reply.
    A NEW chat by default; `new_chat=False` answers inside the chat that is
    already open (feedback to the writer). Raises NeedsSignIn, BotCheck or
    WebChatTimeout."""
    if not (prompt or "").strip():
        raise WebChatError("Nothing to send")
    if new_chat:
        page.goto(site.url)
    _wait_for_box(page, site, clock)
    if site.stream:
        page.evaluate(_CAPTURE_JS)         # no-op when already hooked
    if new_chat and site.prepare:
        status = site.prepare(page, **(options or {}))
        if isinstance(status, str) and status:
            log(status)

    if files:
        page.set_input_files(site.files, [str(f) for f in files])
        _wait_for_uploads(page, files, clock, log)

    n = page.evaluate(_PASTE_JS, {"box": site.box, "text": prompt})
    # a textarea turns \r\n into \n, so compare with the normalised text and
    # allow a sliver of difference (control characters the box drops)
    want = len(prompt.replace("\r\n", "\n").replace("\r", "\n"))
    if n < want * 0.995:
        raise WebChatError(
            f"{site.name}: the prompt box kept {n} of {want} "
            f"characters - the site's page probably changed")
    if n != want:
        log(f"{site.name}: the box dropped {want - n} of {want} characters "
            f"(line endings / control characters)")
    page.wait_for_timeout(500)
    before = page.evaluate(_STATE_JS, {"reply": site.reply})["count"]
    if site.stream:
        page.evaluate(_CAP_RESET_JS)
    msgs_before = page.evaluate(_MSG_COUNT_JS) or 0
    # A send is only repeated when there is NO sign it went out: the box still
    # holds the text, the address did not change, nothing is generating, no
    # new message appeared. A big prompt with attachments can take well over
    # ten seconds to be accepted, and a second click on a send button that has
    # turned into a stop button would cancel the answer.
    for attempt in (1, 2, 3):
        if not page.evaluate(site.send_js):
            raise WebChatError(f"{site.name}: could not find the send button")
        took = False
        for _ in range(30):
            page.wait_for_timeout(1000)
            if (page.evaluate(_TA_LEN_JS, site.box) == 0
                    or page.evaluate(site.generating_js)
                    or (new_chat and page.evaluate(site.sent_js))
                    or (page.evaluate(_MSG_COUNT_JS) or 0) > msgs_before
                    or page.evaluate(_STATE_JS,
                                     {"reply": site.reply})["count"] > before):
                took = True
                break
        if took:
            break
        log(f"{site.name}: the send did not register (try {attempt}/3)")

    t0 = clock()
    last_text, stable_since, continues = "", clock(), 0
    started = False
    while True:
        page.wait_for_timeout(int(poll * 1000))
        if page.evaluate(_BOT_JS):
            raise BotCheck(f"{site.name} is asking for a human check - open "
                           f"the browser window and complete it yourself")
        st = page.evaluate(_STATE_JS, {"reply": site.reply})
        text = st["text"] or ""
        gen = bool(page.evaluate(site.generating_js))
        if not started:
            if st["count"] > before or gen or text:
                started = True
            elif clock() - t0 > start_wait:
                tail = " ".join(str(page.evaluate(_PAGE_TAIL_JS)
                                    or "").split())[-240:]
                raise WebChatTimeout(
                    f"{site.name} did not start answering. The page ends "
                    f"with: \"{tail}\"", "")
        if text != last_text:
            last_text, stable_since = text, clock()
        done_waiting = (started and not gen
                        and clock() - stable_since >= settle)
        if done_waiting and not clean_reply(last_text):
            done_waiting = False           # only "Thinking..." so far
        if (done_waiting and ready is not None
                and clock() - stable_since < ready_wait
                and not ready(clean_reply(last_text))):
            done_waiting = False           # settled, but not a full answer yet
        if done_waiting:
            clicked = (page.evaluate(_CONTINUE_JS, list(CONTINUE_LABELS))
                       if continues < continue_max else "")
            if clicked:
                continues += 1
                log(f"{site.name}: clicked '{clicked}' ({continues})")
                stable_since = clock()
                continue
            streamed = stream_text(page, site)
            if streamed:
                return streamed
            if clean_reply(last_text):
                return clean_reply(last_text)
            raise WebChatError(f"{site.name} returned an empty reply")
        if clock() - t0 > timeout:
            raise WebChatTimeout(
                f"{site.name} did not finish within {int(timeout)}s",
                clean_reply(last_text))


def _wait_for_box(page, site: Site, clock, limit: float = 40.0) -> None:
    t0 = clock()
    while True:
        if page.evaluate(_BOT_JS):
            raise BotCheck(f"{site.name} is asking for a human check - open "
                           f"the browser window and complete it yourself")
        url = str(page.url or "")
        if any(p in url for p in site.login_url_part):
            raise NeedsSignIn(
                f"{site.name} needs you to sign in: run "
                f"`python -m whisperradar.webchat login {site.key}`")
        if page.evaluate("(s)=>!!document.querySelector(s)", site.box):
            return
        if clock() - t0 > limit:
            raise NeedsSignIn(
                f"{site.name}: the chat box never appeared - sign in with "
                f"`python -m whisperradar.webchat login {site.key}`")
        page.wait_for_timeout(1000)


def _wait_for_uploads(page, files, clock, log, limit: float = 120.0) -> None:
    names = [Path(str(f)).name for f in files]
    t0 = clock()
    while True:
        body = page.evaluate("()=>document.body.innerText") or ""
        if all(n in body for n in names):
            return
        if clock() - t0 > limit:
            raise WebChatError("the attached file(s) never showed up in the "
                               "chat: " + ", ".join(names))
        page.wait_for_timeout(1000)


# ---- reading JSON out of a reply ----------------------------------------------------

def extract_json(text: str):
    """The last complete JSON object/array in a chat reply (fenced or bare),
    or None. Chat UIs wrap code in 'json / Copy / Download' chrome, so this
    scans for balanced braces instead of trusting the reply's first line."""
    text = text or ""
    best = None
    for opener, closer in (("{", "}"), ("[", "]")):
        for m in re.finditer(re.escape(opener), text):
            end = _balanced_end(text, m.start(), opener, closer)
            if end is None:
                continue
            try:
                val = json.loads(text[m.start():end])
            except ValueError:
                continue
            if best is None or end > best[0]:
                best = (end, val)
    return best[1] if best else None


def _balanced_end(s: str, start: int, opener: str, closer: str):
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i + 1
    return None


# ---- the real browser ---------------------------------------------------------------

# ---- your own Chrome, attached over the debugging port ------------------------------
#
# `login <site>` starts a NORMAL Chrome (no automation flags, so Google/GitHub
# sign-in works) with a debugging port and the site's profile, and leaves it
# open. A run then attaches to it. If no such Chrome is listening, WebChat
# launches its own as before.

CDP_PORTS = {"zai": 9222, "deepseek": 9223}


def cdp_endpoint(key: str) -> str:
    return f"http://127.0.0.1:{CDP_PORTS.get(key, 9300)}"


def cdp_alive(key: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(cdp_endpoint(key) + "/json/version",
                                    timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def find_chrome() -> str | None:
    cands = []
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            cands += [os.path.join(base, "Google", "Chrome", "Application",
                                   "chrome.exe"),
                      os.path.join(base, "Microsoft", "Edge", "Application",
                                   "msedge.exe")]
    cands += ["/usr/bin/google-chrome", "/usr/bin/chromium",
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    for c in cands:
        if os.path.exists(c):
            return c
    for name in ("chrome", "google-chrome", "msedge", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


def start_chrome(key: str, profile_root, wait: float = 20.0) -> bool:
    """Start Chrome for `key` with a debugging port (left running); True once
    it answers. Reuses one that is already listening."""
    if cdp_alive(key):
        return True
    exe = find_chrome()
    if not exe:
        raise WebChatError("Chrome or Edge was not found on this computer")
    profile = Path(profile_root) / key
    profile.mkdir(parents=True, exist_ok=True)
    args = [exe, f"--remote-debugging-port={CDP_PORTS.get(key, 9300)}",
            "--remote-allow-origins=*", f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check", SITES[key].url]
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen(args, creationflags=flags, close_fds=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.monotonic()
    while time.monotonic() - t0 < wait:
        if cdp_alive(key):
            return True
        time.sleep(0.5)
    return False


class WebChat:
    """A persistent browser profile per site, so you sign in once.

        with WebChat(profile_root) as chat:
            reply = chat.ask("zai", prompt, files=[...])
    """

    def __init__(self, profile_root, headless: bool = False,
                 channel: str | None = None):
        self.profile_root = Path(profile_root)
        self.headless = headless
        self.channel = channel          # e.g. "chrome" / "msedge", or None
        self._pw = None
        self._ctx: dict = {}
        self._hooked: set = set()
        self._attached: set = set()     # sites driven in the user's Chrome
        self._urls: dict = {}       # site -> URL of its current chat

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise WebChatError(
                "Playwright is not installed: pip install playwright, then "
                "python -m playwright install chromium") from exc
        self._pw = sync_playwright().start()
        return self

    def __exit__(self, *exc):
        for key, ctx in self._ctx.items():
            if key in self._attached:
                continue                # your own Chrome stays open
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
        self._ctx.clear()
        self._attached.clear()
        if self._pw:
            self._pw.stop()
            self._pw = None

    def _page(self, key: str):
        if key not in SITES:
            raise WebChatError(f"Unknown chat site '{key}' "
                               f"(known: {', '.join(SITES)})")
        if key not in self._ctx and cdp_alive(key):
            browser = self._pw.chromium.connect_over_cdp(cdp_endpoint(key))
            self._ctx[key] = (browser.contexts[0] if browser.contexts
                              else browser.new_context())
            self._attached.add(key)
        if key not in self._ctx:
            profile = self.profile_root / key
            profile.mkdir(parents=True, exist_ok=True)
            kw = {"headless": self.headless,
                  "viewport": {"width": 1100, "height": 900},
                  # Google refuses sign-in in a browser that announces
                  # itself as automated; drop those tells.
                  "ignore_default_args": ["--enable-automation"],
                  "args": ["--disable-blink-features=AutomationControlled"]}
            ctx = None
            # Prefer the real installed Chrome, then Edge, then Playwright's
            # own Chromium (the first two pass Google's sign-in check).
            for ch in ([self.channel] if self.channel else ["chrome", "msedge", None]):
                if ch:
                    kw["channel"] = ch
                else:
                    kw.pop("channel", None)
                try:
                    ctx = self._pw.chromium.launch_persistent_context(
                        str(profile), **kw)
                    break
                except Exception:
                    if ch is None or self.channel:
                        raise
            self._ctx[key] = ctx
        ctx = self._ctx[key]
        if key not in self._hooked:
            self._hooked.add(key)
            try:
                ctx.add_init_script(
                    "(" + _CAPTURE_JS + ")()")
            except Exception:  # noqa: BLE001
                pass
        if key in self._attached:
            host = SITES[key].url.split("//")[-1].split("/")[0]
            for pg in ctx.pages:
                if host in str(pg.url):
                    return pg
        return ctx.pages[0] if ctx.pages else ctx.new_page()

    def ask(self, key: str, prompt: str, files: Sequence[str] = (),
            new_chat: bool = True, options: Optional[dict] = None,
            **kw) -> str:
        page, site = self._page(key), SITES[key]
        saved = self._urls.get(key)
        if new_chat:
            self._urls.pop(key, None)
        elif saved:
            # keep using the chat we started: if the tab wandered off (or
            # was reloaded onto a blank page), go back to it first
            try:
                if page.url.split("#")[0] != saved:
                    page.goto(saved)
            except Exception:  # noqa: BLE001
                pass
        if not new_chat:
            try:
                in_chat = bool(page.evaluate(site.sent_js))
            except Exception:  # noqa: BLE001
                in_chat = False
            if not in_chat:
                raise ChatLost(
                    f"{site.name}: the chat this answer belongs to is not "
                    f"open in the browser any more")
        reply = ask(page, site, prompt, files, new_chat=new_chat,
                    options=options, **kw)
        try:
            if page.evaluate(site.sent_js):
                self._urls[key] = page.url.split("#")[0]
        except Exception:  # noqa: BLE001
            pass
        return reply

    def sign_in(self, key: str, wait: float = 600.0) -> bool:
        """Open the site and wait for YOU to sign in (never typed for you)."""
        page = self._page(key)
        site = SITES[key]
        page.goto(site.url)
        t0 = time.monotonic()
        while time.monotonic() - t0 < wait:
            url = str(page.url or "")
            if (not any(p in url for p in site.login_url_part)
                    and page.evaluate("(s)=>!!document.querySelector(s)",
                                      site.box)):
                return True
            page.wait_for_timeout(1500)
        return False


def _main(argv: list[str]) -> int:
    from . import config
    if len(argv) < 2 or argv[0] not in ("login", "ask"):
        print("usage: python -m whisperradar.webchat login <zai|deepseek>\n"
              "       python -m whisperradar.webchat ask <zai|deepseek> "
              "\"prompt\"")
        return 2
    cfg = config.load_config()
    root = Path(cfg.db_path).parent / "webchat"
    if argv[0] == "login":
        if argv[1] not in SITES:
            print("unknown site: " + argv[1])
            return 2
        ok = start_chrome(argv[1], root)
        print("Chrome is open for " + argv[1] + (
            ". Sign in there (email, GitHub or Google all work), make sure "
            "the chat box shows, and LEAVE THAT WINDOW OPEN. A run attaches "
            "to it; you do not need to press anything here."
            if ok else ", but it did not answer on its debugging port."))
        return 0 if ok else 1
    with WebChat(root) as chat:
        print(chat.ask(argv[1], " ".join(argv[2:]), log=print))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
