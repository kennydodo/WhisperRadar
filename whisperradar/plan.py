"""Packaging plan, made BEFORE the script: the title (SEO), the one-line
promise the video must keep, the opening hook and a thumbnail idea. A web-chat
writer proposes it from the source video and the niche's best performers, a
judge scores it, code checks the YouTube rules. The script stage then writes
to that promise, and the publish kit / thumbnails later start from it.

`plan_block` / `with_plan` are what the script prompts append.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

PLAN_FILE = "packaging_plan.json"
TITLE_MAX = 100
TITLE_GOOD = 60
PICKS_PER_SEGMENT = 2    # the best titles kept from EACH segment (at least)
MIN_TITLES = 12          # 6 segments x 2
TITLES_KEEP = 24         # cap: up to 3 per segment of 8
SEGMENTS = 8             # the script is divided into this many segments
PER_SEGMENT = 5          # titles drafted for each segment
DRAFT_TITLES = SEGMENTS * PER_SEGMENT
WHY_MAX = 160            # a reason is one short line, not an essay
OPENING_WORDS = 3        # titles sharing these first words ...
OPENING_MAX = 2          # ... may appear at most this many times
MIN_DRAFT_SEGMENTS = 6   # segments the writer must have drafted
TRANSCRIPT_MAX = 24000   # characters of the original script the writer sees
MIN_SCORE = 8.0   # fallback when no setting is reachable (tests, direct calls)


def pass_mark(cfg) -> float:
    """The configured packaging pass mark (Settings > Packaging). Falls back
    to MIN_SCORE if it cannot be read."""
    try:
        from . import db, settings
        conn = db.connect(cfg.db_path)
        try:
            return float(settings.load(conn)["plan_min_rating"])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - never fail a stage over the bar
        return MIN_SCORE
THUMB_WORDS = 4
LAYOUTS = ("character_host", "character", "host")


def empty_plan() -> dict:
    return {"keyword": "", "formula": "", "segments": [], "titles": [],
            "title": "",
            "promise": "", "hook": "", "thumbnail": {"layout": "character", "text": "",
                                      "idea": ""},
            "status": "none", "score": None, "applied": False}


def plan_path(pdir) -> Path:
    return Path(pdir) / PLAN_FILE


def load_plan(pdir) -> dict:
    try:
        data = json.loads(plan_path(pdir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_plan()
    base = empty_plan()
    if isinstance(data, dict):
        base.update(data)
    if not isinstance(base.get("thumbnail"), dict):
        base["thumbnail"] = empty_plan()["thumbnail"]
    return base


def save_plan(pdir, plan: dict) -> None:
    plan_path(pdir).write_text(json.dumps(plan, indent=1, ensure_ascii=False),
                               encoding="utf-8")


def _s(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm_title(text) -> str:
    return re.sub(r"[^\w]+", " ", str(text or "").lower()).strip()


def candidate_title(titles: list, title: str) -> str:
    """The exact candidate string that `title` matches (ignoring case and
    punctuation), or ""."""
    want = _norm_title(title)
    if not want:
        return ""
    for t in titles or []:
        text = t.get("text") if isinstance(t, dict) else t
        if _norm_title(text) == want:
            return str(text)
    return ""


def parse_segments(raw) -> list[dict]:
    """[{name, titles: [str]}] - the script divided into segments, with the
    titles drafted for each. Junk is dropped."""
    out = []
    for seg in raw or []:
        if not isinstance(seg, dict):
            continue
        name = _s(seg.get("name") or seg.get("segment"))[:60]
        titles = []
        for t in seg.get("titles") or []:
            text = _s(t.get("text") if isinstance(t, dict) else t)
            if text and text.lower() not in [x.lower() for x in titles]:
                titles.append(text)
        if name and titles:
            out.append({"name": name, "titles": titles[:PER_SEGMENT + 2]})
    return out[:SEGMENTS + 2]


def parse_plan(raw) -> dict:
    """Normalise the writer's JSON. Missing parts stay empty."""
    plan = empty_plan()
    if not isinstance(raw, dict):
        return plan
    plan["keyword"] = _s(raw.get("keyword"))
    plan["formula"] = _s(raw.get("formula"))[:300]
    plan["segments"] = parse_segments(raw.get("segments"))
    titles = []
    for t in raw.get("titles") or []:
        text = _s(t.get("text") if isinstance(t, dict) else t)
        if text and text.lower() not in [x["text"].lower() for x in titles]:
            isd = isinstance(t, dict)
            titles.append({"text": text,
                           "segment": (_s(t.get("segment")
                                          or t.get("perspective"))[:60]
                                       if isd else ""),
                           "why": (_s(t.get("why"))[:WHY_MAX] if isd else "")})
    plan["titles"] = titles[:TITLES_KEEP]
    title = _s(raw.get("title"))
    # a title that is a candidate in all but punctuation/case becomes the
    # candidate's exact string; anything else stays as written (and is a
    # fault, see local_faults)
    cand = candidate_title(titles, title)
    plan["title"] = cand or title or (titles[0]["text"] if titles else "")
    plan["promise"] = _s(raw.get("promise"))
    plan["hook"] = _s(raw.get("hook"))
    th = raw.get("thumbnail") if isinstance(raw.get("thumbnail"), dict) else {}
    layout = _s(th.get("layout")).lower().replace(" ", "_")
    words = _s(th.get("text")).split(" ")
    plan["thumbnail"] = {
        "layout": layout if layout in LAYOUTS else "character",
        "text": " ".join(w for w in words if w)[:28].strip(),
        "idea": _s(th.get("idea"))}
    return plan


