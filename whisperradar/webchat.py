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


class ModelBusy(WebChatError):
    """The site said the model is at capacity / busy (an error banner, not an
    answer). Not a failure: ask again later, on the SAME model."""


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
BUILTIN_KEYS = tuple(SITES)


# ---- chat sites you add yourself (Settings > Web chat LLMs) -------------------
#
# A definition is a plain dict saved in the `webchat_sites` setting:
#   key, name, url                  which site
#   box, reply, send, generating    CSS selectors: prompt box, an assistant
#                                   message, the send button, anything that
#                                   exists only while the model is writing
#   login_part, sent_part, stream   sign-in URL words, URL word once a chat
#                                   exists (e.g. "/c/"), streaming URL part
#   models:  [{id, label, open, item}]  open = CSS of the picker button (may be
#                                   empty), item = text of the menu entry
#   level_label, levels: [{id, label, open, item}]  low / high / ...
#   toggles: [{id, label, text}]    switches such as DeepThink or Web search;
#                                   text = what the switch says on the page
#   port                            debugging port for its own Chrome

_SLUG = re.compile(r"[^a-z0-9]+")
CUSTOM_PORT_START = 9230

_CLICK_TEXT_JS = """(a) => {
  if (!a.open && !a.current) return 'opened';
  let o = a.open ? document.querySelector(a.open) : null;
  if (!o && a.current) {              // saved selector went stale: find the
    const t = a.current.trim().toLowerCase();
    o = [...document.querySelectorAll(
        'button,[role=button],[role=combobox],[aria-haspopup]')]
      .find(e => e.getBoundingClientRect().width > 0
        && (e.innerText || '').trim().toLowerCase().split('\\n')[0] === t);
  }
  if (!o) return 'missing-picker';
  o.click(); return 'opened'; }"""

_PICK_ITEM_JS = r"""(text) => {
  const want = text.trim().toLowerCase();
  const vis = e => { const r = e.getBoundingClientRect();
                     return r.width > 0 && r.height > 0; };
  const first = e => (e.innerText || '').trim().split('\n')[0].trim()
                       .toLowerCase();
  const menu = [...document.querySelectorAll(
      '[role=menuitem],[role=option],[role=menuitemradio]')]
    .filter(e => vis(e) && first(e) === want);
  const els = menu.length ? menu : [...document.querySelectorAll(
      'div,button,li,span,a')].filter(e => vis(e)
        && e.children.length === 0 && first(e) === want);
  if (!els.length) return 'missing';
  els[els.length - 1].click(); return 'clicked'; }"""

