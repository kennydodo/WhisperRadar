"""Publish kit: title, description, chapters, hashtags, tags and a pinned
comment for a finished video, written by a web-chat writer and scored by a
judge (the same loop as the script and shotlist stages), then checked by
rules that need no LLM.

The pure parts (chapters, checks, assembling the description) are tested
without a browser. `kit_job` is the background job the Studio starts.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

KIT_FILE = "publish_kit.json"

TITLE_MAX = 100            # YouTube's hard limit
TITLE_GOOD = 60            # longer titles get cut in search results
DESC_FOLD = 150            # characters shown before "show more"
HASHTAGS = (3, 5)          # the first 3 appear above the title
TAG_CHARS = 500            # all tags together
CHAPTER_MIN = 3
CHAPTER_GAP = 10.0         # seconds between two chapters (YouTube's minimum)
CHAPTER_MIN_LEN = 25.0     # our own floor so chapters are worth clicking
CHAPTER_TITLE_MAX = 60
MIN_SCORE = 8.0


# ---- chapters ------------------------------------------------------------

def fmt_ts(seconds: float) -> str:
    s = int(round(max(0.0, seconds)))
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def build_chapters(cues: list[dict], shots: list[dict],
                   min_len: float = CHAPTER_MIN_LEN, srt_seconds=None
                   ) -> list[dict]:
    """[{start, end, scene, text}] - one chapter per shotlist scene, short
    ones merged into the one before, the first starting at 0:00. `cues` are
    studio.parse_srt_cues rows; `shots` are the shotlist's shots (each with
    `cues` "a-b" and `scene`). Without a shotlist the narration is cut into
    even sections instead."""
    if srt_seconds is None:
        from . import studio
        srt_seconds = studio._srt_seconds
    if not cues:
        return []
    by_index = {c["index"]: c for c in cues}
    total = srt_seconds(cues[-1]["end"])

    def text_between(a: int, b: int) -> str:
        return " ".join(c["text"] for c in cues if a <= c["index"] <= b)

    sections: list[dict] = []
    for s in shots or []:
        if not isinstance(s, dict):
            continue
        rng = _cue_range(s.get("cues"))
        if not rng:
            continue
        scene = str(s.get("scene") or "")
        if sections and sections[-1]["scene"] == scene:
            sections[-1]["last"] = max(sections[-1]["last"], rng[1])
            continue
        sections.append({"scene": scene, "first": rng[0], "last": rng[1]})
    if not sections:                       # no shotlist: even sections
        n = max(1, int(total // 90))
        per = max(1, len(cues) // n)
        for i in range(0, len(cues), per):
            part = cues[i:i + per]
            sections.append({"scene": f"P{len(sections) + 1}",
                             "first": part[0]["index"],
                             "last": part[-1]["index"]})
    out = []
    for sec in sections:
        first = by_index.get(sec["first"]) or cues[0]
        out.append({"scene": sec["scene"], "first": sec["first"],
                    "last": sec["last"],
                    "start": srt_seconds(first["start"])})
    out.sort(key=lambda c: c["start"])
    if out:
        out[0]["start"] = 0.0
    merged = list(out)
    # a chapter shorter than min_len folds into the one before it (the first
    # one into the one after it)
    changed = True
    while changed and len(merged) > 1:
        changed = False
        for i, ch in enumerate(merged):
            nxt = merged[i + 1]["start"] if i + 1 < len(merged) else total
            if nxt - ch["start"] < min_len:
                if i > 0:
                    merged[i - 1]["last"] = max(merged[i - 1]["last"],
                                                ch["last"])
                    del merged[i]
                else:
                    merged[1]["first"] = ch["first"]
                    merged[1]["start"] = 0.0
                    del merged[0]
                changed = True
                break
    for i, ch in enumerate(merged):
        ch["end"] = merged[i + 1]["start"] if i + 1 < len(merged) else total
        ch["text"] = text_between(ch["first"], ch["last"])
    return merged


def _cue_range(text) -> tuple[int, int] | None:
    m = re.match(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*$", str(text or ""))
    if not m:
        return None
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    return (a, b) if b >= a else (a, a)


# ---- the kit --------------------------------------------------------------

def empty_kit() -> dict:
    return {"version": 1, "keyword": "", "titles": [], "title": "",
            "description": "", "chapter_titles": [], "hashtags": [],
            "tags": [], "pinned_comment": "", "thumbnails": [],
            "score": None, "status": "draft", "notes": ""}


def _clean_hashtag(tag: str) -> str:
    tag = re.sub(r"[^\w#]", "", str(tag or "").strip().replace(" ", ""))
    tag = tag.lstrip("#")
    return ("#" + tag) if tag else ""


def parse_kit(raw: dict) -> dict:
    """The writer's JSON turned into a kit (tolerant of shapes)."""
    kit = empty_kit()
    if not isinstance(raw, dict):
        return kit
    kit["keyword"] = str(raw.get("keyword") or raw.get("primary_keyword")
                         or "").strip()
    titles = []
    for t in raw.get("titles") or []:
        if isinstance(t, str):
            titles.append({"text": t.strip(), "why": ""})
        elif isinstance(t, dict) and str(t.get("text") or t.get("title")
                                         or "").strip():
            titles.append({"text": str(t.get("text") or t.get("title")).strip(),
                           "why": str(t.get("why") or "").strip()})
    kit["titles"] = titles
    chosen = str(raw.get("title") or raw.get("best_title") or "").strip()
    kit["title"] = chosen or (titles[0]["text"] if titles else "")
    kit["description"] = str(raw.get("description") or "").strip()
    kit["chapter_titles"] = [str(x).strip()
                             for x in raw.get("chapter_titles") or []]
    tags_h = raw.get("hashtags") or []
    if isinstance(tags_h, str):
        tags_h = re.findall(r"#?\w+", tags_h)
    seen, hs = set(), []
    for h in tags_h:
        h = _clean_hashtag(h)
        if h and h.lower() not in seen:
            seen.add(h.lower())
            hs.append(h)
    kit["hashtags"] = hs
    tg = raw.get("tags") or []
    if isinstance(tg, str):
        tg = [x for x in re.split(r"[,\n]", tg)]
    kit["tags"] = [str(x).strip().lstrip("#") for x in tg if str(x).strip()]
    kit["pinned_comment"] = str(raw.get("pinned_comment") or "").strip()
    kit["notes"] = str(raw.get("notes") or "").strip()
    return kit