_STOP = set("a an the and or of to in on at for with is are was were be "
            "how why what who this that it its you your i my we our from by "
            "as not no vs".split())
TITLE_SIM_MAX = 0.5    # share of the title's new words also in the source's
HOOK_RUN = 4           # words in a row the hook may share with the source


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def title_similarity(title: str, source_title: str, keyword: str = "") -> float:
    """How much of `title` (beyond the main keyword and filler words) is
    just the source title's own wording: 0 = own words, 1 = a copy."""
    kw = set(_words(keyword))
    mine = [w for w in _words(title) if w not in _STOP and w not in kw]
    theirs = set(_words(source_title))
    if not mine:
        return 0.0
    return sum(w in theirs for w in mine) / len(mine)


def shared_run(a: str, b: str, n: int = HOOK_RUN) -> str:
    """The first n-word run that appears in both texts, or ''."""
    wa, wb = _words(a), _words(b)
    grams = {" ".join(wb[i:i + n]) for i in range(len(wb) - n + 1)}
    for i in range(len(wa) - n + 1):
        g = " ".join(wa[i:i + n])
        if g in grams:
            return g
    return ""


_NUM_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty"
    .split())}
_NUM_WORDS.update({"thirty": 30, "forty": 40, "fifty": 50, "hundred": 100})


def list_numbers(title: str) -> set[int]:
    """Numbers a title promises ("10 reasons", "Seven signs"); years skipped."""
    out = set()
    for w in re.findall(r"\d[\d,]*|[a-zA-Z]+", title or ""):
        if w[0].isdigit():
            try:
                n = int(w.replace(",", ""))
            except ValueError:
                continue
            if n < 1000:
                out.add(n)
        elif w.lower() in _NUM_WORDS:
            out.add(_NUM_WORDS[w.lower()])
    return out


def originality_faults(plan: dict, source_title: str = "",
                       transcript: str = "") -> list[str]:
    """We are replicating the source video, so titles should be SIMILAR to
    it. The one thing rejected is the source's own title, word for word -
    that is a duplicate, not a replica."""
    f = []
    title = plan.get("title") or ""
    if source_title and title:
        norm = " ".join(_words(source_title))
        if " ".join(_words(title)) == norm:
            f.append("the chosen title is identical to the source video's "
                     "title: keep its pattern and keywords but change the "
                     "wording or the angle a little")
        elif any(" ".join(_words(t["text"])) == norm for t in plan["titles"]):
            f.append("one of the title options is identical to the source "
                     "video's title: replace it with a close variation")
    return f


_DANGLING = set("the a an of to and or with for in on at by your my this "
                "that is are was were from as but if than".split())
_SEP = re.compile(r"[:|]|\s[-\u2013\u2014]\s")
# "Your kids don't want it, and downsizing is the smart move": two clauses
_CLAUSES = re.compile(r",\s+(and|but|so|because|then|while|yet)\b|;", re.I)
SHORT_MAX = 70


