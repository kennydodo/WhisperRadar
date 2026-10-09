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
MIN_TITLES = 5
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
    return {"keyword": "", "titles": [], "title": "", "promise": "",
            "hook": "", "thumbnail": {"layout": "character", "text": "",
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


def parse_plan(raw) -> dict:
    """Normalise the writer's JSON. Missing parts stay empty."""
    plan = empty_plan()
    if not isinstance(raw, dict):
        return plan
    plan["keyword"] = _s(raw.get("keyword"))
    titles = []
    for t in raw.get("titles") or []:
        text = _s(t.get("text") if isinstance(t, dict) else t)
        if text and text.lower() not in [x["text"].lower() for x in titles]:
            titles.append({"text": text,
                           "why": _s(t.get("why")) if isinstance(t, dict)
                           else ""})
    plan["titles"] = titles[:12]
    plan["title"] = _s(raw.get("title")) or (titles[0]["text"] if titles
                                             else "")
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


def originality_faults(plan: dict, source_title: str = "",
                       transcript: str = "") -> list[str]:
    """The title and hook must be OUR OWN, built on the keyword - not the
    source's title or opening reworded. Messages never quote the source
    (they are forwarded to the writer)."""
    f = []
    title = plan.get("title") or ""
    if source_title and title:
        norm = " ".join(_words(source_title))
        if " ".join(_words(title)) == norm or title_similarity(
                title, source_title, plan.get("keyword", "")) > TITLE_SIM_MAX:
            f.append("the title copies the source video's title: keep the "
                     "keyword but write a new title with different words "
                     "and a new angle")
        if any(" ".join(_words(t["text"])) == norm for t in plan["titles"]):
            f.append("one of the title options is the source video's own "
                     "title: replace it with an original option")
    if transcript and plan.get("hook") and shared_run(plan["hook"],
                                                      transcript):
        f.append(f"the hook reuses {HOOK_RUN}+ words in a row from the "
                 "source: write a different opening of equal strength")
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
    if len(title) > TITLE_MAX:
        f.append(f"title is {len(title)} characters; YouTube allows "
                 f"{TITLE_MAX}")
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
    if not plan["hook"]:
        f.append("no opening hook")
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
    ctx["transcript"] = (transcript or "").strip()[:5000]
    return ctx


_RULES = f"""Rules for the plan:
- Title: at most {TITLE_MAX} characters, with the main keyword and the promise inside the first {TITLE_GOOD}. One clear curiosity gap that the video can truly deliver. No ALL CAPS shouting, at most one emoji. The source video proved its TOPIC: take its main keyword and the story it tells, then write OUR OWN title around that keyword - as strong as the source's, in different words and from a fresh angle. Never use the source title, and no option may be a reworded copy of it. Never promise anything the source's facts cannot support.
- The promise is what the viewer will KNOW or FEEL after watching, in one or two plain sentences. The script is written to it.
- The hook is how the first 15 seconds start (a question, a surprising fact, a scene) - one or two sentences. It must be as gripping as the source's opening but NOT the same hook: a different first image, question or fact, never its wording.
- The thumbnail idea is one line plus at most {THUMB_WORDS} catchy words that ADD to the title (never repeat it), and a layout: "character_host" (the character and the human host together), "character" (the character alone) or "host" (the host alone)."""


def _source_text(ctx: dict) -> str:
    s = ctx.get("source")
    if not s:
        return "(none - the topic came from the working title only)"
    mult = (f", {s['multiplier']:.1f}x its channel's norm"
            if s.get("multiplier") else "")
    views = f"{s['views']:,} views" if s.get("views") else "views unknown"
    return (f"\"{s['title']}\" ({s.get('channel_name') or 'unknown channel'}"
            f", {views}{mult})")


def writer_prompt(ctx: dict) -> str:
    from .packaging import _refs_text
    past = "\n".join(f"- {t}" for t in ctx["past_titles"]) or "(none yet)"
    return f"""You are a YouTube packaging strategist. Before this video is written, design how it will be sold: the title, the promise, the opening hook and the thumbnail idea.

CHANNEL: {ctx['channel'] or '(unnamed)'} - genre: {ctx['genre']}. {ctx['channel_about']}
WORKING TITLE: {ctx['title']}
SOURCE VIDEO (what it is based on - it already proved the topic works): {_source_text(ctx)}
START OF THE SOURCE'S TRANSCRIPT (for the facts and angle only):
{ctx['transcript'] or '(not available)'}

TITLES THAT BEAT THEIR CHANNEL'S NORM IN THIS NICHE (learn the patterns, do not copy):
{_refs_text(ctx)}

THIS CHANNEL'S EARLIER TITLES (stay consistent, do not repeat):
{past}

{ctx.get('learned') or ''}
{_RULES}

Reply with ONE JSON object and nothing else:
{{"keyword": "the main search phrase (2-4 words)",
 "titles": [{{"text": "...", "why": "pattern used"}}, ... {MIN_TITLES + 3} options: all original titles built on the keyword, from different angles - none is the source title],
 "title": "the best one, copied exactly from the options",
 "promise": "...", "hook": "...",
 "thumbnail": {{"layout": "character_host|character|host", "text": "2-4 words", "idea": "one line"}}}}"""


def _plan_json(plan: dict) -> str:
    return json.dumps({k: plan[k] for k in ("keyword", "titles", "title",
                                            "promise", "hook", "thumbnail")},
                      ensure_ascii=False, indent=1)


def judge_prompt(ctx: dict, plan: dict, faults: list[str],
                 min_score: float = MIN_SCORE) -> str:
    from .packaging import _refs_text
    return f"""You are a strict YouTube growth reviewer. Judge this packaging plan BEFORE the video is written.

CHANNEL: {ctx['channel'] or '(unnamed)'} - genre: {ctx['genre']}
SOURCE VIDEO: {_source_text(ctx)}
THE SOURCE'S FACTS (start of its transcript): {ctx['transcript'][:1500] or '(not available)'}
COMPARABLE TITLES THAT PERFORMED WELL IN THIS NICHE:
{_refs_text(ctx)}

THE PLAN:
{_plan_json(plan)}

RULE CHECKS ALREADY FAILING (code-checked): {faults or 'none'}

{_RULES}

Originality is part of the job: FAIL the plan (pass false, score below the bar) if the title, any option or the hook is the source's own or a reworded copy of it. The same topic and keyword are right; the same wording or hook is not. In "faults" and "fixes" NEVER quote or restate the source's title, hook or wording - say only that it is too close and what kind of change is needed; the strategist has to invent it. Score 1-10 how likely this packaging is to get the video clicked and found AND be deliverable from the source's facts. Check: a real curiosity gap that is not clickbait, the keyword early, a promise the source material can keep, a hook that starts fast, a thumbnail idea that adds to the title. Name the exact text that is weak.

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
    reply, _w = ws._send_in(transport, writer, lambda f: writer_prompt(ctx),
                            log, writer in kept, ready=ws._has_json)
    judge_cont = judge in kept
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
        plan["title"] = _s(title)
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
