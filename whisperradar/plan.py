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
PICKS_PER_SEGMENT = 2    # (old plans) the best titles kept from each segment
MIN_TITLES = 10          # options the titler must give
TITLES_KEEP = 20         # cap on the options kept
TITLES_ASK = 14          # options the titler is asked for
SEGMENTS = 8             # (old plans only: segments are no longer asked for)
PER_SEGMENT = 5
ALTERNATES = 2           # titles the judge keeps next to its pick
WHY_MAX = 160            # a reason is one short line, not an essay
OPENING_WORDS = 3        # titles sharing these first words ...
OPENING_MAX = 2          # ... may appear at most this many times
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
    return {"keyword": "", "overview": "", "premise": "", "values": [],
            "formula": "", "segments": [], "titles": [],
            "points": [], "title": "",
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
    plan["overview"] = _s(raw.get("overview"))[:400]
    plan["formula"] = _s(raw.get("formula"))[:300]
    plan["premise"] = _s(raw.get("premise"))[:600]
    vals = raw.get("values")
    plan["values"] = [x for x in (_s(v)[:60] for v in (vals if isinstance(vals, list) else [])) if x][:10]
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
    pts = raw.get("points")
    plan["points"] = [x for x in (_s(p.get("point") if isinstance(p, dict) else p)[:200]
                                  for p in (pts if isinstance(pts, list) else [])) if x]
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
    """Titles must be SIMILAR in meaning and pattern to the source video's, not
    its wording: a title identical to it, or sharing too many of its content
    words (beyond the keyword), is a copy. Messages never quote the source -
    they go back to a writer that must not know it."""
    f = []
    if not source_title:
        return f
    norm = " ".join(_words(source_title))
    kw = plan.get("keyword", "")
    texts = [plan.get("title") or ""] + [t["text"] for t in plan["titles"]]
    copies = {x for x in texts if x and (
        " ".join(_words(x)) == norm
        or title_similarity(x, source_title, kw) > TITLE_SIM_MAX)}
    if copies:
        f.append(f"{len(copies)} title(s) copy too many words of an existing "
                 f"video's title (for example \"{sorted(copies)[0]}\"): keep "
                 "the topic and the promise but say it in clearly different "
                 "words")
    return f


def too_close(title: str, source_title: str, keyword: str = "") -> bool:
    """True when `title` is a copy of the source title (see above)."""
    if not source_title:
        return False
    return (" ".join(_words(title)) == " ".join(_words(source_title))
            or title_similarity(title, source_title, keyword) > TITLE_SIM_MAX)


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
    """The options must not be one title reworded: no more than OPENING_MAX
    of them may start with the same first words."""
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


_LISTICLE = re.compile(
    r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:\w+\s+){0,2}?(ways?|reasons?|costs?|things?|signs?|steps?|tips?|"
    r"mistakes?|secrets?|rules?|truths?|lessons?|habits?|hacks?|ideas?|"
    r"benefits?|problems?|risks?|questions?|myths?|types?|tricks?|errors?|"
    r"facts?|lies|warnings?|dangers?|killers?|strategies|moves?|rules?)\b", re.I)


_NUMWORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen "
    "twenty".split())}


def list_count(title: str) -> int:
    """N when a title is a numbered list ("10 Things ...", "Seven Ways ...");
    0 when it is not."""
    m = _LISTICLE.search(title or "")
    if not m:
        return 0
    w = m.group(1).lower()
    return int(w) if w.isdigit() else _NUMWORDS.get(w, 0)


def allowed_counts(n: int) -> list[int]:
    """The counts a replica of an N-point list may use: about 20% either way
    (10 -> 8, 9, 11, 12) but NEVER the same count as the original."""
    d = max(1, round(n * 0.2))
    return [c for c in range(max(2, n - d), n + d + 1) if c != n]


