"""Read-only storyboard view of a production's shotlist.

One row per shot: the narration it covers, who is in it, the action and the
judge-only note, the prompt length against the Flow limit, and a thumbnail
once the image exists. Built from shotlist.json and the SRT every time (no
stored copy that can go stale, no LLM, nothing written).

Every cell says how reliable it is:
  exact      - copied from the shotlist / SRT
  inferred   - worked out from other fields (refs, prompt words, note words)
  unknown    - the shotlist does not say; nothing is invented
The warnings are heuristics for a person to look at, never proof.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from . import storyboard as sbmod

LIMIT = 2420

# Places a cue can name. If the narration names one and the prompt does not,
# the row is marked "place not in prompt?" - a word match only, a person or the
# image judge decides whether it is really shown.
PLACES = ("clinic", "hospital", "garage", "dealership", "store", "grocery",
          "bank", "office", "kitchen", "apartment", "gym", "school",
          "restaurant", "pharmacy", "warehouse", "airport", "church",
          "park", "gas station", "laundromat", "dentist", "vet")
STATUS_WORDS = ("actual", "hypothetical", "planned", "imagined", "flashback",
                "metaphor", "future")
SIT_RE = re.compile(r"\b(sits?|sitting|seated|sat)\b", re.I)
NOT_A_PERSON = re.compile(r"(suv|sedan|crossover|vehicle|\bcars?\b|dog|bag|folder|sheet|note|"
                          r"statement|phone|container|laptop|tray|envelope)s?\W+(?:\w+\W+){0,2}$", re.I)
NAME_RE = re.compile(r"\bCH_[A-Z0-9_]+\b")


def _cue_range(text) -> tuple[int, int]:
    m = re.match(r"\s*(\d+)\s*(?:-\s*(\d+))?", str(text or ""))
    if not m:
        return (0, 0)
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    return (a, b) if b >= a else (a, a)


def _find_words(words, text: str) -> list[str]:
    low = (text or "").lower()
    return [w for w in words if re.search(r"\b" + re.escape(w) + r"s?\b", low)]


def build_rows(data: dict, cues: dict[int, str], existing_images=()) -> dict:
    """{"rows": [...], "summary": {...}} for the page."""
    style = str(data.get("style") or "").strip()
    st = len(style) + 1 if style else 0
    images = {i.get("file"): i for i in (data.get("images") or [])
              if isinstance(i, dict) and i.get("file")}
    have = set(existing_images or ())
    rows, prev_action = [], ""
    for s in (data.get("shots") or []):
        if not isinstance(s, dict) or not s.get("asset"):
            continue
        asset = str(s["asset"])
        img = images.get(asset) or {}
        prompt = str(img.get("prompt") or "")
        refs = [str(r) for r in (img.get("refs") or [])]
        a, b = _cue_range(s.get("cues"))
        narration = " ".join(cues.get(c, "") for c in range(a, b + 1)).strip()
        action = str(s.get("action") or "").strip()
        note = str(s.get("note") or "").strip()
        flags = []
        if not img:
            flags.append("no image entry for this shot")
        if not action:
            flags.append("no action written")
        if action and prev_action and action.lower() == prev_action.lower():
            flags.append("same action as the previous shot")
        if any(not NOT_A_PERSON.search(prompt[max(0, m.start() - 40):m.start()])
               for m in SIT_RE.finditer(prompt)):
            flags.append("prompt has the character sitting")
        named = _find_words(PLACES, narration)
        missing = [p for p in named if p not in prompt.lower()]
        if missing:
            flags.append("narration names " + ", ".join(missing)
                         + " - not in prompt?")
        in_prompt = set(NAME_RE.findall(prompt))
        total = len(prompt) + st
        if total > LIMIT:
            flags.append(f"style + prompt is {total} (limit {LIMIT})")
        where = _find_words(PLACES, prompt)
        status = _find_words(STATUS_WORDS, note)
        prev_action = action or prev_action
        rows.append({
            "shot": asset, "cues": f"{a}-{b}" if a else "?",
            "narration": narration, "scene": str(s.get("scene") or ""),
            "motion": str(s.get("motion") or ""),
            "action": action, "note": note, "refs": refs,
            "who_unattached": sorted(in_prompt - set(refs)),
            "where": where, "status": status,
            "prompt": prompt, "chars": total,
            "image": asset if asset in have else "",
            "flags": flags,
        })
    summary = {
        "shots": len(rows),
        "no_action": sum(1 for r in rows if not r["action"]),
        "flagged": sum(1 for r in rows if r["flags"]),
        "sitting": sum(1 for r in rows if any("sitting" in f for f in r["flags"])),
        "over_limit": sum(1 for r in rows if r["chars"] > LIMIT),
        "with_image": sum(1 for r in rows if r["image"]),
        "style_chars": len(style),
        "refs": sorted({x for r in rows for x in r["refs"]}),
    }
    return {"rows": rows, "summary": summary}


def _e(x) -> str:
    return html.escape(str(x or ""), quote=True)


CSS = """
body{font:14px/1.4 system-ui,Segoe UI,sans-serif;margin:16px;color:#1b1f24;background:#fafafa}
h1{font-size:20px;margin:0 0 4px}.sum{color:#555;margin-bottom:10px}
.bar{position:sticky;top:0;background:#fafafa;padding:8px 0;border-bottom:1px solid #ddd;z-index:2}
input{padding:5px 8px;border:1px solid #bbb;border-radius:6px;width:280px}
.chip{display:inline-block;padding:1px 7px;margin:1px;border-radius:10px;background:#e8eefc;font-size:12px;cursor:pointer;border:0}
.chip.on{background:#2b5cd6;color:#fff}.chip.w{background:#fde8e8;cursor:default}
table{border-collapse:collapse;width:100%;margin-top:8px;background:#fff}
td,th{border:1px solid #ddd;padding:6px 8px;vertical-align:top;text-align:left}
th{background:#f0f0f0;position:sticky;top:46px}
.id{white-space:nowrap;font-weight:600}.m{color:#666;font-size:12px}
.u{color:#999;font-style:italic}.bad{color:#b42318}.ok{color:#067647}
details summary{cursor:pointer;color:#2b5cd6;font-size:12px}
pre{white-space:pre-wrap;margin:4px 0 0;font:12px/1.4 ui-monospace,Consolas,monospace}
img{max-width:140px;border-radius:4px;display:block}
tr.hide{display:none}
"""

JS = """
(function(){var q=document.getElementById('q'),rows=[].slice.call(document.querySelectorAll('tbody tr')),
chips=[].slice.call(document.querySelectorAll('.chip[data-f]')),on=null,fl=document.getElementById('fl');
function run(){var t=(q.value||'').toLowerCase();rows.forEach(function(r){var ok=(!t||r.textContent.toLowerCase().indexOf(t)>=0)
&&(!on||(r.getAttribute('data-refs')||'').split(' ').indexOf(on)>=0)&&(!fl.checked||r.getAttribute('data-flag')==='1');r.classList.toggle('hide',!ok);});}
q.addEventListener('input',run);fl.addEventListener('change',run);
chips.forEach(function(c){c.addEventListener('click',function(){var v=c.getAttribute('data-f');on=(on===v)?null:v;
chips.forEach(function(x){x.classList.toggle('on',x.getAttribute('data-f')===on);});run();});});})();
"""


def render_html(built: dict, title: str, image_base: str = "") -> str:
    """Standalone page. image_base: URL prefix for thumbnails ('' = none)."""
    sm, rows = built["summary"], built["rows"]
    out = [f"<!doctype html><html><head><meta charset='utf-8'><title>Storyboard - {_e(title)}</title>"
           f"<style>{CSS}</style></head><body>",
           f"<h1>Storyboard - {_e(title)}</h1>",
           f"<div class='sum'>{sm['shots']} shots · {sm['with_image']} images made · "
           f"{sm['no_action']} without action · {sm['sitting']} with sitting in the prompt · "
           f"{sm['over_limit']} over the {LIMIT} limit (style {sm['style_chars']} chars). "
           "Built from shotlist.json and the SRT, read-only. <i>inferred</i> = worked out from other fields; "
           "<i>unknown</i> = the shotlist does not say. Warnings are word matches for a person to check, not proof.</div>",
           "<div class='bar'><input id='q' placeholder='Filter text (narration, action, prompt...)'> "
           "<label><input type='checkbox' id='fl' style='width:auto'> only shots with warnings</label> "]
    for r in sm["refs"]:
        out.append(f"<button type='button' class='chip' data-f='{_e(r)}'>{_e(r)}</button>")
    out.append("</div><table><thead><tr><th>Shot</th><th>Narration</th><th>Who</th>"
               "<th>Action / note</th><th>Prompt</th><th>Image</th></tr></thead><tbody>")
    for r in rows:
        who = "".join(f"<span class='chip' style='cursor:default'>{_e(x)}</span>" for x in r["refs"]) \
            or "<span class='u'>no ref (none attached)</span>"
        if r["who_unattached"]:
            who += "".join(f"<span class='chip w'>named, not attached: {_e(x)}</span>"
                           for x in r["who_unattached"])
        status = ", ".join(r["status"]) if r["status"] else ""
        where = ", ".join(r["where"]) if r["where"] else ""
        meta = (f"<div class='m'>where (inferred from prompt): "
                f"{_e(where) if where else '<span class=u>unknown</span>'}"
                f" · status (from note): {_e(status) if status else '<span class=u>unknown</span>'}</div>")
        act = (_e(r["action"]) if r["action"] else "<span class='u'>no action written</span>")
        note = f"<div class='m'>note: {_e(r['note'])}</div>" if r["note"] else ""
        flags = "".join(f"<div class='bad'>⚠ {_e(f)}</div>" for f in r["flags"])
        cls = "ok" if r["chars"] <= LIMIT else "bad"
        prompt = (f"<details><summary>{r['chars']} chars with style</summary><pre>{_e(r['prompt'])}</pre></details>")
        img = ""
        if r["image"] and image_base:
            img = f"<img loading='lazy' src='{_e(image_base)}{_e(r['image'])}' alt=''>"
        elif r["image"]:
            img = "image made"
        out.append(
            f"<tr data-refs='{_e(' '.join(r['refs']))}' data-flag='{1 if r['flags'] else 0}'>"
            f"<td class='id'>{_e(r['shot'])}<div class='m'>cues {_e(r['cues'])}<br>{_e(r['motion'])}</div></td>"
            f"<td>{_e(r['narration']) or '<span class=u>no narration found for these cues</span>'}</td>"
            f"<td>{who}</td><td>{act}{note}{meta}{flags}</td>"
            f"<td class='{cls}'>{prompt}</td><td>{img}</td></tr>")
    out.append(f"</tbody></table><script>{JS}</script></body></html>")
    return "".join(out)


def load(pdir: Path, parse_srt) -> dict | None:
    """Built rows for a production folder, or None when there is no shotlist."""
    sl = Path(pdir) / "shotlist.json"
    if not sl.is_file():
        return None
    data = json.loads(sl.read_text(encoding="utf-8"))
    cues = {}
    srt = Path(pdir) / "subtitles.srt"
    if srt.is_file():
        cues = {c["index"]: c["text"]
                for c in parse_srt(srt.read_text(encoding="utf-8", errors="replace"))}
    img_dir = Path(pdir) / "images"
    have = {p.name for p in img_dir.iterdir() if p.is_file()} if img_dir.is_dir() else set()
    return build_rows(data, cues, have)


# ---- the editable storyboard page (pass 1 output) --------------------------------------

def _by_row(messages) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for m in messages or []:
        mm = re.match(r"row (\d+): (.*)", str(m))
        if mm:
            out.setdefault(int(mm.group(1)), []).append(mm.group(2))
    return out


RESIZE_JS = """<script>
(function(){var t=document.getElementById('sbtable');if(!t)return;
var cols=t.querySelectorAll('col'),ths=t.querySelectorAll('thead th'),K='wr_sb_cols2';
var DEF=[80,440,210,210,130,400,170];
function load(){try{return JSON.parse(localStorage.getItem(K)||'[]')}catch(e){return []}}
function save(){try{localStorage.setItem(K,JSON.stringify(Array.prototype.map.call(cols,
function(c){return parseInt(c.style.width,10)})))}catch(e){}}
var w=load();for(var i=0;i<cols.length;i++){if(w[i]>=40)cols[i].style.width=w[i]+'px'}
ths.forEach(function(th,i){var h=document.createElement('div');h.className='rz';
h.title='Drag to resize, double-click to reset';th.appendChild(h);
h.addEventListener('mousedown',function(e){e.preventDefault();var x=e.clientX,
w0=parseInt(cols[i].style.width,10)||th.offsetWidth;h.classList.add('on');
function mv(ev){cols[i].style.width=Math.max(40,w0+ev.clientX-x)+'px'}
function up(){document.removeEventListener('mousemove',mv);document.removeEventListener('mouseup',up);
h.classList.remove('on');save()}
document.addEventListener('mousemove',mv);document.addEventListener('mouseup',up)});
h.addEventListener('dblclick',function(){cols[i].style.width=DEF[i]+'px';save()})})})();
</script>"""


def render_edit_html(sb: dict, title: str, pid: int, cues: dict[int, str],
                     statuses, types, stale=(), msg: str = "", error: str = "",
                     provided=(), cap: int = 20) -> str:
    """The storyboard as one editable form. Saving checks it again and clears
    the approval; Approve is a separate button."""
    rows = sb.get("rows") or []
    ids = sbmod.row_ids(rows)
    faults, warns = _by_row(sb.get("faults")), _by_row(sb.get("warnings"))
    faults = {k: [sbmod.relabel(x, ids) for x in v] for k, v in faults.items()}
    warns = {k: [sbmod.relabel(x, ids) for x in v] for k, v in warns.items()}
    general = [sbmod.relabel(f, ids) for f in (sb.get("faults") or []) if not str(f).startswith("row ")]
    approved = bool(sb.get("approved"))
    stale = set(stale)
    head = (f"<!doctype html><html><head><meta charset='utf-8'><title>Storyboard - {_e(title)}"
            f"</title><style>{CSS}textarea,select,input.c{{width:100%;box-sizing:border-box;"
            "font:13px system-ui;padding:4px}textarea{min-height:54px}"
            ".state{padding:2px 10px;border-radius:10px;font-size:12px}"
            ".btns{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0}"
            "button.p{padding:7px 14px;border:0;border-radius:6px;background:#2b5cd6;color:#fff;cursor:pointer}"
            "button.s{padding:7px 14px;border:1px solid #bbb;border-radius:6px;background:#fff;cursor:pointer}"
            ".note{padding:8px 12px;border-radius:6px;margin:6px 0}"
            "#sbwrap{overflow-x:auto}#sbtable{table-layout:fixed;width:auto;min-width:100%}"
            "#sbtable th{position:relative;top:auto;overflow:hidden}"
            ".rz{position:absolute;right:0;top:0;bottom:0;width:7px;cursor:col-resize}"
            ".rz:hover,.rz.on{background:#2b5cd6}"
            "tr.rf>td{background:#fff1f0}tr.rw>td{background:#fffaeb}tr.rp>td{background:#fff4e5}"
            "tr.rf>td.id{box-shadow:inset 4px 0 #d92d20}tr.rw>td.id{box-shadow:inset 4px 0 #f79009}"
            "tr.rp>td.id{box-shadow:inset 4px 0 #7a5af8}"
            ".pf{color:#5925dc}.tags a{display:inline-block;margin:1px 3px 1px 0;padding:1px 8px;"
            "border-radius:10px;font-size:12px;text-decoration:none;color:#fff}"
            ".tags a.f{background:#d92d20}.tags a.w{background:#f79009}.tags a.p{background:#7a5af8}"
            "table.only tbody tr:not(.rf):not(.rw):not(.rp){display:none}"
            f"</style></head><body><h1>Storyboard - {_e(title)}</h1>")
    state = ("<span class='state' style='background:#d1fadf;color:#067647'>approved</span>"
             if approved else
             "<span class='state' style='background:#fef0c7;color:#93370d'>draft - not approved</span>")
    pf_by_id, pf_general = sbmod.prompt_faults_by_id(sb.get("prompt_faults"))
    f_ids = {ids[n - 1] for n in faults}
    w_ids = {ids[n - 1] for n in warns} - f_ids
    p_ids = {i for i in pf_by_id if i in set(ids)}
    out = [head, f"<div class='sum'>{len(rows)} rows · {state} · "
           f"{len(sb.get('faults') or [])} fault(s) · {len(sb.get('warnings') or [])} warning(s). "
           + ("<b>Structural checks passed; narration alignment still needs review.</b> "
              if not (sb.get("faults") or []) else "")
           + "Edit any cell, then Save (checks run again and the approval is cleared). "
           "Approve only when every row says what you want to see; prompts are written from "
           "the approved rows. <a href='/studio/%d'>Back to the production</a></div>" % pid]
    if msg:
        out.append(f"<div class='note' style='background:#d1fadf'>{_e(msg)}</div>")
    if error:
        out.append(f"<div class='note' style='background:#fee4e2'>{_e(error)}</div>")
    for g in general:
        out.append(f"<div class='note' style='background:#fee4e2'>{_e(g)}</div>")
    def tags(label, cls, group):
        if not group:
            return ""
        links = "".join(f"<a class='{cls}' href='#{i}'>{i}</a>" for i in ids if i in group)
        return f"<div><b>{label} ({len(group)}):</b> {links}</div>"
    summary = (tags("Faults in the storyboard", "f", f_ids)
               + tags("Warnings", "w", w_ids)
               + tags("Problems from the last prompt run", "p", p_ids))
    if summary or pf_general:
        out.append("<div class='note tags' style='background:#fff;border:1px solid #ddd'>"
                   + summary
                   + "".join(f"<div class='pf'>Last prompt run: {_e(g)}</div>" for g in pf_general)
                   + "<label style='display:block;margin-top:4px'><input type='checkbox' "
                     "onchange=\"document.getElementById('sbtable').classList.toggle('only',"
                     "this.checked)\"> Show only rows with a fault, warning or prompt problem"
                     "</label></div>")
    if stale:
        out.append("<div class='note' style='background:#fef0c7'>"
                   f"{len(stale)} shot(s) changed after their prompt was written: "
                   f"{_e(', '.join(sorted(stale)[:12]))}{'...' if len(stale) > 12 else ''}. "
                   "Run 'Write prompts from the approved storyboard' again.</div>")
    out.append(f"<form method='post' action='/studio/{pid}/storyboard/save'>"
               "<div class='btns'><button class='s' type='submit'>Save and check</button>"
               f"<button class='p' type='submit' formaction='/studio/{pid}/storyboard/approve'>"
               "Approve storyboard</button></div>"
               "<div id='sbwrap'><table id='sbtable'><colgroup>"
               "<col style='width:80px'><col style='width:440px'><col style='width:210px'>"
               "<col style='width:210px'><col style='width:170px'><col style='width:130px'><col style='width:400px'>"
               "<col style='width:170px'></colgroup>"
               "<thead><tr><th>Shot</th><th>Narration (cues)</th><th>Who</th><th>Where</th>"
               "<th>Objects</th><th>Status</th><th>Action</th><th>Scene / type / motion</th></tr></thead><tbody>")
    for n, r in enumerate(rows, 1):
        a, _, z = (r.get("cues") or "").partition("-")
        try:
            lo, hi = int(a), int(z or a)
        except ValueError:
            lo = hi = 0
        narr = " ".join(cues.get(c, "") for c in range(lo, hi + 1)) if lo else ""
        flags = "".join(f"<div class='bad'>fault: {_e(f)}</div>" for f in faults.get(n, []))
        flags += "".join(f"<div class='m'>⚠ {_e(w)}</div>" for w in warns.get(n, []))
        if r.get("asset") in stale:
            flags += "<div class='bad'>changed after its prompt was written</div>"
        flags += "".join(f"<div class='pf'>prompt: {_e(x)}</div>" for x in pf_by_id.get(ids[n - 1], []))
        cls = ("rf" if n in faults else "rw" if n in warns else "rp" if ids[n - 1] in p_ids else "")
        opts = "".join(f"<option value='{s}'{' selected' if r.get('status') == s else ''}>{s}</option>"
                       for s in ("",) + tuple(statuses))
        tsel = "".join(f"<option{' selected' if r.get('type') == t else ''}>{t}</option>" for t in types)
        out.append(
            f"<tr id='{ids[n - 1]}' class='{cls}'><td class='id'>{_e(ids[n - 1])}<div class='m'>{_e(r.get('cues'))}</div></td>"
            f"<td>{_e(narr) or '<span class=u>cues unknown</span>'}"
            f"<details><summary>starts at</summary><textarea name='starts_with_{n}'>{_e(r.get('starts_with'))}</textarea></details></td>"
            f"<td><input class='c' name='who_{n}' value='{_e(', '.join(r.get('who') or []))}'></td>"
            f"<td><input class='c' name='where_{n}' value='{_e(r.get('where'))}'></td>"
            f"<td><input class='c' name='objects_{n}' value='{_e(', '.join(r.get('objects') or []))}'></td>"
            f"<td><select name='status_{n}'>{opts}</select></td>"
            f"<td><textarea name='action_{n}'>{_e(r.get('action'))}</textarea>{flags}</td>"
            f"<td style='white-space:nowrap'><input class='c' style='width:34px;padding:4px 2px;text-align:center' name='scene_{n}' value='{_e(r.get('scene'))}'> "
            f"<select name='type_{n}' style='width:62px;padding:4px 0'>{tsel}</select> "
            f"<input class='c' style='width:38px;padding:4px 2px;text-align:center' name='motion_{n}' value='{_e(r.get('motion'))}'></td></tr>")
    chars = sbmod.sync_characters(rows, sb.get("characters"))
    locs = sbmod.sync_locations(rows, sb.get("locations"))
    objs = sbmod.sync_objects(rows, sb.get("objects"))
    obj_n: dict = {}
    for r in rows:
        for o in r.get("objects") or []:
            obj_n[o] = obj_n.get(o, 0) + 1
    who_n: dict = {}
    where_n: dict = {}
    for r in rows:
        for w in r.get("who") or []:
            who_n[w] = who_n.get(w, 0) + 1
        w = (r.get("where") or "").strip()
        if w:
            where_n[w] = where_n.get(w, 0) + 1
    out.append("</tbody></table></div>")
    out.append(RESIZE_JS)
    out.append("<h2>Characters and their references</h2><div class='sum'>Each shot gets exactly the "
               "references of the people in its Who column. List one or more reference names "
               "separated by commas (CH_JOE, CH_JOE_OLDER). Family members and age versions get "
               "their own names. Leave empty for a person who needs no reference.</div>"
               "<table><thead><tr><th>Who</th><th>Shots</th><th>Reference(s)</th></tr></thead><tbody>")
    for i, (w, refs) in enumerate(chars.items(), 1):
        out.append(f"<tr><td>{_e(w)}<input type='hidden' name='char_name_{i}' value='{_e(w)}'></td>"
                   f"<td>{who_n.get(w, 0)}</td>"
                   f"<td><input class='c' style='width:360px' name='char_refs_{i}' "
                   f"value='{_e(', '.join(refs))}'></td></tr>")
    out.append("</tbody></table>")
    out.append("<h2>Places and their background references</h2><div class='sum'>A place with a "
               "BG_ name is generated once, as the empty location, and attached to every shot set "
               f"there. Places shown in {sbmod.MIN_PLACE_ROWS} or more shots are suggested. Give two "
               "spellings of one place the same name to merge them; keep different places (Sam and "
               "Beth's kitchen, Joe's bungalow kitchen) under different names. Empty = no "
               "background reference.</div>"
               "<table><thead><tr><th>Where</th><th>Shots</th><th>Background reference</th></tr></thead><tbody>")
    for i, (w, bg) in enumerate(sorted(locs.items(), key=lambda kv: (-where_n.get(kv[0], 0), kv[0])), 1):
        out.append(f"<tr><td>{_e(w)}<input type='hidden' name='loc_where_{i}' value='{_e(w)}'></td>"
                   f"<td>{where_n.get(w, 0)}</td>"
                   f"<td><input class='c' style='width:360px' name='loc_id_{i}' value='{_e(bg)}'></td></tr>")
    out.append("</tbody></table>")
    out.append("<h2>Objects and their references</h2><div class='sum'>An object listed in "
               f"{sbmod.MIN_OBJECT_ROWS} or more shots gets an OBJ_ reference so it looks the same "
               "every time, and that reference is attached to each shot that lists it. Edit the "
               "Objects column above to add or fix objects. Give two spellings of one object the "
               "same name to merge them. Empty = no reference.</div>"
               "<table><thead><tr><th>Object</th><th>Shots</th><th>Reference</th></tr></thead><tbody>")
    for i, (o, ob) in enumerate(sorted(objs.items(), key=lambda kv: (-obj_n.get(kv[0], 0), kv[0])), 1):
        out.append(f"<tr><td>{_e(o)}<input type='hidden' name='obj_name_{i}' value='{_e(o)}'></td>"
                   f"<td>{obj_n.get(o, 0)}</td>"
                   f"<td><input class='c' style='width:360px' name='obj_id_{i}' value='{_e(ob)}'></td></tr>")
    names = {x for v in chars.values() for x in v} | {v for v in locs.values() if v} \
        | {v for v in objs.values() if v}
    new = sorted(names - set(provided))
    over = len(new) - cap
    style = "background:#fee4e2" if over > 0 else "background:#d1fadf"
    verdict = (f"<b>{len(new)} new references, the cap is {cap}: remove {over}.</b> Merge places "
               "(same BG_ name) or clear references you do not need."
               if over > 0 else f"<b>{len(new)} new references, the cap is {cap}.</b>")
    out.append(f"</tbody></table><div class='note' style='{style}'>{verdict} References that "
               f"already have a file ({_e(', '.join(sorted(names & set(provided))) or 'none')}) "
               "do not count.</div>"
               "<div class='btns'><button class='s' type='submit'>Save and check"
               "</button></div></form></body></html>")
    return "".join(out)
