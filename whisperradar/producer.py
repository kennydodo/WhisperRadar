"""Auto Run producer: create productions from monitored channels, unattended.

Design (agreed with the user, 2026-09-22):

- Monitored source channels map to an own channel by GENRE: an own channel
  draws its ideas from the transcribed videos of monitored channels with the
  same genre.
- Candidates: transcribed source videos not already used by a production,
  published within `candidate_window_days`.
- topic_pick: 'newest' takes the most recent candidate; 'llm' asks the
  producer LLM to choose the best one AND write the production title. The
  returned id must be one of the candidates - otherwise it falls back to
  newest.
- Caps: `per_day` (global and per channel) counts productions CREATED today.
- After creating + seeding, the production runs the normal pipeline and stops
  before review, exactly like the Studio "Run till finish" button.

Nothing here spends image credits by itself - it calls autorun.run_pipeline,
which stops before review and pauses when manual input is missing.
"""

import json
import re
from datetime import datetime

from . import db, settings, studio


def _in_window(vals: dict, now: datetime | None = None) -> bool:
    """True when `now` (local) is inside the configured run window. A start
    later than the end means an overnight window."""
    now = now or datetime.now()
    start = vals.get("run_window_start") or "00:00"
    end = vals.get("run_window_end") or "23:59"
    if start == end:
        return True
    hm = now.strftime("%H:%M")
    if start < end:
        return start <= hm <= end
    return hm >= start or hm <= end


def _created_today(conn, own_channel_id=None) -> int:
    sql = ("SELECT COUNT(*) FROM productions"
           " WHERE date(created_at) = date('now')")
    params: list = []
    if own_channel_id is not None:
        sql += " AND own_channel_id = ?"
        params.append(own_channel_id)
    return conn.execute(sql, params).fetchone()[0]


def candidates(conn, own_channel, window_days: int = 90,
               limit: int = 40):
    """Transcribed source videos of the own channel's genre that no
    production has used yet, newest first."""
    genre = own_channel["genre"] or "general"
    sql = (
        "SELECT v.*, c.name AS channel_name, c.genre AS channel_genre"
        " FROM videos v JOIN channels c ON c.channel_id = v.channel_id"
        " WHERE v.status = 'transcribed' AND c.genre = ?"
        "   AND v.video_id NOT IN (SELECT source_video_id FROM productions"
        "                          WHERE source_video_id IS NOT NULL)"
    )
    params: list = [genre]
    if window_days and window_days > 0:
        sql += (" AND COALESCE(v.published_at, v.discovered_at)"
                " >= date('now', ?)")
        params.append(f"-{int(window_days)} days")
    sql += " ORDER BY COALESCE(v.published_at, v.discovered_at) DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def _recent_titles(conn, own_channel_id, limit: int = 10) -> list[str]:
    rows = conn.execute(
        "SELECT title FROM productions WHERE own_channel_id = ?"
        " ORDER BY id DESC LIMIT ?", (own_channel_id, limit)).fetchall()
    return [r["title"] for r in rows]