def awkward_title(title: str, keyword: str = "") -> str:
    """Why a title reads badly aloud ("" when it reads fine): the checks a
    person makes by ear - it must sound like something a human would say,
    not a keyword pile."""
    t = _s(title)
    words = _words(t)
    if len(words) < 3:
        return "too short to say anything"
    if len(words) > 16:
        return "too long to read in one breath"
    if words[-1] in _DANGLING:
        return f"ends on a dangling \"{words[-1]}\""
    if _SEP.search(t) or _CLAUSES.search(t) or "(" in t or "[" in t:
        return ("is split into two parts (colon, dash, brackets or ', and "
                "...') - a title is ONE phrase, not a headline plus a "
                "second half")
    if len(t) > SHORT_MAX:
        return (f"is {len(t)} characters - a title is short enough to read "
                f"at a glance (about {SHORT_MAX} at most)")
    shout = [w for w in re.findall(r"[A-Za-z']{3,}", t) if w.isupper()]
    if len(shout) > 1:
        return "shouts in ALL CAPS"
    seen: dict[str, int] = {}
    for w in words:
        if len(w) > 3 and w not in _STOP and not w.isdigit():
            seen[w] = seen.get(w, 0) + 1
    # natural parallel phrasing ("everyone avoids ... everyone needs") is
    # fine; a repeated KEYWORD word (or any word 3 times) is stuffing
    kw = set(_words(keyword))
    again = [w for w, n in seen.items()
             if n > 1 and (not kw or w in kw or n > 2)]
    if again:
        return f"repeats \"{again[0]}\" (keyword stuffing)"
    return ""


def variety_faults(plan: dict) -> list[str]:
    """Ten titles must not be one title reworded: no more than OPENING_MAX
    of them may start with the same first words, and the options must come
    from several segments of the script."""
    f = []
    opens: dict[str, int] = {}
    for t in plan["titles"]:
        key = " ".join(_words(t["text"])[:OPENING_WORDS])
        if key:
            opens[key] = opens.get(key, 0) + 1
    worst = max(opens.items(), key=lambda kv: kv[1], default=("", 0))
    if worst[1] > OPENING_MAX:
        f.append(f"{worst[1]} title options start with \"{worst[0]}\": no "
                 f"more than {OPENING_MAX} may open with the same words - "
                 "vary the opening and the sentence shape (the keyword may "
                 "sit anywhere in the first "
                 f"{TITLE_GOOD} characters)")
    # plans made before the segment step have none: leave them alone
    if plan["titles"] and plan.get("formula"):
        if len(plan.get("segments") or []) < MIN_DRAFT_SEGMENTS:
            f.append(f"divide the whole script into segments and draft "
                     f"titles for each: got {len(plan.get('segments') or [])}"
                     f", need at least {MIN_DRAFT_SEGMENTS} (aim for "
                     f"{SEGMENTS})")
        picks: dict[str, int] = {}
        for t in plan["titles"]:
            k = _norm_title(t.get("segment", ""))
            picks[k] = picks.get(k, 0) + 1
        thin = [g["name"] for g in plan.get("segments") or []
                if picks.get(_norm_title(g["name"]), 0) < PICKS_PER_SEGMENT]
        if thin:
            f.append(f"every segment needs its top {PICKS_PER_SEGMENT} titles "
                     f"in the options (copy the segment name exactly); "
                     f"short: {', '.join(thin[:4])}")
    return f


def phrasing_faults(plan: dict) -> list[str]:
    """The chosen title must read naturally; so must most of the options."""
    f = []
    why = awkward_title(plan.get("title") or "", plan.get("keyword", ""))
    if why:
        f.append(f"the chosen title reads awkwardly - it {why}: rewrite it "
                 "as one natural sentence a person would say, keeping the "
                 "keyword as a real phrase")
    bad = [(t["text"], awkward_title(t["text"], plan.get("keyword", "")))
           for t in plan["titles"]]
    bad = [(a, b) for a, b in bad if b]
    if len(bad) >= 3:
        f.append(f"{len(bad)} title options read awkwardly (for example "
                 f"\"{bad[0][0]}\" - it {bad[0][1]}): every option must "
                 "sound like natural spoken English")
    return f


def _origin(ctx: dict) -> tuple[str, str]:
    """(source video title, start of its transcript) for the originality
    checks; the working title is NOT used - it may be our own earlier plan."""
    return ((ctx.get("source") or {}).get("title") or "", ctx.get("transcript") or "")


def faults_for(cfg, pid: int, plan: dict) -> list[str]:
    """local_faults including the originality checks against the source."""
    try:
        origin = _origin(context(cfg, pid))
    except Exception:  # noqa: BLE001 - a page must still render
        origin = ("", "")
    return local_faults(plan, *origin)