# Lists the model-pickers of a chat page WITHOUT clicking anything (clicking
# is done one-by-one from Python, with a real Escape key between tries, because
# synthetic Escape does not reach React portals and a stuck-open menu would
# poison every later candidate). Each entry: {open, current, text}.
_DETECT_MODELS_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect();
                     return r.width > 0 && r.height > 0; };
  const esc = v => String(v).replace(/"/g, '\\"');
  const path = el => {
    // machine-generated ids (base-ui/radix ":r2q:" style) change every load:
    // only an id that looks stable may be saved as the picker selector
    const stableId = id => id && !/^(base-ui|radix|headlessui|\d|:)/i.test(id)
                           && !/[-_:]r[0-9a-z]{2,}([-_:]?|)$/i.test(id);
    if (stableId(el.id)) return '#' + CSS.escape(el.id);
    const t = el.getAttribute('data-testid');
    if (t) return el.tagName.toLowerCase() + '[data-testid="' + esc(t) + '"]';
    const a = el.getAttribute('aria-label');
    if (a) return el.tagName.toLowerCase() + '[aria-label="' + esc(a) + '"]';
    const parts = [];
    while (el && el.nodeType === 1 && parts.length < 6) {
      let i = 1, s = el;
      while ((s = s.previousElementSibling)) if (s.tagName === el.tagName) i++;
      parts.unshift(el.tagName.toLowerCase() + ':nth-of-type(' + i + ')');
      el = el.parentElement;
    }
    return parts.join(' > ');
  };
  const modelish = /model|gpt|claude|gemini|glm|grok|llama|qwen|mistral|flash|\bpro\b|sonnet|opus|haiku|auto|thinking|instant|deepseek|\bo[134]\b/i;
  // the site's own shell (nav/sidebar) hides real pickers too sometimes, so
  // chrome only disqualifies the WEAK (name-based) candidates; an explicit
  // menu/listbox/combobox declaration counts wherever it lives, as long as it
  // is not the brand or a promo button (those opened the blank-tab storm)
  const inChrome = b => !!b.closest(
      'nav,[class*=sidebar],[class*=side-bar],[class*=left-panel],'
      + '[class*=app-list]');
  const promo = /download|install|get the|mobile|desktop|\bapp\b|extension|careers|blog|whatsapp|telegram|twitter|instagram/i;
  const brand = /^(chatgpt|gpt|claude ?ai?|glm|deepseek|gemini)[ *·•\-]*$/i;
  const label = b => (b.innerText || '') + ' ' + (b.getAttribute('aria-label')
      || '') + ' ' + (b.getAttribute('data-testid') || '') + ' ' +
      (b.getAttribute('title') || '');
  const isPicker = b => {
    const txt = (b.innerText || '').trim();
    if (brand.test(txt) || promo.test(label(b))) return false;
    const h = (b.getAttribute('aria-haspopup') || '').toLowerCase();
    if (h === 'dialog') return false;         // "Deep research", modals
    if (h === 'menu' || h === 'listbox' || h === 'true' || h === 'tree'
        || b.getAttribute('role') === 'combobox') return true;
    return modelish.test(label(b)) && !inChrome(b);
  };
  const cands = [...document.querySelectorAll(
      'button,[role=button],[role=combobox],[aria-haspopup]')].filter(b => {
    const t = (b.innerText || '').trim();
    return vis(b) && t.length > 0 && t.length < 60 && isPicker(b);
  });
  cands.sort((a, b) => modelish.test(label(b)) - modelish.test(label(a)));
  return {cands: cands.slice(0, 10).map(b => ({
             open: path(b),
             current: (b.innerText || '').trim().split('\n')[0],
             text: label(b).slice(0, 120)}))}; }"""

# One candidate: click its picker, read the menu rows, and only report them
# when they read like MODELS (a Tools/Projects popover must not pass).
_DETECT_ONE_JS = r"""async (a) => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const vis = e => { const r = e.getBoundingClientRect();
                     return r.width > 0 && r.height > 0; };
  const junk = /log ?out|sign ?out|settings|profile|account|help|upgrade|share|new chat|history|api key|data controls|\beffort\b|more models|\bmanage\b|\badd\b/i;
  const modelishRow = l => /gpt|sonnet|opus|haiku|gemini|glm|grok|llama|qwen|mistral|flash|deepseek|mini|\bo[134]\b|instant|thinking|auto|\d\.\d/i.test(l);
  const rows = () => {
    let els = [...document.querySelectorAll(
        '[role=menuitem],[role=option],[role=menuitemradio]')].filter(vis);
    if (els.length < 2) {            // some sites draw plain rows in a portal
      const cont = [...document.querySelectorAll(
          '[role=menu],[role=listbox],[data-radix-popper-content-wrapper],'
          + '[class*=menu-content],[class*=dropdown-content],[class*=popover]')]
        .filter(vis).pop();
      if (cont) els = [...cont.querySelectorAll('button,li,[role]')]
        .filter(e => vis(e) && (e.innerText || '').trim());
    }
    const out = [];
    for (const e of els) {
      const t = (e.innerText || '').trim().split('\n')[0].trim();
      if (t && t.length < 60 && !junk.test(t) && !out.includes(t)) out.push(t);
    }
    return out;
  };
  const _open = window.open;                   // promos cannot spawn tabs
  window.open = () => null;
  const orig = location.href;
  let b = a.open ? document.querySelector(a.open) : null;
  if (!b && a.current) {              // the saved path rotted: find the button
    const t = a.current.trim().toLowerCase();   // still showing that model name
    b = [...document.querySelectorAll(
        'button,[role=button],[role=combobox],[aria-haspopup]')]
      .find(e => vis(e) && (e.innerText || '').trim().toLowerCase()
        .split('\n')[0] === t);
  }
  if (!b) { window.open = _open; return {items: [], moved: false}; }
  b.click();
  let labels = [];
  for (let i = 0; i < 6; i++) {
    await sleep(300); labels = rows(); if (labels.length >= 2) break;
  }
  window.open = _open;
  const ok = labels.length >= 2 && labels.some(modelishRow);
  return {items: ok ? labels : [], all: labels,
          moved: location.href !== orig}; }"""

_TOGGLE_JS = """(a) => {
  const want = a.text.trim().toLowerCase();
  const els = [...document.querySelectorAll(
      'button,[role=switch],[role=button],[role=checkbox],label,'
      + '[class*=toggle]')]
    .filter(e => (e.innerText || '').trim().toLowerCase().startsWith(want));
  if (!els.length) return 'missing';
  const e = els[0];
  let on = null;
  for (const k of ['aria-pressed', 'aria-checked', 'aria-selected', 'data-state']) {
    const v = e.getAttribute(k);
    if (v !== null) { on = (v === 'true' || v === 'on' || v === 'checked'
                            || v === 'active'); break; }
  }
  if (on === null) on = /\\b(active|selected|checked|enabled)\\b/.test(e.className || '');
  if (on === a.want) return 'ok';
  e.click(); return 'clicked'; }"""


def _by_id(items, wanted):
    """The entry the run asked for; nothing asked = leave the site as it is."""
    if not wanted:
        return None
    for it in items or []:
        if it.get("id") == wanted:
            return it
    return None


def _generic_prepare(spec: dict):
    """Build the prepare(page, **options) for a site you defined: pick the
    model, the level, then set each switch. A step that cannot be done on the
    page is reported in the log and skipped - the run still asks."""
    def prepare(page, model=None, level=None, toggles=None, **_ignored):
        notes = []

        def pick(group, wanted, what):
            item = _by_id(spec.get(group), wanted)
            if not item or not item.get("item"):
                return
            try:
                opener = {"open": item.get("open") or "",
                          "current": item.get("current") or ""}

                def poll(rounds=4):
                    res = "missing"
                    for _ in range(rounds):
                        page.wait_for_timeout(400)
                        res = page.evaluate(_PICK_ITEM_JS, item["item"])
                        if res == "clicked":
                            break
                    return res

                def open_it():
                    r = page.evaluate(_CLICK_TEXT_JS, opener)
                    if r != "opened":
                        notes.append(f"{what}: picker not found")
                    return r == "opened"

                # the menu may already be open (right after a detection run:
                # clicking the trigger then CLOSES it), so try the row first,
                # open only if needed, and re-open if a toggle closed it
                res = poll(1)
                if res != "clicked" and open_it():
                    res = poll()
                    if res != "clicked" and open_it():
                        res = poll()
                page.wait_for_timeout(500)
                notes.append(f"{what} {item.get('label') or item['item']}"
                             + ("" if res == "clicked"
                                else " (menu entry not found)"))
            except Exception as exc:  # noqa: BLE001
                notes.append(f"{what}: {exc}"[:80])

        if spec.get("paid"):               # free accounts have no choice
            pick("models", model, "model")
        pick("levels", level, spec.get("level_label") or "level")
        for tg in spec.get("toggles") or []:
            want = bool((toggles or {}).get(tg["id"], False))
            try:
                res = page.evaluate(_TOGGLE_JS, {"text": tg.get("text")
                                                 or tg.get("label"),
                                                 "want": want})
                if res == "clicked":
                    page.wait_for_timeout(300)
                notes.append(f"{tg.get('label')} {'on' if want else 'off'}"
                             + (" (switch not found)" if res == "missing"
                                else ""))
            except Exception as exc:  # noqa: BLE001
                notes.append(f"{tg.get('label')}: {exc}"[:80])
        page.wait_for_timeout(200)
        return f"{spec['name']}: " + ", ".join(notes) if notes else ""
    return prepare


# Best-guess defaults for a site you only gave a name and URL. They work on
# the common chat layouts; a site that needs different ones can be given them
# in its definition (box / send / reply / generating / sent_part).
DEFAULT_REPLY = ('[data-message-author-role="assistant"], '
                 '[class*="assistant" i], [class*="markdown" i]')
DEFAULT_GENERATING = ('[aria-label*="Stop" i], [title*="Stop" i], '
                      '[data-testid*="stop" i], [class*="stop-generat" i]')

_GENERIC_SEND_JS = """() => {
  const list = [...document.querySelectorAll(%s)];
  const ta = list.find(e => e.getBoundingClientRect().width > 0) || list[0] || null;
  // a WHOLE word of the accessible name: "Ask" must not match "Task",
  // "stop" must not match "Deep research"; ChatGPT's composer button is
  // named only by its class, so a type=submit inside the composer counts
  const word = w => new RegExp('\\\\b(' + w + ')\\\\b', 'i');
  const att = x => [x.getAttribute('aria-label'), x.getAttribute('title'),
      x.getAttribute('data-testid'), x.id, (x.innerText || '').trim()];
  const send = word('send|submit|ask|generate');
  const no = word('stop|continue|up arrow|model|deep|research');
  const btns = el => [...el.querySelectorAll('button,[role=button]')];
  let scope = document;
  if (ta) {
    scope = ta.closest('form');
    if (!scope) {
      // the send button is NOT always near the box (Claude's contenteditable
      // sits 4+ levels below the composer): climb until a send-like button
      // is inside, at most 8 levels
      let el = ta.parentElement, i = 0;
      while (el && i < 8 && !btns(el).some(x => att(x).some(
          v => v && send.test(v) && !no.test(v)))) { el = el.parentElement; i++; }
      scope = (el && i < 8) ? el : document;
    }
  }
  const cands = [...scope.querySelectorAll('button,[role=button]')].filter(
      b => !b.disabled && b.getAttribute('aria-disabled') !== 'true');
  let b = cands.find(x => att(x).some(v => v && send.test(v) && !no.test(v)));
  if (!b && ta && ta.closest('form'))
    b = [...ta.closest('form').querySelectorAll('button[type=submit]')]
        .find(x => !x.disabled && !att(x).some(v => v && no.test(v)));
  if (!b) b = cands[cands.length - 1];
  if (!b) return false;
  b.click(); return true; }"""


def site_from_def(d: dict) -> Site:
    box = d.get("box") or ("textarea, [contenteditable=true], [role=textbox]")
    sel_stop = d.get("generating") or DEFAULT_GENERATING
    sent = d.get("sent_part") or ""
    login = tuple(x.strip() for x in str(d.get("login_part") or "").split(",")
                  if x.strip()) or ("sign_in", "login", "auth", "signin")
    if d.get("send"):
        send_js = ("() => { const b=document.querySelector(%s);"
                   " if(!b) return false; b.click(); return true; }"
                   % json.dumps(d["send"]))
    else:
        send_js = _GENERIC_SEND_JS % json.dumps(box)
    if sent:
        sent_js = "() => location.pathname.includes(%s)" % json.dumps(sent)
    else:
        # a started chat has its own address; the start page does not
        from urllib.parse import urlparse
        base = urlparse(d["url"]).path or "/"
        sent_js = ("() => location.pathname.length > 1 && "
                   "location.pathname !== %s" % json.dumps(base))
    return Site(
        key=d["key"], name=d["name"], url=d["url"], box=box,
        reply=d.get("reply") or DEFAULT_REPLY, send_js=send_js,
        generating_js="() => !!document.querySelector(%s)"
                      % json.dumps(sel_stop),
        login_url_part=login, stream=d.get("stream") or "",
        sent_js=sent_js, prepare=_generic_prepare(d))


def sanitize_sites(raw) -> tuple[list[dict], list[str]]:
    """Clean what the settings editor posts. Returns (definitions, problems);
    entries that cannot work are dropped with a problem message."""
    out, problems, used = [], [], set(BUILTIN_KEYS)
    ports = set(CDP_PORTS.values())
    for d in raw if isinstance(raw, list) else []:
        if not isinstance(d, dict):
            continue
        name = str(d.get("name") or "").strip()
        if not name:
            if str(d.get("url") or "").strip():
                problems.append("an entry has an address but no name")
            continue
        key = _SLUG.sub("-", str(d.get("key") or name).lower()).strip("-")
        if not key or key in used:
            problems.append(f"{name}: the key '{key}' is already taken")
            continue
        clean = {"key": key, "name": name,
                 "url": str(d.get("url") or "").strip(),
                 "box": str(d.get("box") or "").strip(),
                 "reply": str(d.get("reply") or "").strip(),
                 "send": str(d.get("send") or "").strip(),
                 "generating": str(d.get("generating") or "").strip(),
                 "login_part": str(d.get("login_part") or "").strip(),
                 "sent_part": str(d.get("sent_part") or "").strip(),
                 "stream": str(d.get("stream") or "").strip(),
                 "level_label": str(d.get("level_label") or "").strip()
                 or "Level",
                 "paid": bool(d.get("paid"))}
        if not clean["url"].startswith(("http://", "https://")):
            problems.append(f"{name}: needs a web address starting with "
                            f"https://")
            continue

        def group(items, ident):
            res, seen = [], set()
            for it in items if isinstance(items, list) else []:
                if not isinstance(it, dict):
                    continue
                label = str(it.get("label") or it.get("item")
                            or it.get("text") or "").strip()
                if not label:
                    continue
                iid = _SLUG.sub("-", str(it.get("id") or label).lower()
                                ).strip("-") or ident
                if iid in seen:
                    continue
                seen.add(iid)
                row = {"id": iid, "label": label}
                for f in ("open", "item", "text", "current"):
                    if str(it.get(f) or "").strip():
                        row[f] = str(it[f]).strip()
                res.append(row)
            return res

        clean["models"] = group(d.get("models"), "model")
        clean["levels"] = group(d.get("levels"), "level")
        clean["toggles"] = group(d.get("toggles"), "toggle")
        try:
            port = int(d.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if port < 1024 or port in ports:
            port = CUSTOM_PORT_START
            while port in ports:
                port += 1
        ports.add(port)
        clean["port"] = port
        used.add(key)
        out.append(clean)
    return out, problems


def set_custom_sites(defs) -> list[str]:
    """Make exactly these user-defined sites available (replacing the
    previous ones). Built-in z.ai / DeepSeek are never touched."""
    for key in [k for k in SITES if k not in BUILTIN_KEYS]:
        SITES.pop(key, None)
        CDP_PORTS.pop(key, None)
    cleaned, _problems = sanitize_sites(defs)
    for d in cleaned:
        SITES[d["key"]] = site_from_def(d)
        CDP_PORTS[d["key"]] = d["port"]
    return [d["key"] for d in cleaned]


def custom_sites_ui() -> list[dict]:
    """What the run forms need to draw the controls of each custom site."""
    out = []
    for key, site in SITES.items():
        if key in BUILTIN_KEYS:
            continue
        out.append(_UI.get(key) or {"key": key, "name": site.name})
    return out


_UI: dict = {}


def load_custom_sites(conn) -> list[str]:
    """Read the saved definitions from the settings table and activate them."""
    from . import db
    try:
        raw = json.loads(db.get_setting(conn, "webchat_sites") or "[]")
    except ValueError:
        raw = []
    defs, _ = sanitize_sites(raw)
    keys = set_custom_sites(defs)
    _UI.clear()
    for d in defs:
        _UI[d["key"]] = {k: d[k] for k in
                         ("key", "name", "models", "levels", "level_label",
                          "toggles", "paid")}
    return keys

CONTINUE_LABELS = ("continue generating", "continue", "weiter")

_BOT_JS = """() => { const t=(document.body.innerText||'').toLowerCase();
  const ti=(document.title||'').toLowerCase();
  return /verify (that )?you are (a )?human|are you a robot|complete the captcha|security check|checking your browser|performing security verification|verifies you are not a human|verifies you are not a bot/.test(t)
         || /just a moment|verify you are human|pardon our interruption/.test(ti)
         || !!document.querySelector('iframe[src*=captcha],iframe[src*=turnstile],#cf-challenge-running'); }"""

# Words of a "model is busy" banner. Matched only in short elements OUTSIDE the
# reply and the (long) prompt. Every banner element seen is MARKED in the DOM
# and only UNMARKED elements are reported: an old banner left in the chat is
# never mistaken for the answer to a new message, and a NEW banner re-rendered
# with the same wording still counts (it is a different element).
BUSY_RE = (r"at capacity|over capacity|currently busy|server (is )?busy|"
           r"too many (requests|users)|try again (later|in a)|"
           r"concurrent conversation limit|overloaded|rate limit|peak hours|"
           r"coordination of resources")

_BUSY_JS = """(args) => {
  const re=new RegExp(args.re,'i'), MARK='data-wr-busy';
  const replies=[...document.querySelectorAll(args.reply)];
  let n=0, text='';
  for (const e of document.querySelectorAll('div,p,span,li,[role=alert]')) {
    if (e.children.length>2) continue;
    const t=(e.innerText||'').trim();
    if (!t || t.length>220 || !re.test(t)) continue;
    if (replies.some(r=>r.contains(e))) continue;
    const r=e.getBoundingClientRect(); if(!r.width||!r.height) continue;
    if (e.hasAttribute(MARK)) continue;
    e.setAttribute(MARK, '1');
    n++; text=t; }
  return {n:n, text:text}; }"""

# z.ai's "Currently in peak hours - switch to GLM-5.3-Flash" pop-up. It is
# dismissed with Cancel / Close ONLY: the "Switch" button changes the model.
_LIMIT_JS = r"""() => {
  const re = /(chat|conversation) (is )?paused|paused until|usage (limit|resets)|reached (the |your )?(free |daily |usage |message )?limit|limit (reached|resets)/i;
  const replies = [...document.querySelectorAll(%s)];
  for (const e of document.querySelectorAll('div,p,span,li,[role=alert]')) {
    if (e.children.length > 3) continue;
    const t = (e.innerText || '').trim();
    if (!t || t.length > 220 || !re.test(t)) continue;
    if (replies.some(r => r.contains(e))) continue;
    if (e.closest('[data-message-author-role=user]')) continue;
    const r = e.getBoundingClientRect(); if (!r.width || !r.height) continue;
    return t; }
  return ''; }"""


def _limit_banner(page, site: Site) -> str:
    """A "Chat paused until usage resets at 2:37 PM" style notice (free plan
    limit, e.g. after ChatGPT drew an image). Unlike `_busy` this ignores the
    'already on the page' marks: the notice is usually there BEFORE the send
    that then silently does nothing."""
    try:
        return str(page.evaluate(_LIMIT_JS % json.dumps(site.reply)) or "")
    except Exception:  # noqa: BLE001
        return ""


_AGE_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect();
                     return r.width > 0 && r.height > 0; };
  // the button may be a <div role=button> or a styled <span>: take the
  // smallest visible element whose whole text is "Continue"
  const b = [...document.querySelectorAll('button,[role=button],div,span,a')]
    .filter(x => vis(x) && /^continue$/i.test((x.innerText || '').trim()))
    .sort((p, q) => p.querySelectorAll('*').length - q.querySelectorAll('*').length)[0];
  if (!b) return '';
  let c = b, ok = false;
  for (let i = 0; i < 8 && c.parentElement; i++) {
    c = c.parentElement;
    if (/confirm your age|year were you born|date of birth/i.test(c.innerText || '')) { ok = true; break; }
  }
  if (!ok) return '';
  // only confirm what the page already shows (a year is filled in); never
  // pick a birth year on the user's behalf
  if (!/\b(19|20)\d{2}\b/.test(c.innerText || '')) return 'needs-year';
  b.click(); return 'clicked'; }"""