def _parse_reply(text: str) -> dict:
    """Pull the first JSON object out of an LLM reply."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def pick_prompt(own_channel, cands, produced: list[str]) -> str:
    lines = []
    for i, v in enumerate(cands, 1):
        when = (v["published_at"] or v["discovered_at"] or "")[:10]
        lines.append(f"{i}. id={v['video_id']} | {v['channel_name']} | "
                     f"{when} | {v['title']}")
    done = "\n".join(f"- {t}" for t in produced) or "- (nothing yet)"
    return (
        f'You are the producer for the YouTube channel '
        f'"{own_channel["name"]}" ({own_channel["genre"]}).\n\n'
        f"Choose the single best source video to adapt into a new ORIGINAL "
        f"video for this channel. Prefer freshness and a strong fit with the "
        f"channel's genre and audience, and avoid repeating a topic already "
        f"produced.\n\n"
        f"Already produced for this channel:\n{done}\n\n"
        f"Candidate source videos:\n" + "\n".join(lines) + "\n\n"
        f"Reply with ONLY a JSON object:\n"
        f'{{"source_video_id": "<one id from the list>", '
        f'"title": "<a compelling original title for the new video>", '
        f'"reason": "<one short line>"}}'
    )


def choose_topic(cfg, conn, own_channel, cands, provider: str | None,
                 topic_pick: str) -> dict:
    """Decide which candidate to produce. Returns
    {video, title, reason, method}. Never raises for a bad LLM reply - it
    falls back to the newest candidate."""
    newest = cands[0]
    if topic_pick != "llm" or not provider:
        return {"video": newest, "title": newest["title"],
                "reason": "newest candidate", "method": "newest"}
    if not studio.provider_ready(cfg, provider):
        return {"video": newest, "title": newest["title"],
                "reason": f"provider '{provider}' not ready - newest used",
                "method": "newest"}
    prompt = pick_prompt(own_channel, cands, _recent_titles(conn, own_channel["id"]))
    try:
        reply = _parse_reply(studio.llm_generate(cfg, prompt, provider=provider))
    except Exception as exc:  # noqa: BLE001 - never lose the run over a pick
        return {"video": newest, "title": newest["title"],
                "reason": f"LLM pick failed ({exc}) - newest used",
                "method": "newest"}
    wanted = str(reply.get("source_video_id") or "")
    chosen = next((v for v in cands if v["video_id"] == wanted), None)
    if chosen is None:
        return {"video": newest, "title": newest["title"],
                "reason": "LLM returned an unknown id - newest used",
                "method": "newest"}
    title = str(reply.get("title") or "").strip() or chosen["title"]
    return {"video": chosen, "title": title,
            "reason": str(reply.get("reason") or "").strip() or "LLM pick",
            "method": "llm"}


def build_plan(cfg, conn) -> list[dict]:
    """What Auto Run would do right now, per own channel (no LLM calls, so
    the confirm modal is free to open)."""
    vals = settings.load(conn)
    plan: list[dict] = []
    if not vals["autorun_enabled"]:
        return [{"action": "pause", "detail": "Auto Run is off in Settings"}]
    if not _in_window(vals):
        return [{"action": "pause",
                 "detail": f"outside the run window "
                           f"({vals['run_window_start']}-{vals['run_window_end']})"}]
    global_cap = int(vals["per_day"] or 0)
    made_today = _created_today(conn)
    if global_cap and made_today >= global_cap:
        return [{"action": "pause",
                 "detail": f"daily cap reached ({made_today}/{global_cap} "
                           f"created today)"}]
    window = int(vals.get("candidate_window_days") or 0)
    for oc in db.list_own_channels(conn, active_only=True):
        eff = settings.for_production(conn, {"own_channel_id": oc["id"]})
        entry = {"own_channel_id": oc["id"], "own_channel": oc["name"],
                 "genre": oc["genre"], "engine": eff["engine"],
                 "topic_pick": eff["topic_pick"], "per_day": eff["per_day"]}
        if not eff["autorun_enabled"]:
            plan.append({**entry, "action": "skip",
                         "detail": "auto-run is off for this channel"})
            continue
        cap = int(eff["per_day"] or 0)
        made_here = _created_today(conn, oc["id"])
        if cap and made_here >= cap:
            plan.append({**entry, "action": "skip",
                         "detail": f"channel daily cap reached "
                                   f"({made_here}/{cap})"})
            continue
        if global_cap and made_today + sum(
                1 for p in plan if p["action"] == "run") >= global_cap:
            plan.append({**entry, "action": "skip",
                         "detail": "global daily cap reached"})
            continue
        cands = candidates(conn, oc, window)
        if not cands:
            plan.append({**entry, "action": "skip",
                         "detail": f"no un-produced transcribed video in "
                                   f"genre '{oc['genre']}'"})
            continue
        if eff["topic_pick"] == "llm":
            plan.append({**entry, "action": "run", "candidates": len(cands),
                         "detail": f"{len(cands)} candidate(s) - the producer "
                                   f"LLM will pick the best"})
        else:
            top = cands[0]
            plan.append({**entry, "action": "run", "candidates": len(cands),
                         "source_video_id": top["video_id"],
                         "source_title": top["title"],
                         "detail": f"newest candidate: [{top['channel_name']}] "
                                   f"{top['title'][:70]}"})
    return plan


def run(cfg, log=None, job=None) -> dict:
    """Create + queue a production per runnable channel, then run each one.

    Returns {created: [...], skipped: [...], result: 'ok'|'paused:...'|
    'failed:...'}. Stops the whole run on the first pause/failure - a paused
    production needs a human, so continuing would just pile up.
    """
    from . import autorun

    log = log or (lambda m: None)
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        vals = settings.load(conn)
        plan = build_plan(cfg, conn)
        provider = vals.get("producer_llm_provider") or None
        window = int(vals.get("candidate_window_days") or 0)
        created: list[dict] = []
        skipped: list[dict] = []
        for entry in plan:
            if job is not None and job.cancel:
                return {"created": created, "skipped": skipped,
                        "result": "stopped"}
            if entry["action"] != "run":
                skipped.append(entry)
                log(f"[produce] {entry.get('own_channel', '-')}: "
                    f"{entry['detail']}")
                continue
            oc = db.get_own_channel(conn, entry["own_channel_id"])
            cands = candidates(conn, oc, window)
            if not cands:
                continue
            eff = settings.for_production(conn, {"own_channel_id": oc["id"]})
            choice = choose_topic(cfg, conn, oc, cands, provider,
                                  eff["topic_pick"])
            video = choice["video"]
            title = choice["title"][:200]
            log(f"[produce] {oc['name']}: {choice['method']} pick -> "
                f"'{title}' (from [{video['channel_name']}] {video['title'][:60]})"
                + (f" - {choice['reason']}" if choice["reason"] else ""))
            pid = db.create_production(conn, title, oc["genre"],
                                       video["video_id"], None)
            db.update_production(conn, pid, own_channel_id=oc["id"],
                                 llm_provider=provider)
            prod = db.get_production(conn, pid)
            seeded = studio.seed_production(cfg, conn, prod, log=log)
            created.append({"pid": pid, "title": title,
                            "own_channel": oc["name"],
                            "source_video_id": video["video_id"],
                            "seed": seeded})
        conn.commit()
    finally:
        conn.close()

    result = "ok"
    for item in created:
        if job is not None and job.cancel:
            result = "stopped"
            break
        log(f"[produce] running production {item['pid']}: {item['title']}")
        result = autorun.run_pipeline(cfg, item["pid"], job=job, log=log)
        if result != "ok":
            log(f"[produce] stopping: production {item['pid']} returned "
                f"{result}")
            break
    return {"created": created, "skipped": skipped, "result": result}