def local_faults(plan: dict, source_title: str = "",
                 transcript: str = "") -> list[str]:
    f = []
    title = plan["title"]
    if not title:
        return ["no title"]
    f += originality_faults(plan, source_title, transcript)
    f += phrasing_faults(plan)
    f += variety_faults(plan)
    if len(title) > TITLE_MAX:
        f.append(f"title is {len(title)} characters; YouTube allows "
                 f"{TITLE_MAX}")
    if plan["titles"] and not candidate_title(plan["titles"], title):
        f.append("the chosen title is not one of the title options - pick "
                 "one of the options exactly")
    kw = plan["keyword"].lower()
    if not kw:
        f.append("no main keyword")
    elif kw not in title.lower():
        f.append(f"the keyword \"{plan['keyword']}\" is not in the title")
    elif title.lower().find(kw) + len(kw) > TITLE_GOOD:
        f.append(f"the keyword ends after the first {TITLE_GOOD} characters "
                 "- search results cut the title there")
    if len(plan["titles"]) < MIN_TITLES:
        f.append(f"give at least {MIN_TITLES} title options "
                 f"(got {len(plan['titles'])})")
    if not plan["promise"]:
        f.append("no promise: say what the viewer will get")
    th = plan["thumbnail"]
    n = len(th["text"].split())
    if not th["text"] or n > THUMB_WORDS:
        f.append(f"thumbnail text must be 1-{THUMB_WORDS} words")
    elif th["text"].lower() == title.lower():
        f.append("thumbnail text repeats the title")
    return f


# ---- what the script stage sees -------------------------------------------

def plan_block(plan: dict) -> str:
    """The text appended to the script prompts, or "" when there is no
    usable plan. ONLY the title and the main keyword go to the production:
    the promise, hook and thumbnail idea are for the thumbnails and the
    publish kit made after the video is merged, never for the script."""
    if not plan.get("title"):
        return ""
    lines = ["VIDEO TITLE AND KEYWORD (what the video is about - the "
             "script must deliver what this title says, never less or more):",
             f"- Title: {plan['title']}"]
    if plan.get("keyword"):
        lines.append(f"- Main keyword: {plan['keyword']} - use it naturally "
                     "where it fits, never forced")
    lines.append("- RULE: the script is spoken narration only. It must NEVER "
                 "mention or allude to the thumbnail, the cover, the title, "
                 "the packaging, or this note. A judge must list every such "
                 "mention as a failure and the writer must remove it.")
    return "\n".join(lines)


_META_RE = re.compile(
    r"\b(thumbnails?|thumb|cover image|video title|packaging plan)\b", re.I)


def meta_mentions(script: str) -> list[str]:
    """Words in a spoken script that talk about the video's packaging (the
    thumbnail, the title...): they must not be in narration."""
    return sorted({m.group(1).lower() for m in _META_RE.finditer(script or "")})


def with_plan(cfg, prod, direction: str) -> str:
    """The script stage's extra direction plus the packaging plan, when the
    production has one."""
    try:
        from . import studio
        block = plan_block(load_plan(studio.prod_dir(cfg, prod["id"])))
    except Exception:  # noqa: BLE001 - a plan problem never blocks a script
        block = ""
    direction = direction or ""
    if not block:
        return direction
    return (direction + "\n\n" if direction.strip() else "") + block


# ---- context + prompts -------------------------------------------------------

def context(cfg, pid: int) -> dict:
    from . import autorun, db, outliers, packaging, studio
    ctx = packaging.context(cfg, pid)
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    src = None
    try:
        prod = db.get_production(conn, pid)
        vid = prod["source_video_id"]
        if vid:
            rows = conn.execute(
                "SELECT v.video_id, v.channel_id, c.name AS channel_name,"
                " c.genre AS genre, v.title, v.url, v.published_at,"
                " v.view_count, v.duration, v.status FROM videos v"
                " JOIN channels c ON c.channel_id = v.channel_id"
                " WHERE v.view_count IS NOT NULL").fetchall()
            src = next((i for i in outliers.build(rows)
                        if i["video_id"] == vid), None)
            if src is None:
                row = db.get_video(conn, vid)
                if row:
                    src = {"title": row["title"], "channel_name": "",
                           "views": row["view_count"], "multiplier": None}
    except Exception:  # noqa: BLE001
        src = None
    finally:
        conn.close()
    try:
        transcript = autorun._source_transcript_text(cfg, pid)
    except Exception:  # noqa: BLE001
        transcript = ""
    ctx["source"] = src
    # the plan writer replicates the source: it gets the original script
    ctx["transcript"] = (transcript or "").strip()[:TRANSCRIPT_MAX]
    ctx["brief"] = ""
    try:
        have = (ctx["pdir"] / autorun.RESEARCH_NOTES_FILE).read_text(
            encoding="utf-8").strip()
        if have and autorun._notes_cache_valid(ctx["pdir"], have, transcript):
            ctx["brief"] = have
    except OSError:
        pass
    return ctx