def assemble_description(kit: dict, chapters: list[dict]) -> str:
    """The text to paste into YouTube: body, chapters, hashtags."""
    parts = [kit.get("description", "").strip()]
    titles = kit.get("chapter_titles") or []
    if chapters:
        lines = []
        for i, ch in enumerate(chapters):
            name = (titles[i] if i < len(titles) and titles[i]
                    else f"Part {i + 1}")
            lines.append(f"{fmt_ts(ch['start'])} {name}")
        parts.append("Chapters:\n" + "\n".join(lines))
    if kit.get("hashtags"):
        parts.append(" ".join(kit["hashtags"]))
    return "\n\n".join(p for p in parts if p)


def checks(kit: dict, chapters: list[dict]) -> list[dict]:
    """Rule checks that need no LLM: [{ok, level, text}]; `level` is "fix"
    for things YouTube rejects or that clearly hurt, "tip" for advice."""
    out: list[dict] = []

    def add(ok, level, text):
        out.append({"ok": bool(ok), "level": level, "text": text})

    title = (kit.get("title") or "").strip()
    kw = (kit.get("keyword") or "").strip().lower()
    add(bool(title), "fix", "a title is chosen")
    if title:
        add(len(title) <= TITLE_MAX, "fix",
            f"title is at most {TITLE_MAX} characters ({len(title)})")
        add(len(title) <= TITLE_GOOD, "tip",
            f"title fits search results (about {TITLE_GOOD} characters; "
            f"it is {len(title)})")
        letters = [c for c in title if c.isalpha()]
        add(not letters or sum(c.isupper() for c in letters) / len(letters)
            < 0.6, "tip", "title is not mostly capital letters")
    if kw:
        add(kw in title.lower(), "fix", f"title contains the keyword '{kw}'")
        add(title.lower().find(kw) in range(0, 41) if kw in title.lower()
            else False, "tip", "keyword is in the first 40 characters")
        add(kw in (kit.get("description") or "")[:DESC_FOLD].lower(), "tip",
            f"keyword is in the first {DESC_FOLD} characters of the description")
    else:
        add(False, "tip", "a main keyword is set")
    desc = (kit.get("description") or "").strip()
    add(len(desc) >= 200, "tip",
        f"description has substance (200+ characters; {len(desc)})")
    add(len(desc) <= 4500, "fix", "description is under 4500 characters")
    hs = kit.get("hashtags") or []
    add(HASHTAGS[0] <= len(hs) <= HASHTAGS[1], "fix",
        f"{HASHTAGS[0]}-{HASHTAGS[1]} hashtags ({len(hs)})")
    add(all(len(h) <= 30 for h in hs), "fix", "hashtags are short (30 or less)")
    tags = kit.get("tags") or []
    add(sum(len(t) + 1 for t in tags) <= TAG_CHARS, "fix",
        f"tags fit in {TAG_CHARS} characters")
    add(len(tags) >= 5, "tip", f"at least 5 tags ({len(tags)})")
    if chapters:
        names = kit.get("chapter_titles") or []
        add(len(chapters) >= CHAPTER_MIN, "tip",
            f"{CHAPTER_MIN}+ chapters ({len(chapters)})")
        add(len(names) >= len(chapters) and all(n.strip() for n in
                                                names[:len(chapters)]),
            "fix", "every chapter has a title")
        add(all(len(n) <= CHAPTER_TITLE_MAX for n in names), "tip",
            f"chapter titles are at most {CHAPTER_TITLE_MAX} characters")
        gaps = [b["start"] - a["start"]
                for a, b in zip(chapters, chapters[1:])]
        add(all(g >= CHAPTER_GAP for g in gaps), "fix",
            f"chapters are at least {int(CHAPTER_GAP)}s apart")
    add(bool((kit.get("pinned_comment") or "").strip()), "tip",
        "a pinned comment is written")
    return out