def _dismiss_age(page, log) -> bool:
    """Qwen asks "Confirm your age to continue" again and again and blocks the
    send behind it: press Continue when a year is already filled in."""
    try:
        res = page.evaluate(_AGE_JS)
    except Exception:  # noqa: BLE001
        return False
    if res == "clicked":
        log("age confirmation pop-up: pressed Continue (the year was already filled in)")
        page.wait_for_timeout(800)
        return True
    if res == "needs-year":
        log("an age confirmation pop-up needs a birth year - choose it once "
            "in the sign-in window")
    return False


_PEAK_JS = """(args) => {
  const re=new RegExp(args.re,'i');
  for (const d of document.querySelectorAll(
        '[role=dialog],[role=alertdialog],.modal,[class*=dialog],[class*=modal]')) {
    const r=d.getBoundingClientRect(); if(!r.width||!r.height) continue;
    const t=(d.innerText||'').trim();
    if (!t || t.length>600 || !re.test(t)) continue;
    let clicked='';
    if (args.dismiss) {
      for (const b of d.querySelectorAll('button,[role=button]')) {
        const bt=(b.innerText||b.getAttribute('aria-label')||'').trim().toLowerCase();
        if (/switch/.test(bt)) continue;
        if (/^(cancel|close|not now|dismiss|no thanks|x|×)$/.test(bt)) {
          b.click(); clicked=bt; break; }
      }
    }
    return {text:t.slice(0,200), clicked:clicked};
  }
  return null; }"""