_RULES = f"""Rules for the plan:
- Titles are the most important part. The goal is to REPLICATE the source video. Work in this order:
  0. READ the WHOLE original script first.
  1. FORMULA: read the script and the niche's winning titles and decide the title formula that fits this video - its skeleton in one line, with the variable parts in brackets, e.g. "[Number] [things] [that stopped working] in [year]", "[Blunt truth] ([Why] [topic] is the [superlative] [thing] you're [avoiding])" or "I [did the thing] for [time]" (examples only: derive the formula from THIS video, whatever its subject). If the winning titles share a number or a year, use it the same way. Also decide the MAIN KEYWORD yourself: the 2-4 word phrase people would search for to find this video, taken from the script.
  2. SEGMENTS: divide the video into {SEGMENTS} segments by CORE VALUE. A segment is one reason a viewer would care - the core benefit, emotion or curiosity the video serves (for example: fear of missing out, relief, belonging, status, hope, surprise, curiosity about how it works, a mistake to avoid, the "insider" feeling, nostalgia, a challenge to a belief). They are different ways to sell the video's ONE big promise. They are NOT a list of the script's facts, figures, costs, claims, steps, examples or conclusions, and they work for any subject (finance, cooking, animals, history, gaming, health...). Read the script to find what the video is really worth to the viewer, then name each segment in 2-4 words by that value.
  3. DRAFT: for EACH segment write about {PER_SEGMENT} titles that keep the formula and the topic, changing only the core value they sell. A title may use the topic and the keyword; it must NOT use the script's specifics (its figures, item names, mechanisms, costs, claims or conclusions).
  4. SELECT: from the drafts of EACH segment take its top {PICKS_PER_SEGMENT} titles (more only if a segment has extra strong ones) - every segment must be represented by at least {PICKS_PER_SEGMENT}. Then rank ALL the picks best first; rank 1 is the chosen title.
  Titles are SIMILAR to what the original video would be called - same topic, same promise, the patterns of the niche's winning titles.
  Every title is ONE short phrase (about 45-65 characters), never split into two parts: no colon, no dash, no brackets, no "X, and Y" second half. One clause, a clear topic and a stake, and nothing more. The source and the best titles in this niche show the shape; copy their structure, not their words.
  A title is a TEASER, never the story. It names the topic and the value or stake for the viewer, and it leaves the answer, the reason, the mechanism, the numbers and the fix for the video. If the title could stand as a one-line summary of the video, it gives too much away: rewrite it. Never put the conclusion, the cause, a specific claim or the solution in the title. Bad: "[Problem], and [Topic] Is the [Verdict]" (problem + answer + verdict in one title). Good: a title that raises the question and makes the viewer need the video, e.g. "Why Nobody Wants Your [Thing] Anymore" or "[N] Things About [Topic] That Quietly Change Everything".
- Every title is at most {TITLE_MAX} characters, with the main keyword and the promise inside the first {TITLE_GOOD} - the keyword may sit anywhere in that stretch; it does NOT have to be the first words. Vary how titles open: no more than {OPENING_MAX} of them may start with the same first {OPENING_WORDS} words. It reads like a real sentence a person would say out loud: natural grammar, ordinary everyday words, one clear idea. The keyword is part of the sentence (a phrase people actually search for, kept in its natural word order) - it is NEVER stuffed in, repeated, split up or bolted on with colons, dashes or brackets. No ALL CAPS shouting, at most one emoji, at most one ":" or "-".
  Bad (never write like this): a keyword pile ("[Topic] Hacks Secrets Revealed Now"), the keyword repeated or bolted on with a colon or brackets, or unnatural word order ("Habits Home Japanese That Work"). Test every title by reading it aloud: if it sounds like a machine or a keyword list, rewrite it.
  Only promise what the original script's topic can deliver. If a title promises a number of points ("7 reasons"), it may match or stay close to the source's number.
- List the picks in rank order (rank 1 first), each with its "segment" (the segment name copied exactly) and a "why" of ONE short line (under {WHY_MAX} characters).
- The promise is what the viewer will KNOW or FEEL after watching, in one or two plain sentences. The script is written to it.
- The thumbnail idea is one line plus at most {THUMB_WORDS} catchy words that ADD to the title (never repeat it), and a layout: "character_host" (the character and the human host together), "character" (the character alone) or "host" (the host alone). The thumbnails are designed in their own stage, so keep this short."""


def _source_text(ctx: dict) -> str:
    s = ctx.get("source")
    if not s:
        return "(none - the topic came from the working title only)"
    mult = (f", {s['multiplier']:.1f}x its channel's norm"
            if s.get("multiplier") else "")
    views = f"{s['views']:,} views" if s.get("views") else "views unknown"
    return (f"\"{s['title']}\" ({s.get('channel_name') or 'unknown channel'}"
            f", {views}{mult})")