def scope_faults(plan: dict, source_title: str = "") -> list[str]:
    """A title covers the WHOLE video. "4 Hidden Costs of ..." promises one
    list of parts - one slice of the script. A count is allowed ONLY when the
    source video is itself a list of N points: then the count must be within
    ~20% of N and must differ from N."""
    src = list_count(source_title)
    titles = [t["text"] for t in plan["titles"]]
    if plan.get("title") and plan["title"] not in titles:
        titles.append(plan["title"])
    counted = [(t, list_count(t)) for t in titles if list_count(t)]
    f = []
    if not src:
        if counted:
            f.append(f"{len(counted)} title(s) are a numbered list of parts "
                     f"(for example \"{counted[0][0]}\"): a list of ways, "
                     "reasons or costs is one slice of the script, not the "
                     "whole video - rewrite each as a title for the overall "
                     "subject, with no count")
        return f
    ok = allowed_counts(src)
    same = [t for t, n in counted if n == src]
    off = [t for t, n in counted if n != src and n not in ok]
    if same:
        f.append(f"{len(same)} title(s) use the same count as the original "
                 f"list (for example \"{same[0]}\"): use a different number, "
                 f"one of {', '.join(map(str, ok))}")
    if off:
        f.append(f"{len(off)} title(s) use a count outside the allowed "
                 f"{', '.join(map(str, ok))} (for example \"{off[0]}\")")
    return f


def specific_faults(plan: dict, transcript: str, package_text: str,
                    refs_text: str = "") -> list[str]:
    """Titles must come from the whole story, not from the script's own
    specifics: a figure, or a name that appears in the script but is not in
    the overview package or the niche's titles, means a title was built on
    one detail."""
    if not transcript:
        return []
    allowed = set(_words(package_text)) | set(_words(refs_text))
    names = {m.group(1).lower() for m in re.finditer(
        r"(?<![.!?\n]\s)(?<!^)\b([A-Z][a-z]{3,})\b", transcript)}
    nums = set(re.findall(r"\d[\d,.]*", transcript))
    bad = []
    for t in plan["titles"]:
        words = _words(t["text"])
        hit = [w for w in words if w in names and w not in allowed
               and w not in _STOP]
        figs = [n for n in re.findall(r"\d[\d,.]*", t["text"])
                if n in nums and n not in allowed and list_count(t["text"]) == 0]
        if hit or figs:
            bad.append((t["text"], (hit or figs)[0]))
    if not bad:
        return []
    return [f"{len(bad)} title(s) use a name or figure taken from the script "
            f"(for example \"{bad[0][0]}\" uses \"{bad[0][1]}\"): titles sell "
            "the whole story, never one detail of it"]