_PASTE_JS = """(args) => {
  const list = [...document.querySelectorAll(args.box)];
  const ta = list.find(e => e.getBoundingClientRect().width > 0) || list[0];
  if (!ta) return -1;
  if (ta.isContentEditable) {                 // Claude's composer et al.
    ta.focus();
    try { const sel=window.getSelection(), r=document.createRange();
          r.selectNodeContents(ta); sel.removeAllRanges(); sel.addRange(r);
          document.execCommand('insertText', false, args.text); }
    catch (e) { ta.textContent = args.text; }
    return (ta.innerText||ta.textContent||'').replace(/\\u200b/g,'').length; }
  const proto = ta.tagName === 'INPUT' ? HTMLInputElement : HTMLTextAreaElement;
  const set=Object.getOwnPropertyDescriptor(proto.prototype,'value').set;
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

_TA_LEN_JS = """(sel) => { const list=[...document.querySelectorAll(sel)];
  const t=list.find(e => e.getBoundingClientRect().width > 0) || list[0];
  if(!t) return -1;
  if (t.isContentEditable) return (t.textContent||'').length;
  return (t.value || '').length; }"""
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


def _busy(page, site: Site) -> dict:
    out = page.evaluate(_BUSY_JS, {"re": BUSY_RE, "reply": site.reply})
    return out if isinstance(out, dict) else {"n": 0, "text": ""}


def _peak_dialog(page, dismiss: bool = True):
    try:
        out = page.evaluate(_PEAK_JS, {"re": BUSY_RE, "dismiss": dismiss})
    except Exception:  # noqa: BLE001
        return None
    return out if isinstance(out, dict) else None


def retry_when_busy(call, log, sleep=time.sleep, should_stop=None,
                    first_wait: float = 30.0, max_wait: float = 300.0):
    """Run `call()`; when the model is at capacity wait (30s, 60s ... 5 min)
    and ask again on the same model, until it answers or you stop the run."""
    attempt = 0
    while True:
        try:
            return call()
        except ModelBusy as exc:
            attempt += 1
            wait = min(first_wait * 2 ** (attempt - 1), max_wait)
            log(f"{exc} - the model is busy; waiting {int(wait)}s, then "
                f"asking again (attempt {attempt}). The model is NOT changed.")
            waited = 0.0
            while waited < wait:
                if should_stop is not None and should_stop():
                    raise WebChatError("stopped by you while waiting for "
                                       "the model to have capacity")
                sleep(1.0)
                waited += 1.0


# ---- one conversation -----------------------------------------------------------

def _ask_inner(page, site: Site, prompt: str, files: Sequence[str] = (),
        timeout: float = 900, settle: float = 6.0, poll: float = 2.0,
        continue_max: int = 8, start_wait: float = 90.0,
        new_chat: bool = True, options: Optional[dict] = None,
        ready: Optional[Callable[[str], bool]] = None, ready_wait: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = lambda m: None,
        max_total: float = 4 * 3600, resume: bool = False,
        stop: Optional[Callable[[], bool]] = None) -> str:
    """`timeout` is a STALL limit: seconds with no new text and no sign of
    thinking/writing. A reply that keeps going is waited for (up to
    `max_total`). `resume=True` sends nothing: it collects the answer of the
    chat that is already open (after a reload or a stall).
    Send `prompt` (+ `files`, local paths) and return the finished reply.
    A NEW chat by default; `new_chat=False` answers inside the chat that is
    already open (feedback to the writer). Raises NeedsSignIn, BotCheck or
    WebChatTimeout."""
    if not resume and not (prompt or "").strip():
        raise WebChatError("Nothing to send")
    if resume:
        _wait_for_box(page, site, clock)
        if site.stream:
            page.evaluate(_CAPTURE_JS)
        st0 = page.evaluate(_STATE_JS, {"reply": site.reply})
        c, old_text = st0["count"], ""
        before = max(0, c - 1)             # the last reply is the one we want
        msgs_before = 0
        _busy(page, site)          # mark any banner already on the page
        start_wait = min(start_wait, 30.0)
    elif new_chat:
        _goto(page, site.url)
    _wait_for_box(page, site, clock)
    if site.stream:
        page.evaluate(_CAPTURE_JS)         # no-op when already hooked
    if not resume:
        if new_chat and site.prepare:
            status = site.prepare(page, **(options or {}))
            if isinstance(status, str) and status:
                log(status)

        if files:
            seen = _name_counts(page, files)
            page.set_input_files(site.files, [str(f) for f in files])
            _wait_for_uploads(page, files, clock, log, seen=seen)

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
        st0 = page.evaluate(_STATE_JS, {"reply": site.reply})
        before, old_text = st0["count"], (st0["text"] or "")
        if site.stream:
            page.evaluate(_CAP_RESET_JS)
        msgs_before = page.evaluate(_MSG_COUNT_JS) or 0
        _busy(page, site)                  # mark banners already on the page
        # A send is only repeated when there is NO sign it went out: the box still
        # holds the text, the address did not change, nothing is generating, no
        # new message appeared. A big prompt with attachments can take well over
        # ten seconds to be accepted, and a second click on a send button that has
        # turned into a stop button would cancel the answer.
        peak = None
        _dismiss_age(page, log)
        for attempt in (1, 2, 3):
            how = _press_send(page, site, log)
            if not how:
                raise WebChatError(
                    f"{site.name}: could not find the send button - "
                    + _composer_report(page, site))
            took = False
            age_cleared = False
            for _ in range(30):
                page.wait_for_timeout(1000)
                if _dismiss_age(page, log):
                    age_cleared = True       # the pop-up ate the send: redo it
                    break
                pk = _peak_dialog(page)
                if pk:
                    peak = pk
                    log(f"{site.name}: pop-up \"{pk['text'][:90]}\" - closed "
                        f"it ({pk['clicked'] or 'could not'}); the model is "
                        f"NOT changed")
                    break
                why = ("the box emptied" if page.evaluate(_TA_LEN_JS, site.box) == 0
                       else "it is generating" if page.evaluate(site.generating_js)
                       else "the address changed" if (
                           new_chat and page.evaluate(site.sent_js))
                       else "a new message appeared" if (
                           (page.evaluate(_MSG_COUNT_JS) or 0) > msgs_before)
                       else "a new reply element appeared" if page.evaluate(
                           _STATE_JS, {"reply": site.reply})["count"] > before
                       else "")
                if why:
                    log(f"{site.name}: send registered ({why})")
                    took = True
                    break
            if took:
                break
            if age_cleared:
                continue
            if peak:
                raise ModelBusy(f"{site.name} says: \"{peak['text'][:140]}\"")
            lim = _limit_banner(page, site)
            if lim:
                raise ModelBusy(f"{site.name} says: \"{lim[:140]}\"")
            try:
                sig = {"box_chars": page.evaluate(_TA_LEN_JS, site.box),
                       "generating": bool(page.evaluate(site.generating_js)),
                       "new_address": bool(page.evaluate(site.sent_js)),
                       "messages": page.evaluate(_MSG_COUNT_JS),
                       "replies": page.evaluate(
                           _STATE_JS, {"reply": site.reply})["count"],
                       "replies_before": before}
            except Exception:  # noqa: BLE001
                sig = {}
            log(f"{site.name}: the send did not register (try {attempt}/3) "
                f"{sig}")

    t0 = clock()
    last_text, stable_since, continues = "", clock(), 0
    started = False
    probed = False
    active = clock()                       # last sign of life
    while True:
        page.wait_for_timeout(int(poll * 1000))
        if stop is not None and stop():
            raise WebChatError(f"{site.name}: stopped by you while waiting")
        if page.evaluate(_BOT_JS):
            raise BotCheck(f"{site.name} is asking for a human check - open "
                           f"the browser window and complete it yourself")
        st = page.evaluate(_STATE_JS, {"reply": site.reply})
        text = st["text"] or ""
        gen = bool(page.evaluate(site.generating_js))
        if not gen and not clean_reply(text):
            pk = _peak_dialog(page)
            if pk:
                raise ModelBusy(f"{site.name} says: \"{pk['text'][:140]}\" "
                                f"(pop-up closed)")
            busy = _busy(page, site)
            if busy["n"]:
                raise ModelBusy(f"{site.name} says: \"{busy['text'][:140]}\"")
        elif not gen and clean_reply(text) and clock() - stable_since > 60:
            # the reply died part-way and a NEW capacity banner appeared while
            # it sat stalled: ask again instead of waiting out the stall limit
            busy = _busy(page, site)
            if busy["n"]:
                raise ModelBusy(f"{site.name} says: \"{busy['text'][:140]}\" "
                                f"- the reply stopped after "
                                f"{len(clean_reply(text).split())} word(s)")
        if not started:
            # the LAST reply on the page is still the PREVIOUS answer until
            # the new one starts: its text must not count as the answer
            if st["count"] > before or gen or (text and text != old_text):
                started = True
            elif not probed and clock() - t0 > 20:
                # nothing yet: say where the chat is. A site may open the new
                # chat in ANOTHER tab/page (the watched page then stays on
                # the empty start page) - follow it there.
                probed = True
                try:
                    pages = list(page.context.pages)
                    log(f"{site.name}: no answer yet after 20 s; this page: "
                        f"{page.url}; open pages: {[p.url for p in pages]}")
                    for p in pages:
                        if p is not page and p.evaluate(site.sent_js):
                            page = p
                            log(f"{site.name}: the chat is open in another "
                                f"page - switched to it")
                            break
                except Exception:  # noqa: BLE001
                    pass
            elif clock() - t0 > start_wait:
                lim = _limit_banner(page, site)
                if lim:
                    raise ModelBusy(f"{site.name} says: \"{lim[:140]}\"")
                tail = " ".join(str(page.evaluate(_PAGE_TAIL_JS)
                                    or "").split())[-240:]
                raise WebChatTimeout(
                    f"{site.name} did not start answering. The page ends "
                    f"with: \"{tail}\"", "")
        if text != last_text:
            last_text, stable_since = text, clock()
            active = clock()
        if gen:
            active = clock()               # still thinking / writing
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
        if started and clock() - active > timeout:
            raise WebChatTimeout(
                f"{site.name} made no progress for {int(timeout)}s",
                clean_reply(last_text))
        if clock() - t0 > max_total:
            raise WebChatTimeout(
                f"{site.name} did not finish within {int(max_total)}s",
                clean_reply(last_text))


# the prompt box must be VISIBLE: pages carry hidden off-screen textareas,
# and scoping the send/paste to one of those is how "no send button" happened
_BOX_THERE_JS = """(s) => {
  const q = document.querySelector(s);
  if (!q) return false;
  const r = q.getBoundingClientRect();
  if (r.width > 0 || r.height > 0) return true;
  return [...document.querySelectorAll(s)].some(
      e => { const b = e.getBoundingClientRect();
             return b.width > 0 || b.height > 0; }); }"""



def _goto(page, url, wait: float = 60.0) -> None:
    """Open a page and return once its HTML is there. Playwright's default
    waits for the `load` event, which chat sites with endless analytics /
    streaming requests (Qwen) never fire within 30 s - the box check after it
    already waits for the real thing."""
    page.goto(url, wait_until="domcontentloaded", timeout=int(wait * 1000))



def _press_send(page, site: Site, log, wait: float = 12.0) -> str:
    """Click the send button; '' when there is none. The button is often
    disabled for a moment after a big paste (the page's framework has not seen
    the text yet), so look again for a while before giving up, and as a last
    resort press Enter in the box (every chat site sends on Enter)."""
    for _ in range(max(1, int(wait / 0.5))):
        if page.evaluate(site.send_js):
            return "button"
        page.wait_for_timeout(500)
    try:
        page.evaluate("""(sel) => { const l = [...document.querySelectorAll(sel)];
            const e = l.find(x => x.getBoundingClientRect().width > 0) || l[0];
            if (e) e.focus(); }""", site.box)
        page.keyboard.press("Enter")
    except Exception:  # noqa: BLE001
        return ""
    log(f"{site.name}: no send button found - pressed Enter in the box")
    return "enter"


_COMPOSER_JS = r"""(sel) => {
  const list = [...document.querySelectorAll(sel)];
  const ta = list.find(e => e.getBoundingClientRect().width > 0) || list[0]
             || null;
  const btns = el => [...el.querySelectorAll('button,[role=button]')];
  let scope = document;
  if (ta) {
    scope = ta.closest('form');
    if (!scope) {
      let el = ta.parentElement, i = 0;
      while (el && i < 8 && btns(el).length < 2) { el = el.parentElement; i++; }
      scope = el || document;
    }
  }
  return btns(scope).slice(0, 14)
    .map(b => (b.getAttribute('aria-label') || b.getAttribute('data-testid')
               || (b.innerText || '').trim() || b.tagName).slice(0, 30)
         + (b.disabled || b.getAttribute('aria-disabled') === 'true'
            ? ' (disabled)' : '')); }"""


def _composer_report(page, site: Site) -> str:
    """What buttons sit around the prompt box - for the 'no send button' error,
    so the log shows why (all disabled? a limit banner? a different layout?)."""
    try:
        names = page.evaluate(_COMPOSER_JS, site.box) or []
    except Exception:  # noqa: BLE001
        names = []
    return ("buttons near the box: " + "; ".join(names)) if names \
        else "no buttons near the box"


_DIAG_ROOT: Optional[Path] = None


def _save_last(page, site: Site, outcome: str) -> None:
    """Keep what the page looked like when a run ended (screenshot + the page
    text + how it ended) in <profile root>/diag/<site>/last-run.*, so a reply
    that was not picked up can be understood afterwards."""
    if _DIAG_ROOT is None:
        return
    try:
        d = _DIAG_ROOT / "diag" / site.key
        d.mkdir(parents=True, exist_ok=True)
        body = page.evaluate("() => (document.body.innerText || '')") or ""
        names = ["last-run"] + ([] if outcome.startswith("ok")
                                else ["last-failure"])
        for n in names:                  # a failure is kept past later runs
            (d / f"{n}.txt").write_text(
                f"outcome: {outcome}\nurl: {page.url}\n\n{str(body)[-30000:]}",
                encoding="utf-8")
            page.screenshot(path=str(d / f"{n}.png"))
    except Exception:  # noqa: BLE001 - diagnostics must never break a run
        pass


def ask(page, site: Site, *args, **kw) -> str:
    try:
        text = _ask_inner(page, site, *args, **kw)
    except BaseException as exc:
        _save_last(page, site, f"{type(exc).__name__}: {str(exc)[:300]}")
        raise
    _save_last(page, site, f"ok, {len(text)} characters")
    return text


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
        if page.evaluate(_BOX_THERE_JS, site.box):
            return
        if clock() - t0 > limit:
            raise NeedsSignIn(
                f"{site.name}: the chat box never appeared - sign in with "
                f"`python -m whisperradar.webchat login {site.key}`")
        page.wait_for_timeout(1000)


def _name_counts(page, files) -> dict:
    body = page.evaluate("()=>document.body.innerText") or ""
    return {Path(str(f)).name: body.count(Path(str(f)).name) for f in files}


def _wait_for_uploads(page, files, clock, log, limit: float = 120.0,
                      seen: Optional[dict] = None, settle: float = 3.0) -> None:
    """Wait until every NEW attachment shows in the chat. In a chat that
    already mentions narration.txt / shotlist.json the bare name is on the
    page from the start, so a file only counts once its name appears MORE
    often than before the upload (else the send goes out without it)."""
    names = [Path(str(f)).name for f in files]
    seen = seen or {}
    t0 = clock()
    while True:
        body = page.evaluate("()=>document.body.innerText") or ""
        if all(body.count(n) > seen.get(n, 0) for n in names):
            page.wait_for_timeout(int(settle * 1000))   # let the upload finish
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
            "--no-first-run", "--no-default-browser-check",
            # a fresh, quiet window: no synced extensions (a wallet extension
            # opened its own blank tab in front of the chat), no sync prompt
            "--disable-extensions", "--disable-sync",
            "--disable-features=ChromeWhatsNewUI", SITES[key].url]
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    proc = subprocess.Popen(args, creationflags=flags, close_fds=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.monotonic()
    while time.monotonic() - t0 < wait:
        if cdp_alive(key):
            return True
        if proc.poll() is not None:
            # the process we launched exited: a Chrome already uses this
            # profile and swallowed our request (forwarding) - a debugging
            # port can never come up while those windows stay open
            return False
        time.sleep(0.5)
    return False


def profile_busy(profile) -> bool:
    """A browser process running with this --user-data-dir? It locks the
    profile: a new launch would forward to it (nothing happens) or make a
    second stray window - never a clean start."""
    if os.name != "nt":
        lock = Path(profile) / "SingletonLock"
        return lock.exists()
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter "
             "\"Name='chrome.exe' or Name='msedge.exe'\" | "
             "ForEach-Object CommandLine"],
            capture_output=True, text=True, timeout=12).stdout or ""
    except Exception:  # noqa: BLE001 - best effort
        return False
    want = str(profile).lower().strip('"')
    return any(want in line.lower() for line in out.splitlines()
               if line.strip())


def launch_persistent(pw, site: Site, profile_root, headless: bool = False,
                      channel: str | None = None):
    """The app's own persistent browser for a site - the same window that
    z.ai/DeepSeek run in and that ChatGPT/Claude accept (headed real Chrome
    passes their bot checks; a debugging port is NOT needed)."""
    profile = Path(profile_root) / site.key
    profile.mkdir(parents=True, exist_ok=True)
    if not cdp_alive(site.key) and profile_busy(profile):
        raise WebChatError(
            f"{site.name}: a browser window is still open on its sign-in "
            f"profile. Close it (your sign-in is kept) and try again")
    kw = {"headless": headless,
          "viewport": {"width": 1100, "height": 900},
          # Google refuses sign-in in a browser that announces
          # itself as automated; drop those tells.
          "ignore_default_args": ["--enable-automation"],
          "args": ["--disable-blink-features=AutomationControlled"]}
    # Prefer the real installed Chrome, then Edge, then Playwright's
    # own Chromium (the first two pass Google's sign-in check).
    last: Exception | None = None
    for ch in ([channel] if channel else ["chrome", "msedge", None]):
        if ch:
            kw["channel"] = ch
        else:
            kw.pop("channel", None)
        try:
            return pw.chromium.launch_persistent_context(str(profile), **kw)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if channel:
                raise
    raise last or WebChatError("could not launch a browser")


def open_sign_in_window(key: str, profile_root) -> None:
    """Open the site in the very browser a run uses and hold it while you
    sign in; close the window when done - the profile keeps the session."""
    if key not in SITES:
        raise WebChatError(f"Unknown chat site '{key}'")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise WebChatError("Playwright is not installed") from exc
    site = SITES[key]
    if cdp_alive(key):
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_endpoint(key))
            ctx = browser.contexts[0] if browser.contexts \
                else browser.new_context()
            for pg in ctx.pages:
                if site.url.split("//")[-1].split("/")[0] in str(pg.url):
                    pg.bring_to_front()
                    return
            pg = ctx.new_page()
            _goto(pg, site.url)
            pg.bring_to_front()
        return
    with sync_playwright() as pw:
        ctx = launch_persistent(pw, site, profile_root)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            _goto(page, site.url)
        except Exception:  # noqa: BLE001 - the window shows whatever loaded
            pass
        page.bring_to_front()
        while ctx.pages:                 # hold the window while you sign in
            time.sleep(0.5)
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass


class WebChat:
    """A persistent browser profile per site, so you sign in once.

        with WebChat(profile_root) as chat:
            reply = chat.ask("zai", prompt, files=[...])
    """

    def __init__(self, profile_root, headless: bool = False,
                 channel: str | None = None):
        self.profile_root = Path(profile_root)
        global _DIAG_ROOT
        _DIAG_ROOT = self.profile_root
        self.headless = headless
        self.channel = channel          # e.g. "chrome" / "msedge", or None
        self._pw = None
        self._ctx: dict = {}
        self._hooked: set = set()
        self._attached: set = set()     # sites driven in the user's Chrome
        self.should_stop = None         # () -> bool, checked while waiting
        self._urls: dict = {}       # site -> URL of its current chat

    def _launch(self, key: str):
        return launch_persistent(self._pw, SITES[key], self.profile_root,
                                 self.headless, self.channel)

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
            # a debugging Chrome (started with `login`, or a leftover that
            # DID accept the port): drive that instead of opening a window
            browser = self._pw.chromium.connect_over_cdp(cdp_endpoint(key))
            self._ctx[key] = (browser.contexts[0] if browser.contexts
                              else browser.new_context())
            self._attached.add(key)
        if key not in self._ctx:
            self._ctx[key] = self._launch(key)
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

    def detect_models(self, key: str) -> dict:
        """Open the site and read its model picker: {open, current, items}.
        `items` is empty when no model menu could be found. Candidate buttons
        are tried one by one from here, each on a clean page and with a REAL
        Escape between them - a synthetic Escape does not reach React portals,
        and one menu left open makes every later candidate click a no-op."""
        page, site = self._page(key), SITES[key]
        for attempt in range(3):               # headers fill their name late
            _goto(page, site.url)
            _wait_for_box(page, site, time.monotonic)
            page.wait_for_timeout(1500 + 2000 * attempt)
            try:
                listing = page.evaluate(_DETECT_MODELS_JS)
            except Exception as exc:  # noqa: BLE001
                return {"open": "", "current": "", "items": [],
                        "error": f"{type(exc).__name__}: {exc}"[:160]}
            cands = listing.get("cands") if isinstance(listing, dict) else []
            for c in cands or []:
                try:
                    one = page.evaluate(_DETECT_ONE_JS,
                                        {"open": c.get("open"),
                                         "current": c.get("current")})
                except Exception:  # noqa: BLE001
                    one = {"items": []}
                if one.get("items"):
                    return {"open": c.get("open"), "current": c.get("current"),
                            "items": one["items"]}
                if one.get("moved"):           # a click that navigated: back
                    _goto(page, site.url)
                    _wait_for_box(page, site, time.monotonic)
                    page.wait_for_timeout(1200)
                else:
                    try:
                        page.keyboard.press("Escape")   # the real dismiss
                        page.wait_for_timeout(400)
                    except Exception:  # noqa: BLE001
                        pass
            if cands:
                break                          # candidates existed; no retry
        return {"open": "", "current": "", "items": []}

    def adopt(self, key: str, url: str) -> None:
        """Continue an earlier chat: the next `new_chat=False` ask opens `url`."""
        if url:
            self._urls[key] = url

    def urls(self) -> dict:
        """The URL of each site's current chat (to keep for a later stage)."""
        return dict(self._urls)

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
                    _goto(page, saved)
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
        log = kw.get("log") or (lambda m: None)
        kw.setdefault("stop", self.should_stop)
        try:
            return self._ask_checked(page, site, key, prompt, files, new_chat,
                                     options, kw, log)
        except WebChatError:
            raise
        except Exception as exc:  # noqa: BLE001
            if "has been closed" in str(exc) or "Target closed" in str(exc):
                raise WebChatError(
                    f"{site.name}: the browser window was closed, so the run "
                    f"stopped (close it only after the run is done)") from exc
            raise

    def _ask_checked(self, page, site, key, prompt, files, new_chat, options,
                     kw, log):
        try:
            reply = retry_when_busy(
                lambda: ask(page, site, prompt, files, new_chat=new_chat,
                            options=options, **kw),
                log, should_stop=self.should_stop)
        except WebChatTimeout as exc:
            reply = self._recover_timeout(page, site, exc, kw, log)
        try:
            if page.evaluate(site.sent_js):
                self._urls[key] = page.url.split("#")[0]
        except Exception:  # noqa: BLE001
            pass
        return reply

    def _recover_timeout(self, page, site, exc, kw, log, rounds: int = 3):
        """A stall is not the end: reload the same chat and collect what the
        site has by now (the answer may be finished, or still coming)."""
        last = exc
        try:
            started = bool(page.evaluate(site.sent_js))
        except Exception:  # noqa: BLE001
            started = True
        if not started:
            # still on the empty start page: the prompt never went out.
            # Reloading it would open the LAST chat of the site (Qwen does)
            # and hand back an old answer as if it were the new one.
            raise WebChatError(
                f"{site.name}: the prompt never went out - the page is still "
                f"an empty new chat ({exc})")
        for i in range(1, rounds + 1):
            log(f"{site.name}: {last} - reloading the chat and checking "
                f"again ({i}/{rounds})")
            try:
                page.reload()
                page.wait_for_timeout(4000)
            except Exception:  # noqa: BLE001
                pass
            try:
                return retry_when_busy(
                    lambda: ask(page, site, "", (), new_chat=False,
                                resume=True, **{k: v for k, v in kw.items()
                                                if k in ("timeout", "settle",
                                                         "poll", "log",
                                                         "stop", "ready",
                                                         "max_total",
                                                         "continue_max")}),
                    log, should_stop=self.should_stop)
            except WebChatTimeout as again:
                last = again
        raise exc

    def sign_in(self, key: str, wait: float = 600.0) -> bool:
        """Open the site and wait for YOU to sign in (never typed for you)."""
        page = self._page(key)
        site = SITES[key]
        _goto(page, site.url)
        t0 = time.monotonic()
        while time.monotonic() - t0 < wait:
            url = str(page.url or "")
            if (not any(p in url for p in site.login_url_part)
                    and page.evaluate(_BOX_THERE_JS, site.box)):
                return True
            page.wait_for_timeout(1500)
        return False

    def diagnose(self, key: str, out_dir=None) -> str:
        """A full report of what the signed-in page looks like to the
        automation: prompt box(es), the buttons around it, which one 'send'
        would click (without clicking), every model-picker candidate with the
        rows its menu shows (before any model-name filtering) and a
        screenshot of each. Written to <profile root>/diag/<key>/report.txt -
        one command instead of guessing selectors."""
        page, site = self._page(key), SITES[key]
        d = Path(out_dir or (self.profile_root / "diag" / key))
        d.mkdir(parents=True, exist_ok=True)
        out: list[str] = []

        def shot(name):
            try:
                page.screenshot(path=str(d / name))
                out.append(f"  screenshot: {name}")
            except Exception as exc:  # noqa: BLE001
                out.append(f"  screenshot failed: {exc}"[:160])

        t0 = time.monotonic()
        try:
            _goto(page, site.url)
            _wait_for_box(page, site, time.monotonic, limit=40)
            out.append(f"loaded in {time.monotonic() - t0:.1f}s: {page.url}")
        except Exception as exc:  # noqa: BLE001
            out.append(f"LOAD PROBLEM {type(exc).__name__}: {str(exc)[:200]}")
        page.wait_for_timeout(2500)
        out.append(f"title: {page.title()!r}")
        shot("1-start.png")
        out.append("prompt box selector: " + site.box)
        out.append(page.evaluate(_BOX_REPORT_JS, site.box))
        out.append("buttons around the box: " + "; ".join(
            page.evaluate(_COMPOSER_JS, site.box) or ["(none)"]))
        try:
            page.evaluate(_PASTE_JS, {"box": site.box, "text": "hello"})
            page.wait_for_timeout(800)
            dry = site.send_js.replace(
                "b.click(); return true;",
                "return (b.getAttribute('aria-label') || b.innerText || "
                "b.tagName) + ' | ' + b.outerHTML.slice(0, 220);")
            if dry == site.send_js:
                out.append("send button: (built-in site - not dry-run)")
            else:
                got = page.evaluate(dry)
                out.append("send button WOULD BE: " + (
                    str(got) if got else "NONE FOUND after typing 'hello'"))
            shot("2-typed.png")
        except Exception as exc:  # noqa: BLE001
            out.append(f"typing test failed: {type(exc).__name__}: "
                       f"{str(exc)[:160]}")
        try:
            _goto(page, site.url)
            _wait_for_box(page, site, time.monotonic, limit=40)
        except Exception as exc:  # noqa: BLE001 - report it, keep going
            out.append(f"RELOAD PROBLEM {type(exc).__name__}: {str(exc)[:160]}")
        page.wait_for_timeout(2500)
        listing = page.evaluate(_DETECT_MODELS_JS) or {}
        cands = listing.get("cands") or []
        out.append(f"model-picker candidates: {len(cands)}")
        for i, c in enumerate(cands, 1):
            out.append(f"  #{i} shows {c.get('current')!r} path={c.get('open')}")
            out.append(f"     label: {c.get('text')!r}")
            try:
                one = page.evaluate(_DETECT_ONE_JS, {"open": c.get("open"),
                                                     "current": c.get("current")})
            except Exception as exc:  # noqa: BLE001
                out.append(f"     click failed: {str(exc)[:140]}")
                continue
            out.append(f"     rows seen: {one.get('all')} -> accepted as "
                       f"models: {one.get('items')}")
            shot(f"3-candidate-{i}.png")
            if one.get("moved"):
                try:
                    _goto(page, site.url)
                    _wait_for_box(page, site, time.monotonic, limit=40)
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_timeout(1500)
            else:
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
        if not cands:
            out.append("  (no button looked like a model picker - see "
                       "1-start.png for what the page shows)")
            out.append("all visible buttons: " + "; ".join(
                page.evaluate(_ALL_BUTTONS_JS) or []))
        text = "\n".join(out)
        (d / "report.txt").write_text(text, encoding="utf-8")
        return text

    def inspect(self, key: str) -> str:
        """Open the site and report its prompt box, its model-like buttons and
        what happens when each is clicked - so a site whose menu the auto
        -detection misses can be understood from the real, signed-in page."""
        page = self._page(key)
        _goto(page, SITES[key].url)
        _wait_for_box(page, SITES[key], time.monotonic, limit=20)
        info = page.evaluate(_INSPECT_JS)
        out = [f"url: {page.url}",
               f"box: {info.get('box')} | reply el: {info.get('reply')} "
               f"| buttons: {info.get('count')}"]
        for b in info.get("cands") or []:
            out.append(f"  [{b['tag']}] text={b['text']!r} "
                       f"popup={b['popup']!r} role={b['role']!r} "
                       f"testid={b['testid']!r} label={b['aria']!r} "
                       f"in={b['in']!r}")
        if info.get("where"):
            out.append("  where the model-named buttons live:")
            for w in info["where"]:
                out.append(f"    {w['text']!r}: form={w['inForm']} "
                           f"dialog={w['inDialog']} header={w['inHeader']}")
        return "\n".join(out)