def _refs_without_source(ctx: dict) -> str:
    """The niche's winning titles for the prompts, minus the source video's
    own title (the writer decides its titles and keywords from the script,
    never from the original title)."""
    from .packaging import _refs_text
    src = _norm_title((ctx.get("source") or {}).get("title") or "")
    refs = [(t, m) for t, m in ctx.get("refs") or []
            if not src or _norm_title(t) != src]
    return _refs_text({**ctx, "refs": refs})


def _original_script(ctx: dict) -> str:
    t = (ctx.get("transcript") or "").strip()
    return t or "(not available - rely on the source title and the brief)"


def writer_prompt(ctx: dict) -> str:
    past = "\n".join(f"- {t}" for t in ctx["past_titles"]) or "(none yet)"
    return f"""You are a YouTube packaging strategist. I want to replicate this video. Provide titles similar to this one, but the content perspective may change a little. Titles matter most. You decide the titles and the main keyword yourself, from the script.

CHANNEL: {ctx['channel'] or '(unnamed)'} - genre: {ctx['genre']}. {ctx['channel_about']}

THE ORIGINAL SCRIPT (the video we are replicating - it already proved the topic works; read it all: this is what the titles must be true to):
{_original_script(ctx)}

TITLES THAT BEAT THEIR CHANNEL'S NORM IN THIS NICHE (learn the patterns and phrasing):
{_refs_without_source(ctx)}

THIS CHANNEL'S EARLIER TITLES (stay consistent in style, do not repeat):
{past}

{ctx.get('learned') or ''}
{_RULES}

Reply with ONE JSON object and nothing else:
{{"keyword": "the main search phrase (2-4 words) that YOU chose from the script",
 "formula": "the source title's formula in one line",
 "segments": [{{"name": "2-5 words", "titles": ["...", "..."]}}, ... {SEGMENTS} segments of the script, about {PER_SEGMENT} drafted titles each],
 "titles": [{{"text": "...", "segment": "its segment's exact name", "why": "one short line"}}, ... the top {PICKS_PER_SEGMENT} (or more) of EACH segment, ranked, rank 1 first, all similar to the source, none identical to it],
 "title": "rank 1, copied exactly from the options",
 "promise": "...",
 "thumbnail": {{"layout": "character_host|character|host", "text": "2-4 words", "idea": "one line"}}}}"""


def _plan_json(plan: dict) -> str:
    return json.dumps({k: plan[k] for k in ("keyword", "formula", "segments",
                                            "titles",
                                            "title",
                                            "promise", "thumbnail")},
                      ensure_ascii=False, indent=1)


def judge_prompt(ctx: dict, plan: dict, faults: list[str],
                 min_score: float = MIN_SCORE) -> str:
    return f"""You are a strict YouTube growth reviewer. Judge this packaging plan BEFORE the video is written.

CHANNEL: {ctx['channel'] or '(unnamed)'} - genre: {ctx['genre']}
THE ORIGINAL SCRIPT (excerpt): {_original_script(ctx)[:6000]}
COMPARABLE TITLES THAT PERFORMED WELL IN THIS NICHE:
{_refs_without_source(ctx)}

THE PLAN:
{_plan_json(plan)}

RULE CHECKS ALREADY FAILING (code-checked): {faults or 'none'}

{_RULES}

Similarity is part of the job: the titles must replicate the video the script comes from - the same topic, promise and the title patterns of the niche, with only a slight change of content perspective. FAIL the plan if the titles drift to a different topic or promise. Quote the weak title and give a closer rewrite in "fixes". Spoilers are part of the job: FAIL the plan if the chosen title or several options give away the story - the problem AND its answer, the verdict, the reason or the fix - or are split into two parts (colon, dash, brackets, \", and ...\"), or run past about 65 characters. A good title is one short phrase that makes the viewer need the video. Variety is part of the job: FAIL the plan if the options are one title reworded - many opening with the same words, one sentence shape, or some segment has fewer than {PICKS_PER_SEGMENT} titles among the options, or the segments are not core values (they list the script's facts, costs, figures or claims instead) or a title uses the script's specifics instead of the topic and the value it sells, or the script was not really divided into segments - or if the formula does not match the source's. Phrasing is part of the job: FAIL the plan if the chosen title or several options read awkwardly aloud - unnatural word order, keyword pile, repeated words, odd grammar, or a keyword forced in. Titles need the keyword AND a natural sentence; one without the other fails. Quote the awkward title and show the natural rewrite in "fixes". Score 1-10 how likely this packaging is to get the video clicked and found AND be deliverable from the brief's topic. Check: the keyword is a phrase people really search for, taken from the script, the titles are built on it and sound like natural spoken English; a real curiosity gap that is not clickbait, the keyword early, a promise the original script can keep, exactly {TITLES_KEEP} titles, ranked best first (check the ranking is sensible: the strongest, most natural title is first). Name the exact text that is weak.

Reply with ONE JSON object and nothing else:
{{"score": 7.5, "pass": false, "faults": ["specific problem"], "fixes": ["specific rewrite"]}}
"pass" is true only when the score is {min_score:g} or higher and nothing is left to fix."""