def narrow_faults(verdict: dict) -> list[str]:
    """Titles the judge named as covering only one point of the script."""
    n = verdict.get("narrow") if isinstance(verdict, dict) else None
    n = [_s(x) for x in n if _s(x)] if isinstance(n, list) else []
    if not n:
        return []
    return [f"{len(n)} title(s) cover only one point of the script, not the "
            f"whole video (for example \"{n[0]}\"): replace each with a "
            "title for the overall subject"]


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
    f += scope_faults(plan, source_title)
    f += specific_faults(plan, transcript, plan.get("overview", "") + " "
                         + plan.get("premise", "") + " " + plan.get("keyword", "")
                         + " " + " ".join(plan.get("values") or []))
    if len(title) > TITLE_MAX:
        f.append(f"title is {len(title)} characters; YouTube allows "
                 f"{TITLE_MAX}")
    if plan["titles"] and not candidate_title(plan["titles"], title):
        f.append("the chosen title is not one of the title options - pick "
                 "one of the options exactly")
    kw = plan["keyword"].lower()
    if not kw:
        f.append("no main keyword")
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
    r"\b(thumbnails?|thumb[- ]?nails?|cover image|video title|packaging plan)\b",
    re.I)       # not bare "thumb": "rule of thumb" is ordinary narration


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
    ctx["niche"] = None
    try:
        from . import niche_profile
        conn = db.connect(cfg.db_path)
        try:
            rows = conn.execute(
                "SELECT v.video_id, v.channel_id, c.name AS channel_name,"
                " c.genre AS genre, v.title, v.url, v.published_at,"
                " v.view_count, v.duration, v.status FROM videos v"
                " JOIN channels c ON c.channel_id = v.channel_id"
                " WHERE c.active = 1 AND v.view_count IS NOT NULL").fetchall()
            prod2 = db.get_production(conn, pid)
        finally:
            conn.close()
        # the source video's own niche decides the references, not the
        # (possibly "general") genre of the channel the video is made for
        genre = ((src or {}).get("genre") or prod2["genre"]
                 or ctx.get("genre") or "")
        prof = niche_profile.build(
            outliers.build(rows), genre, (src or {}).get("channel_id") or "",
            (src or {}).get("channel_name") or "",
            (src or {}).get("title") or "")
        ctx["niche"] = prof
        if prof["refs"]:
            ctx["refs"] = prof["refs"]
    except Exception:  # noqa: BLE001 - the planner works without a profile
        pass
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
- Titles are the most important part. Every title sells the WHOLE video - the overview - never one point, item, example, cost, number, audience, scene or sub-topic of it. Test: delete any one section of the video and the title must still be completely true. You are not given the script on purpose: write from the overview, the premise and the core values only.
- Use the CORE VALUES as angles: each title sells the SAME whole story through one value (fear of missing out, relief, status, hope, curiosity about how it works, a mistake to avoid, an insider feeling, a challenge to a belief...). Vary the angle from title to title.
- Decide the title formula that fits this niche from its winning titles (its skeleton in one line, e.g. "[Blunt truth] ([Why] [topic] is [what you avoid])" or "I [did the thing] for [time]"; examples only - derive it from the niche's winners). If the winners share a number or a year, use it the same way.
- Every title is ONE short phrase (about 45-65 characters), never split into two parts: no colon, no dash, no brackets, no "X, and Y" second half. It reads like a real sentence a person would say out loud: natural grammar, ordinary everyday words, one clear idea. Never a keyword pile, a repeated word, or unnatural word order. No ALL CAPS shouting, at most one emoji. Test every title by reading it aloud.
- A title is a TEASER, never the story: it names the topic and the value or stake for the viewer and leaves the answer, the reason, the mechanism, the numbers and the fix for the video. Never put the conclusion, the cause, a specific claim or the solution in the title.
- The keyword is the topic you anchor on; it does NOT have to be in every title, and may sit anywhere. Vary how titles open: no more than {OPENING_MAX} of them may start with the same first {OPENING_WORDS} words. Every title is at most {TITLE_MAX} characters.
- Do NOT use a numbered list as a title ("4 Ways to...", "3 Hidden Costs of...") unless the brief says numbers are allowed and gives the counts; then use ONLY those counts.
- Only promise what the video's overview can deliver.
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
    own title (the titler never sees the original title)."""
    from .packaging import _refs_text
    src = _norm_title((ctx.get("source") or {}).get("title") or "")
    refs = [(t, m) for t, m in ctx.get("refs") or []
            if not src or _norm_title(t) != src]
    return _refs_text({**ctx, "refs": refs})


def _niche_text(ctx: dict) -> str:
    from . import niche_profile
    return niche_profile.prompt_block(ctx.get("niche"))


def _original_script(ctx: dict) -> str:
    t = (ctx.get("transcript") or "").strip()
    return t or "(not available - rely on the source title)"


# ---- step 1: the analyst reads the script, never writes titles --------------

def analyst_prompt(ctx: dict) -> str:
    return f"""You analyse a YouTube video for a packaging team. Read the whole script below and describe the WHOLE story. Do NOT write any titles.

CHANNEL: {ctx['channel'] or '(unnamed)'} - genre: {ctx['genre']}

THE SCRIPT:
{_original_script(ctx)}

Reply with ONE JSON object and nothing else:
{{"overview": "ONE sentence: what the WHOLE video is about and promises - its overall subject, not any single part",
 "premise": "two sentences: the kind of situation, the central tension and the payoff for the viewer. No names of people or companies, no figures, no list of the script's items or examples",
 "keyword": "the 2-4 word phrase people would search for to find this video, taken from the script",
 "values": ["5-8 core values the video sells, each 2-4 words (the core benefit, emotion or curiosity it serves: relief, fear of missing out, status, a mistake to avoid, an insider edge...). Not facts, costs, claims or steps of the script"]}}"""


def parse_package(raw) -> dict:
    out = {"overview": "", "premise": "", "keyword": "", "values": []}
    if not isinstance(raw, dict):
        return out
    out["overview"] = _s(raw.get("overview"))[:400]
    out["premise"] = _s(raw.get("premise"))[:600]
    out["keyword"] = _s(raw.get("keyword"))[:80]
    v = raw.get("values")
    out["values"] = [x for x in (_s(a)[:60] for a in (v if isinstance(v, list)
                                                      else [])) if x][:10]
    return out


def package_ok(pkg: dict) -> bool:
    return bool(pkg.get("overview") and pkg.get("keyword") and pkg.get("values"))


def _package_text(pkg: dict) -> str:
    return (f"OVERVIEW OF THE WHOLE VIDEO: {pkg.get('overview', '')}\n"
            f"PREMISE: {pkg.get('premise', '')}\n"
            f"MAIN KEYWORD: {pkg.get('keyword', '')}\n"
            f"CORE VALUES (angles): {'; '.join(pkg.get('values') or [])}")


def _numbers_text(ctx: dict) -> str:
    src = (ctx.get("source") or {}).get("title") or ""
    n = list_count(src)
    if not n:
        return "NUMBERED TITLES: not allowed - no title may be a numbered list."
    return ("NUMBERED TITLES: allowed, because this niche's proven video is a "
            f"list. If a title uses a count, it must be one of "
            f"{', '.join(map(str, allowed_counts(n)))}; titles without a count "
            "are fine too.")


# ---- step 2: the titler never sees the script or the original title ---------

def writer_prompt(ctx: dict) -> str:
    past = "\n".join(f"- {t}" for t in ctx["past_titles"]) or "(none yet)"
    pkg = ctx.get("package") or {}
    return f"""You are a YouTube packaging strategist. A video is about to be made; design how it will be sold. Titles matter most. You do not have the script - work from this description of the whole video.

CHANNEL: {ctx['channel'] or '(unnamed)'} - genre: {ctx['genre']}. {ctx['channel_about']}

{_package_text(pkg)}
{_numbers_text(ctx)}

TITLES THAT BEAT THEIR CHANNEL'S NORM IN THIS NICHE (learn the patterns and phrasing, do not copy):
{_refs_without_source(ctx)}
{_niche_text(ctx)}

THIS CHANNEL'S EARLIER TITLES (stay consistent in style, do not repeat):
{past}

{ctx.get('learned') or ''}
{_RULES}

Reply with ONE JSON object and nothing else:
{{"formula": "the title formula you decided on, in one line",
 "titles": [{{"text": "...", "why": "one short line: the value it sells"}}, ... {TITLES_ASK} different titles, each selling the whole video through a different angle],
 "promise": "...",
 "thumbnail": {{"layout": "character_host|character|host", "text": "2-4 words", "idea": "one line"}}}}"""


def _plan_json(plan: dict) -> str:
    return json.dumps({k: plan[k] for k in ("formula", "titles", "promise",
                                            "thumbnail")},
                      ensure_ascii=False, indent=1)


# ---- step 3: the judge knows the original; it audits and picks --------------

def judge_prompt(ctx: dict, plan: dict, faults: list[str],
                 min_score: float = MIN_SCORE) -> str:
    pkg = ctx.get("package") or {}
    return f"""You are a strict YouTube growth reviewer. Judge this packaging plan BEFORE the video is written. The title writer never saw the script or the original title: it worked only from the description below.

CHANNEL: {ctx['channel'] or '(unnamed)'} - genre: {ctx['genre']}
THE ORIGINAL VIDEO (the one we replicate): {_source_text(ctx)}
THE ORIGINAL SCRIPT (excerpt): {_original_script(ctx)[:6000]}

WHAT THE TITLE WRITER WAS GIVEN:
{_package_text(pkg)}
{_numbers_text(ctx)}

COMPARABLE TITLES THAT PERFORMED WELL IN THIS NICHE:
{_refs_without_source(ctx)}
{_niche_text(ctx)}

THE PLAN:
{_plan_json(plan)}

RULE CHECKS ALREADY FAILING (code-checked): {faults or 'none'}

{_RULES}

You do three things.
1. AUDIT every title, one by one, for scope: "if any single section of the script were deleted, is this title still completely true, and does it describe the WHOLE video?" A title built on ONE point, item, example, cost, audience, relationship, number or list from the script FAILS. Put the exact text of every failing title in "narrow". Also FAIL spoilers (the title gives away the problem AND its answer, the verdict, the reason or the fix), two-part titles (colon, dash, brackets, ", and ..."), titles longer than about 65 characters, titles that read awkwardly aloud (unnatural word order, keyword pile, repeated words), options that are one title reworded, and titles that drift to a different topic or promise than the original video. A numbered title is fine ONLY when the numbers note above allows it.
2. PICK. Choose the option whose meaning, promise and pattern are CLOSEST to the original video's title: the one a viewer of the original would recognise as the same video. Compare meaning and pattern, not shared words. Copy it exactly into "closest", and the next {ALTERNATES} closest into "alternates". NEVER reveal the original title, the pick or the alternates in "faults" or "fixes" - those are sent to the title writer, who must not learn them.
3. SCORE 1-10 how likely this packaging is to get the video clicked and found AND be true to the whole video. In "faults" and "fixes" say what is wrong and what KIND of change is needed; never quote the original title or script and never offer a rewrite that borrows from them. Name the exact option text that is weak.

Reply with ONE JSON object and nothing else:
{{"score": 7.5, "pass": false, "narrow": ["exact title that fails the scope audit"], "closest": "exact option text", "alternates": ["exact option text", "exact option text"], "faults": ["specific problem"], "fixes": ["specific change"]}}
"pass" is true only when the score is {min_score:g} or higher and nothing is left to fix."""


def judge_followup(plan: dict, faults: list[str]) -> str:
    return ("The strategist revised the plan after your review. Judge it "
            "again under the SAME rules and reply in exactly the SAME JSON "
            "format, including a fresh \"closest\" and \"alternates\". First "
            "check each point you raised, then that nothing else got worse."
            f"\n\nRULE CHECKS STILL FAILING: {faults or 'none'}"
            "\n\n" + _plan_json(plan))


def safe_verdict(verdict: dict, plan: dict, source_title: str,
                 transcript: str, package_text: str) -> dict:
    """What may go back to the title writer: no pick, no alternates, and no
    note that carries anything of the original title or script."""
    from . import studio
    v = {k: val for k, val in (verdict or {}).items()
         if k not in ("closest", "alternates", "pick", "narrow")}
    own = _plan_json(plan) + "\n" + package_text
    for k in ("faults", "fixes"):
        items = v.get(k)
        if isinstance(items, list):
            v[k], _gone = studio.scrub_for_writer(
                [str(x) for x in items], own, f"{source_title}\n{transcript}")
    return v


def writer_feedback(verdict: dict, faults: list[str]) -> str:
    return ("The reviewer found problems with your plan.\n"
            + (f"Rule checks run by code (fix these too): "
               f"{'; '.join(faults)}\n" if faults else "")
            + "The reviewer's verdict, verbatim (JSON):\n"
            + json.dumps(verdict, ensure_ascii=False, indent=1)
            + "\n\nChange ONLY what is named; keep the rest as it was. "
              "Return the COMPLETE plan again as one JSON object in the "
              "same format, and nothing else.")


def apply_pick(plan: dict, verdict: dict, source_title: str = "") -> dict:
    """The judge's pick becomes the title (the next ones follow it as the
    alternatives), unless it is too close to the original title in wording.
    The rest keep their order."""
    titles = plan["titles"]
    by = {_norm_title(t["text"]): t for t in titles}

    def pool(names):
        out = []
        for n in names if isinstance(names, list) else [names]:
            t = by.get(_norm_title(n))
            if t and t not in out and not too_close(
                    t["text"], source_title, plan.get("keyword", "")):
                out.append(t)
        return out
    v = verdict or {}
    first = pool(v.get("closest"))
    lead = first + [t for t in pool(v.get("alternates")) if t not in first]
    if first:
        plan["titles"] = lead + [t for t in titles if t not in lead]
        plan["title"] = first[0]["text"]
        plan["picked_by"] = "judge"
    return plan


def _ask_package(ctx: dict, ask) -> dict:
    """Analyst step: `ask(prompt) -> raw reply`; up to 3 tries."""
    from . import studio
    prompt = analyst_prompt(ctx)
    for _ in range(3):
        pkg = parse_package(studio._parse_json_object(ask(prompt)))
        if package_ok(pkg):
            return pkg
        prompt = analyst_prompt(ctx) + ("\n\nYour last reply had no usable "
                                        "JSON. Reply with the one JSON object "
                                        "only.")
    raise RuntimeError("the analyst did not return a usable description of "
                       "the video")


def _adopt_package(plan: dict, pkg: dict) -> None:
    """The analyst's description is the plan's description and keyword."""
    plan["overview"], plan["premise"] = pkg["overview"], pkg["premise"]
    plan["values"], plan["keyword"] = pkg["values"], pkg["keyword"]


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
    if writer == judge:
        log("warning: the writer and the judge are the same chat - the title "
            "writer would see the script. Use two different LLMs.")
    # step 1: the judge's chat (it may see the script) describes the WHOLE
    # video; the title writer gets only that description
    state = {"cont": judge_cont}

    def ask_pkg(prompt: str) -> str:
        raw, state["cont"] = ws._send_in(transport, judge, lambda f: prompt,
                                         log, state["cont"],
                                         ready=ws._has_json)
        return raw
    log(f"{judge}: describing the whole video (overview, premise, keyword, "
        f"core values)")
    ctx["package"] = _ask_package(ctx, ask_pkg)
    judge_cont = state["cont"] or True
    log(f"keyword: {ctx['package']['keyword']}")
    # step 2: the title writer never sees the script or the original title
    reply, _w = ws._send_in(transport, writer, lambda f: writer_prompt(ctx),
                            log, writer in kept, ready=ws._has_json)
    src_title, transcript = _origin(ctx)
    best, judge_open, rnd, empty = None, False, 0, 0
    while True:
        rnd += 1
        plan = parse_plan(studio._parse_json_object(reply))
        _adopt_package(plan, ctx["package"])
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
        faults = faults + [x for x in narrow_faults(verdict) if x not in faults]
        passed = (score is not None and score >= min_score and not faults
                  and verdict.get("pass") is not False)
        plan["score"] = score
        plan["status"] = "ready" if passed else "draft"
        log(f"round {rnd}: score {score if score is not None else 'n/a'} "
            f"(min {min_score:g}) - " + ("PASSED" if passed
                                         else "not accepted"))
        rank = (not faults, score or 0.0)
        if best is None or rank > best[0]:
            best = (rank, plan, verdict)
        if passed or should_stop():
            if not passed:
                log("stopped by you")
            break
        reply = transport.ask(writer, writer_feedback(
            safe_verdict(verdict or {"faults": ["no verdict"]}, plan,
                         src_title, transcript, _package_text(ctx["package"])),
            faults), (), new_chat=False, ready=ws._has_json)
    plan = apply_pick(best[1], best[2], src_title)
    if plan.get("picked_by"):
        log(f"{judge} picked the closest title to the original: "
            f"\"{plan['title'][:70]}\"")
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
    ctx["package"] = _ask_package(ctx, lambda pr: studio.llm_generate(
        cfg, pr, provider=judge or writer, temperature=0.3))
    log(f"keyword: {ctx['package']['keyword']}")
    base = writer_prompt(ctx)
    src_title, transcript = _origin(ctx)
    best, prompt, rnd = None, base, 0
    while rnd < max_rounds:
        rnd += 1
        raw = studio.llm_generate(cfg, prompt, provider=writer,
                                  temperature=0.8)
        plan = parse_plan(studio._parse_json_object(raw))
        _adopt_package(plan, ctx["package"])
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
        faults = faults + [x for x in narrow_faults(verdict) if x not in faults]
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
            best = (rank, plan, verdict)
        if passed or should_stop():
            break
        prompt = (base + "\n\nYOUR PREVIOUS ATTEMPT:\n" + _plan_json(plan)
                  + "\n\n" + writer_feedback(
                      safe_verdict(verdict or {"faults": faults
                                               or ["no verdict"]}, plan,
                                   src_title, transcript,
                                   _package_text(ctx["package"])), faults))
    if best is None:
        raise RuntimeError("the LLM returned no usable packaging plan")
    plan = apply_pick(best[1], best[2], src_title)
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