_BOX_REPORT_JS = r"""(sel) => [...document.querySelectorAll(sel)].slice(0, 6)
  .map(e => { const r = e.getBoundingClientRect();
    return '  box: <' + e.tagName.toLowerCase() + '> '
      + (e.getAttribute('contenteditable') ? 'contenteditable ' : '')
      + 'visible=' + (r.width > 0 && r.height > 0) + ' '
      + Math.round(r.width) + 'x' + Math.round(r.height)
      + ' placeholder=' + JSON.stringify((e.getAttribute('placeholder')
        || e.getAttribute('data-placeholder') || '').slice(0, 40))
      + ' id=' + (e.id || '') + ' class=' + (e.className || '').toString()
        .slice(0, 50); }).join('\n') || '  (no element matches the box selector)'"""

_ALL_BUTTONS_JS = r"""() => [...document.querySelectorAll(
    'button,[role=button],[role=combobox],[aria-haspopup]')]
  .filter(b => { const r = b.getBoundingClientRect();
                 return r.width > 0 && r.height > 0; }).slice(0, 40)
  .map(b => ((b.innerText || '').trim().split('\n')[0].slice(0, 28)
    || b.getAttribute('aria-label') || b.getAttribute('data-testid')
    || b.tagName) + (b.getAttribute('aria-haspopup')
      ? '[popup=' + b.getAttribute('aria-haspopup') + ']' : ''))"""