def kit_score(items: list[dict]) -> int:
    """0-100: a failed 'fix' costs 12, a failed 'tip' costs 5."""
    pen = sum(12 if (not i["ok"] and i["level"] == "fix") else
              5 if not i["ok"] else 0 for i in items)
    return max(0, 100 - pen)


def local_faults(kit: dict, chapters: list[dict]) -> list[str]:
    return [i["text"] for i in checks(kit, chapters)
            if not i["ok"] and i["level"] == "fix"]


# ---- storage ---------------------------------------------------------------

def kit_path(pdir) -> Path:
    return Path(pdir) / KIT_FILE


def load_kit(pdir) -> dict:
    try:
        data = json.loads(kit_path(pdir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_kit()
    base = empty_kit()
    if isinstance(data, dict):
        base.update(data)
    return base


def save_kit(pdir, kit: dict) -> None:
    kit_path(pdir).write_text(json.dumps(kit, indent=1, ensure_ascii=False),
                              encoding="utf-8")


# ---- context + prompts -------------------------------------------------------

def load_chapters(pdir) -> tuple[list[dict], list[dict]]:
    """(cues, chapters) from the production's subtitles.srt + shotlist.json."""
    from . import studio

    def read(name: str) -> str:
        try:
            return (Path(pdir) / name).read_text(encoding="utf-8")
        except OSError:
            return ""

    cues = studio.parse_srt_cues(read("subtitles.srt"))
    try:
        shots = (json.loads(read("shotlist.json") or "{}").get("shots")
                 or [])
    except ValueError:
        shots = []
    return cues, build_chapters(cues, shots)


def context(cfg, pid: int) -> dict:
    """Everything the writer and judge need about one finished production."""
    from . import db, outliers, studio
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        prod = db.get_production(conn, pid)
        if not prod:
            raise RuntimeError("Unknown production")
        own = (db.get_own_channel(conn, prod["own_channel_id"])
               if prod["own_channel_id"] else None)
        past = []
        for p in db.list_productions(conn):
            if (p["id"] != pid and p["status"] in ("ready", "published")
                    and p["own_channel_id"] == prod["own_channel_id"]):
                past.append(p["title"])
        genre = (own["genre"] if own else None) or prod["genre"] or ""
        refs = []
        try:
            rows = conn.execute(
                "SELECT v.video_id, v.channel_id, c.name AS channel_name,"
                " c.genre AS genre, v.title, v.url, v.published_at,"
                " v.view_count, v.duration, v.status FROM videos v"
                " JOIN channels c ON c.channel_id = v.channel_id"
                " WHERE c.active = 1 AND v.view_count IS NOT NULL").fetchall()
            shown, _n = outliers.filter_sort(
                outliers.build(rows), min_multiplier=5.0, genre=genre,
                limit=12)
            if len(shown) < 6:
                shown, _n = outliers.filter_sort(
                    outliers.build(rows), min_multiplier=5.0, limit=12)
            refs = [(i["title"], i["multiplier"]) for i in shown]
        except Exception:  # noqa: BLE001
            refs = []
    finally:
        conn.close()
    pdir = studio.prod_dir(cfg, pid)

    def read(name: str) -> str:
        try:
            return (pdir / name).read_text(encoding="utf-8")
        except OSError:
            return ""

    cues, chapters = load_chapters(pdir)
    from . import plan as packplan
    return {"plan": packplan.load_plan(pdir), "pid": pid, "pdir": pdir, "title": prod["title"],
            "genre": genre, "channel": own["name"] if own else "",
            "channel_about": (own["description"] if own else "") or "",
            "script": read("script.md").strip(), "cues": cues,
            "chapters": chapters, "refs": refs,
            "past_titles": past[:15],
            "duration": (studio._srt_seconds(cues[-1]["end"]) if cues
                         else 0.0)}


_RULES = f"""YouTube rules and what works:
- Title: at most {TITLE_MAX} characters, and the part that matters (the main keyword and the promise) in the first {TITLE_GOOD}. One clear curiosity gap, never a lie: the video must deliver what the title promises. No ALL CAPS shouting, no more than one emoji.
- Description: the first {DESC_FOLD} characters are what people see before "show more" - they must hook AND contain the main keyword. Then 2-3 short paragraphs saying what the viewer learns, written for people (not a keyword list). No made-up links, no made-up sources.
- Hashtags: {HASHTAGS[0]} to {HASHTAGS[1]}, each one word starting with #, relevant to the video. The first three show above the title.
- Tags: 8-15 search phrases without #, all together under {TAG_CHARS} characters.
- Chapter titles: short (under {CHAPTER_TITLE_MAX} characters), specific, in the order given. Write exactly one per section."""


def _sections_text(ctx: dict, limit: int = 260) -> str:
    lines = []
    for i, ch in enumerate(ctx["chapters"], 1):
        lines.append(f"{i}. [{fmt_ts(ch['start'])}] {ch['text'][:limit]}")
    return "\n".join(lines)


def _refs_text(ctx: dict) -> str:
    if not ctx["refs"]:
        return "(none available)"
    return "\n".join(f"- {t} ({m:.0f}x its channel's norm)"
                     for t, m in ctx["refs"])


def _plan_text(ctx: dict) -> str:
    """The packaging plan made before the script, as a starting point."""
    plan = ctx.get("plan") or {}
    if not plan.get("title"):
        return ""
    return ("PACKAGING PLAN made before the video was written (a starting "
            "point - improve it where the finished video allows, keep what "
            f"works):\n- title: {plan['title']}\n- promise: "
            f"{plan.get('promise', '')}\n")


def writer_prompt(ctx: dict) -> str:
    n = len(ctx["chapters"])
    past = ("\n".join(f"- {t}" for t in ctx["past_titles"])
            or "(none yet)")
    return f"""You are a YouTube packaging expert. Write the publish kit for a finished video.

CHANNEL: {ctx['channel'] or '(unnamed)'} - genre: {ctx['genre']}. {ctx['channel_about']}
WORKING TITLE: {ctx['title']}
VIDEO LENGTH: {fmt_ts(ctx['duration'])}

{_plan_text(ctx)}
THE SCRIPT (what the video actually says - the title and description must never promise more):
{ctx['script'][:12000]}

THE {n} SECTIONS (for the chapter titles, in order, with their start time):
{_sections_text(ctx)}

TITLES THAT BEAT THEIR CHANNEL'S NORM IN THIS NICHE (learn the patterns, do not copy them):
{_refs_text(ctx)}

THIS CHANNEL'S EARLIER TITLES (stay consistent in style, do not repeat them):
{past}

{_RULES}

Reply with ONE JSON object and nothing else:
{{
  "keyword": "the main search phrase this video should rank for (2-4 words)",
  "titles": [{{"text": "...", "why": "one short line: the pattern it uses"}}, ... 8 options of different angles],
  "title": "the best one of those titles, copied exactly",
  "description": "the description body WITHOUT chapters and WITHOUT hashtags",
  "chapter_titles": [exactly {n} strings],
  "hashtags": ["#one", "#two", "#three"],
  "tags": ["search phrase", ...],
  "pinned_comment": "a first comment that starts a conversation (a question the video raises)"
}}"""


def judge_prompt(ctx: dict, kit: dict, faults: list[str]) -> str:
    return f"""You are a strict YouTube growth reviewer. Judge this publish kit for a finished video.

CHANNEL: {ctx['channel'] or '(unnamed)'} - genre: {ctx['genre']}
WHAT THE VIDEO SAYS (start of the script, then the sections):
{ctx['script'][:1500]}
{_sections_text(ctx, 140)}

COMPARABLE TITLES THAT PERFORMED WELL IN THIS NICHE:
{_refs_text(ctx)}

THE KIT:
{json.dumps({k: kit[k] for k in ('keyword', 'title', 'titles', 'description', 'chapter_titles', 'hashtags', 'tags', 'pinned_comment')}, ensure_ascii=False, indent=1)}

RULE CHECKS ALREADY FAILING (code-checked, must be fixed): {faults or 'none'}

{_RULES}

Score from 1 to 10 how well this kit will get the video clicked AND found, and be honest about what the video delivers. Check: does the chosen title create a real curiosity gap and keep its promise; is the keyword early; does the description hook inside {DESC_FOLD} characters; are hashtags and tags relevant and not stuffed; do the chapter titles describe their section; is the pinned comment a real question. Be specific: name the exact text that is weak.

Reply with ONE JSON object and nothing else:
{{"score": 7.5, "pass": false, "faults": ["specific problem 1", "..."], "fixes": ["specific rewrite suggestion 1", "..."]}}
"pass" is true only when the score is {MIN_SCORE:g} or higher and there is nothing left to fix."""


def judge_followup(kit: dict, faults: list[str]) -> str:
    return ("The writer revised the kit after your review. Judge it again "
            "under the SAME rules and reply in exactly the SAME JSON format. "
            "First check that every point you raised is fixed, then check "
            "that nothing else got worse.\n\nRULE CHECKS STILL FAILING: "
            f"{faults or 'none'}\n\n"
            + json.dumps({k: kit[k] for k in (
                'keyword', 'title', 'titles', 'description', 'chapter_titles',
                'hashtags', 'tags', 'pinned_comment')},
                ensure_ascii=False, indent=1))


def writer_feedback(verdict: dict, faults: list[str]) -> str:
    return ("The reviewer found problems with your kit.\n"
            + (f"Rule checks run by code (fix these too): {'; '.join(faults)}\n"
               if faults else "")
            + "The reviewer's verdict, verbatim (JSON):\n"
            + json.dumps(verdict, ensure_ascii=False, indent=1)
            + "\n\nChange ONLY what is named; keep everything else exactly as "
              "it was. Return the COMPLETE kit again as one JSON object in "
              "the same format, and nothing else.")


# ---- the loop ------------------------------------------------------------------

def run_kit(cfg, pid: int, transport, writer: str = "zai",
            judge: str = "deepseek", log: Callable[[str], None] = print,
            should_stop: Callable[[], bool] = lambda: False,
            min_score: float = MIN_SCORE) -> dict:
    from . import studio, webstages as ws
    ctx = context(cfg, pid)
    if not ctx["script"]:
        raise ws.StageFailed("This production has no script yet.")
    chapters = ctx["chapters"]
    log(f"publish kit: {len(chapters)} chapter section(s), "
        f"{len(ctx['refs'])} reference title(s)")
    reply = ws._send(transport, writer, lambda f: writer_prompt(ctx), log,
                     ready=ws._has_json)
    best, judge_open, rnd, empty = None, False, 0, 0
    while True:
        rnd += 1
        kit = parse_kit(studio._parse_json_object(reply))
        if not kit["title"]:
            empty += 1
            if empty >= 3:
                raise ws.StageFailed(f"{writer} did not return a kit")
            log(f"{writer}: no usable kit in the reply - asking again")
            reply = transport.ask(
                writer, "Your reply had no usable JSON kit. Reply with the "
                "complete kit as ONE JSON object in the format given, and "
                "nothing else.", (), new_chat=False, ready=ws._has_json)
            continue
        faults = local_faults(kit, chapters)
        log(f"round {rnd}: title \"{kit['title'][:70]}\" - "
            f"{len(faults)} rule fault(s); asking {judge} to review")
        verdict = {}
        for attempt in (1, 2):
            if judge_open:
                raw = ws._send(transport, judge,
                               lambda f: judge_followup(kit, faults), log,
                               new_chat=False, ready=ws._is_json_verdict)
            else:
                raw = ws._send(transport, judge,
                               lambda f: judge_prompt(ctx, kit, faults), log,
                               ready=ws._is_json_verdict)
            verdict = studio._parse_json_object(raw)
            if verdict:
                judge_open = True
            try:
                score = round(float(verdict.get("score")), 1)
                break
            except (TypeError, ValueError):
                score = None
                log(f"{judge} gave no usable score"
                    + (" - asking once more" if attempt == 1 else ""))
        passed = (score is not None and score >= min_score and not faults
                  and verdict.get("pass") is not False)
        kit["score"] = score
        kit["status"] = "ready" if passed else "draft"
        log(f"round {rnd}: score {score if score is not None else 'n/a'} "
            f"(min {min_score:g}) - " + ("PASSED" if passed
                                         else "not accepted"))
        rank = (not faults, score or 0.0)
        if best is None or rank > best[0]:
            best = (rank, kit)
        if passed:
            save_kit(ctx["pdir"], kit)
            log("publish kit saved")
            return kit
        if should_stop():
            log("stopped by you")
            break
        reply = transport.ask(writer, writer_feedback(
            verdict or {"faults": ["no verdict"]}, faults), (),
            new_chat=False, ready=ws._has_json)
    kit = best[1]
    save_kit(ctx["pdir"], kit)
    log("the best kit so far was saved as a draft")
    return kit


def kit_job(cfg, pid: int, writer: str, judge: str, log,
            should_stop: Callable[[], bool] = lambda: False,
            options: dict | None = None) -> None:
    from . import webstages as ws
    with ws.web_transport(cfg, log, options) as t:
        log(f"settings: {t.options}")
        t.set_stop(should_stop)
        # a NEW chat for each LLM: the kit is its own task, and the chats the
        # script and shotlist used must stay as they are (not saved here)
        run_kit(cfg, pid, t, writer, judge, log=log, should_stop=should_stop)