def judge_followup(plan: dict, faults: list[str]) -> str:
    return ("The strategist revised the plan after your review. Judge it "
            "again under the SAME rules and reply in exactly the SAME JSON "
            "format. First check each point you raised, then that nothing "
            f"else got worse.\n\nRULE CHECKS STILL FAILING: {faults or 'none'}"
            "\n\n" + _plan_json(plan))


def writer_feedback(verdict: dict, faults: list[str]) -> str:
    return ("The reviewer found problems with your plan.\n"
            + (f"Rule checks run by code (fix these too): "
               f"{'; '.join(faults)}\n" if faults else "")
            + "The reviewer's verdict, verbatim (JSON):\n"
            + json.dumps(verdict, ensure_ascii=False, indent=1)
            + "\n\nChange ONLY what is named; keep the rest as it was. "
              "Return the COMPLETE plan again as one JSON object in the "
              "same format, and nothing else.")


def run_plan(cfg, pid: int, transport, writer: str = "zai",
             judge: str = "deepseek", log: Callable[[str], None] = print,
             should_stop: Callable[[], bool] = lambda: False,
             min_score: float = MIN_SCORE, same_chats: bool = True) -> dict:
    from . import studio, webstages as ws
    ctx = context(cfg, pid)
    log(f"packaging plan for \"{ctx['title'][:70]}\" "
        f"({len(ctx['refs'])} reference title(s))")
    # one production = one chat per LLM: continue the chats the production
    # already has (a chat that cannot be reopened is replaced by a new one)
    kept = (ws.adopt_chats(cfg, pid, transport, {writer, judge}, log)
            if same_chats else set())
    judge_cont = judge in kept
    if not ctx["brief"] and ctx["transcript"]:
        ctx["brief"], built = ws.ensure_brief(cfg, pid, transport, judge, log,
                                              judge_cont)
        judge_cont = judge_cont or built
    reply, _w = ws._send_in(transport, writer, lambda f: writer_prompt(ctx),
                            log, writer in kept, ready=ws._has_json)
    best, judge_open, rnd, empty = None, False, 0, 0
    while True:
        rnd += 1
        plan = parse_plan(studio._parse_json_object(reply))
        if not plan["title"]:
            empty += 1
            if empty >= 3:
                raise ws.StageFailed(f"{writer} did not return a plan")
            log(f"{writer}: no usable plan - asking again (it said: "
                f"{' '.join(str(reply).split())[:200]!r})")
            reply = transport.ask(
                writer, "Your reply had no usable JSON plan. Reply with the "
                "complete plan as ONE JSON object in the format given, and "
                "nothing else.", (), new_chat=False, ready=ws._has_json)
            continue
        faults = local_faults(plan, *_origin(ctx))
        log(f"round {rnd}: \"{plan['title'][:70]}\" - {len(faults)} rule "
            f"fault(s); asking {judge} to review")
        verdict, score = {}, None
        for attempt in (1, 2):
            if judge_open:
                raw = ws._send(transport, judge,
                               lambda f: judge_followup(plan, faults), log,
                               new_chat=False, ready=ws._is_json_verdict)
            else:
                raw, judge_cont = ws._send_in(
                    transport, judge,
                    lambda f: judge_prompt(ctx, plan, faults, min_score),
                    log, judge_cont, ready=ws._is_json_verdict)
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
        plan["score"] = score
        plan["status"] = "ready" if passed else "draft"
        log(f"round {rnd}: score {score if score is not None else 'n/a'} "
            f"(min {min_score:g}) - " + ("PASSED" if passed
                                         else "not accepted"))
        rank = (not faults, score or 0.0)
        if best is None or rank > best[0]:
            best = (rank, plan)
        if passed or should_stop():
            if not passed:
                log("stopped by you")
            break
        reply = transport.ask(writer, writer_feedback(
            verdict or {"faults": ["no verdict"]}, faults), (),
            new_chat=False, ready=ws._has_json)
    plan = best[1]
    old = load_plan(ctx["pdir"])
    plan["applied"] = bool(old.get("applied")) and old.get("title") == plan["title"]
    save_plan(ctx["pdir"], plan)
    log("packaging plan saved" + ("" if plan["status"] == "ready"
                                  else " as a draft"))
    return plan