_INSPECT_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect();
                     return r.width > 0 && r.height > 0; };
  const btns = [...document.querySelectorAll(
      'button,[role=button],[role=combobox],[aria-haspopup]')].filter(vis);
  const modelish = /model|gpt|claude|gemini|glm|grok|llama|qwen|mistral|flash|\bpro\b|sonnet|opus|haiku|auto|thinking|instant|deepseek|\bo[134]\b/i;
  const label = b => ((b.innerText||'') + ' ' + (b.getAttribute('aria-label')
      || '') + ' ' + (b.getAttribute('data-testid') || '') + ' ' +
      (b.getAttribute('title') || ''));
  const pick = btns.filter(b => {
      const t = (b.innerText||'').trim();
      const h = (b.getAttribute('aria-haspopup')||'').toLowerCase();
      return (h === 'menu' || h === 'listbox' || h === 'true'
              || b.getAttribute('role') === 'combobox'
              || modelish.test(label(b)))
          && t.length && t.length < 60; });
  return {
    box: !!document.querySelector(
        "textarea,[contenteditable=true],[role=textbox]"),
    reply: !!document.querySelector(
        '[data-message-author-role=assistant],[class*=assistant],[class*=markdown]'),
    count: btns.length,
    cands: pick.slice(0, 24).map(b => ({
      tag: b.tagName, text: (b.innerText||'').trim().split('\n')[0].slice(0,36),
      popup: b.getAttribute('aria-haspopup'), role: b.getAttribute('role'),
      testid: (b.getAttribute('data-testid')||'').slice(0,44),
      aria: (b.getAttribute('aria-label')||'').slice(0,44),
      in: (b.closest('nav,header,[class*=sidebar],[class*=left-panel]')
           ? 'chrome' : 'page') })),
    // where each picker lives: the composer (form), the header bar, a dialog
    where: btns.filter(b => modelish.test(label(b))).slice(0, 12).map(b => ({
      text: (b.innerText||'').trim().split('\n')[0].slice(0,36),
      inForm: !!b.closest('form'),
      inDialog: !!b.closest('[role=dialog],[aria-modal=true]'),
      inHeader: !!b.closest('header,[class*=header],[class*=topbar]') }))
  }; }"""


def _main(argv: list[str]) -> int:
    from . import config
    if len(argv) < 2 or argv[0] not in ("login", "ask", "inspect", "diagnose"):
        print("usage: python -m whisperradar.webchat login <site>\n"
              "       python -m whisperradar.webchat ask <site> \"prompt\"\n"
              "       python -m whisperradar.webchat inspect <site>\n"
              "       python -m whisperradar.webchat diagnose <site>")
        return 2
    cfg = config.load_config()
    root = Path(cfg.db_path).parent / "webchat"
    try:                              # sites added in Settings > Web chat LLMs
        from . import db
        _conn = db.connect(cfg.db_path)
        try:
            db.init_db(_conn)
            load_custom_sites(_conn)
        finally:
            _conn.close()
    except Exception:  # noqa: BLE001 - the built-in sites still work
        pass
    if argv[0] == "login":
        if argv[1] not in SITES:
            print("unknown site: " + argv[1])
            return 2
        print("opening " + SITES[argv[1]].name + ": sign in in the window "
              "and CLOSE it when done - the profile keeps your session.")
        try:
            open_sign_in_window(argv[1], root)
        except WebChatError as exc:
            print(exc)
            return 1
        print("done - the sign-in is kept for runs.")
        return 0
    if argv[0] == "diagnose":
        if argv[1] not in SITES:
            print("unknown site: " + argv[1])
            return 2
        with WebChat(root) as chat:
            try:
                print(chat.diagnose(argv[1]))
            except Exception as exc:  # noqa: BLE001
                print(type(exc).__name__ + ": " + str(exc)[:300])
                return 1
        print("\nsaved in " + str(root / "diag" / argv[1]))
        return 0
    if argv[0] == "inspect":
        if argv[1] not in SITES:
            print("unknown site: " + argv[1])
            return 2
        with WebChat(root) as chat:
            try:
                print(chat.inspect(argv[1]))
            except Exception as exc:  # noqa: BLE001
                print(type(exc).__name__ + ": " + str(exc)[:200])
                return 1
        return 0
    with WebChat(root) as chat:
        print(chat.ask(argv[1], " ".join(argv[2:]), log=print))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