def plan_job(cfg, pid: int, writer: str, judge: str, log,
             should_stop: Callable[[], bool] = lambda: False,
             options: dict | None = None) -> None:
    from . import webstages as ws
    mark = pass_mark(cfg)
    with ws.web_transport(cfg, log, options) as t:
        log(f"settings: {t.options}")
        t.set_stop(should_stop)
        try:
            run_plan(cfg, pid, t, writer, judge, log=log,
                     should_stop=should_stop, min_score=mark)
        finally:
            ws.save_chats(cfg, pid, t)


def run_plan_api(cfg, pid: int, writer: str | None, judge: str | None,
                 log: Callable[[str], None] = print,
                 should_stop: Callable[[], bool] = lambda: False,
                 max_rounds: int = 3, min_score: float = MIN_SCORE) -> dict:
    """The same plan loop as run_plan, but through the configured API LLM
    providers (what auto-run uses - no browser). Stateless calls, so each
    revision is sent with the previous attempt and the verdict. With no judge
    available a plan with no rule faults is accepted (score stays empty)."""
    from . import studio
    ctx = context(cfg, pid)
    log(f"packaging plan for \"{ctx['title'][:70]}\" "
        f"({len(ctx['refs'])} reference title(s))")
    if not ctx["brief"] and ctx["transcript"]:
        from . import autorun
        full = autorun._source_transcript_text(cfg, pid)
        ctx["brief"] = autorun._research_notes(
            cfg, ctx["pdir"], ctx["title"], ctx["genre"], full,
            judge or writer)
    base = writer_prompt(ctx)
    best, prompt, rnd = None, base, 0
    while rnd < max_rounds:
        rnd += 1
        raw = studio.llm_generate(cfg, prompt, provider=writer,
                                  temperature=0.8)
        plan = parse_plan(studio._parse_json_object(raw))
        if not plan["title"]:
            log(f"round {rnd}: no usable plan in the reply")
            prompt = base + ("\n\nYour last reply had no usable JSON plan. "
                             "Reply with ONE JSON object only.")
            continue
        faults = local_faults(plan, *_origin(ctx))
        verdict, score = {}, None
        if judge:
            try:
                vraw = studio.llm_generate(
                    cfg, judge_prompt(ctx, plan, faults, min_score),
                    provider=judge,
                    temperature=0.2)
                verdict = studio._parse_json_object(vraw)
                score = round(float(verdict.get("score")), 1)
            except (TypeError, ValueError):
                score = None
            except Exception as exc:  # noqa: BLE001 - a judge outage is not fatal
                log(f"judge unavailable ({type(exc).__name__}) - using the "
                    "rule checks only")
                judge = None
        passed = (not faults and (
            (score is not None and score >= min_score
             and verdict.get("pass") is not False)
            or (judge is None)))
        plan["score"] = score
        plan["status"] = "ready" if passed else "draft"
        log(f"round {rnd}: \"{plan['title'][:70]}\" - {len(faults)} rule "
            f"fault(s), score {score if score is not None else 'n/a'} - "
            + ("accepted" if passed else "not accepted"))
        rank = (not faults, score or 0.0)
        if best is None or rank > best[0]:
            best = (rank, plan)
        if passed or should_stop():
            break
        prompt = (base + "\n\nYOUR PREVIOUS ATTEMPT:\n" + _plan_json(plan)
                  + "\n\n" + writer_feedback(
                      verdict or {"faults": faults or ["no verdict"]},
                      faults))
    if best is None:
        raise RuntimeError("the LLM returned no usable packaging plan")
    plan = best[1]
    save_plan(ctx["pdir"], plan)
    log("packaging plan saved" + ("" if plan["status"] == "ready"
                                  else " as a draft"))
    return plan


def apply_plan(cfg, pid: int, title: str | None = None) -> dict:
    """Make the plan the production's: its (or the given) title becomes the
    production title, and the publish kit starts from it."""
    from . import db, studio
    pdir = studio.prod_dir(cfg, pid)
    plan = load_plan(pdir)
    if title:
        plan["title"] = (candidate_title(plan["titles"], _s(title))
                         or _s(title))
    elif plan["titles"] and not candidate_title(plan["titles"],
                                                plan["title"]):
        raise ValueError("The plan's title is not one of its vetted title "
                         "options - pick one before applying")
    if not plan["title"]:
        raise ValueError("The plan has no title")
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        if not plan.get("source_title"):
            plan["source_title"] = db.get_production(conn, pid)["title"]
        db.update_production(conn, pid, title=plan["title"][:TITLE_MAX])
    finally:
        conn.close()
    plan["applied"] = True
    save_plan(pdir, plan)
    return plan
