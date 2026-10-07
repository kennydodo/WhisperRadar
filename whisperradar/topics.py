"""Topics: the outlier videos grouped by subject, so you can see WHICH topics
keep winning across different channels (a topic that beat the norm on several
channels is a safer bet than one viral video).

A web-chat writer groups the outliers; code checks the ids and computes the
numbers, so the chat only decides what belongs together.
"""
from __future__ import annotations

import json
import re
import statistics
from pathlib import Path
from typing import Callable

TOPICS_FILE = "research_topics.json"
MAX_VIDEOS = 80
MIN_TOPICS = 3


def topics_path(cfg) -> Path:
    return Path(cfg.db_path).parent / TOPICS_FILE


def load_topics(cfg) -> dict:
    try:
        data = json.loads(topics_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"topics": [], "made_at": None, "genre": ""}
    return data if isinstance(data, dict) and "topics" in data else {
        "topics": [], "made_at": None, "genre": ""}


def save_topics(cfg, data: dict) -> None:
    p = topics_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1, ensure_ascii=False),
                 encoding="utf-8")


def candidates(items: list[dict], genre: str = "", min_multiplier: float = 3.0,
               limit: int = MAX_VIDEOS) -> list[dict]:
    """The outliers the chat groups: best multipliers first, no Shorts."""
    from . import outliers
    shown, _n = outliers.filter_sort(items, min_multiplier=min_multiplier,
                                     genre=genre, limit=limit)
    return shown


def writer_prompt(videos: list[dict]) -> str:
    lines = "\n".join(
        f"{v['video_id']} | {v['title'][:110]} | {v['channel_name']} | "
        f"{v['multiplier']:.0f}x" for v in videos)
    return f"""You are a YouTube niche analyst. Below are {len(videos)} videos that each got far more views than their own channel normally does (id | title | channel | multiplier). Group them into TOPICS: the subject or angle that made them work, so someone can decide what to make next.

{lines}

Rules:
- 5 to 15 topics. A topic is a subject or angle (e.g. "animals that survive impossible cold"), not a channel and not a format.
- Every video goes in at most one topic; leave out videos that fit nowhere. Use only the ids above, copied exactly.
- A topic needs at least one video; prefer topics that several DIFFERENT channels share.
- For each topic give a one-line "angle": what a new video on it should promise.

Reply with ONE JSON object and nothing else:
{{"topics": [{{"name": "short topic name", "angle": "what a new video should promise", "video_ids": ["id", "id"]}}]}}"""


def parse_topics(raw, videos: list[dict]) -> list[dict]:
    """Topics with only valid ids, plus the numbers computed here (not by the
    chat). Sorted: more channels first, then best multiplier."""
    by_id = {v["video_id"]: v for v in videos}
    items = raw.get("topics") if isinstance(raw, dict) else raw
    seen: set[str] = set()
    out = []
    for t in items if isinstance(items, list) else []:
        if not isinstance(t, dict):
            continue
        name = re.sub(r"\s+", " ", str(t.get("name") or "")).strip()
        ids = []
        for i in t.get("video_ids") or []:
            i = str(i).strip()
            if i in by_id and i not in seen and i not in ids:
                ids.append(i)
        if not name or not ids:
            continue
        seen.update(ids)
        vids = sorted((by_id[i] for i in ids),
                      key=lambda v: v["multiplier"], reverse=True)
        out.append({
            "name": name[:80],
            "angle": re.sub(r"\s+", " ", str(t.get("angle") or "")).strip()[:240],
            "video_ids": [v["video_id"] for v in vids],
            "videos": [{"video_id": v["video_id"], "title": v["title"],
                        "channel_name": v["channel_name"],
                        "channel_id": v["channel_id"],
                        "genre": v["genre"],
                        "multiplier": round(v["multiplier"], 1),
                        "views": v["views"]} for v in vids],
            "channels": len({v["channel_id"] for v in vids}),
            "best": round(vids[0]["multiplier"], 1),
            "median": round(statistics.median(
                v["multiplier"] for v in vids), 1),
            "views": sum(v["views"] for v in vids),
        })
    out.sort(key=lambda t: (-t["channels"], -t["best"]))
    return out


def run_topics(cfg, transport, videos: list[dict], writer: str = "zai",
               log: Callable[[str], None] = print, genre: str = "") -> dict:
    from . import studio, webstages as ws
    if len(videos) < MIN_TOPICS:
        raise ws.StageFailed(
            f"Only {len(videos)} outlier video(s) to group - refresh view "
            "counts or lower the multiplier first.")
    log(f"topics: grouping {len(videos)} outlier video(s)")
    reply = ws._send(transport, writer, lambda f: writer_prompt(videos), log,
                     ready=ws._has_json)
    topics = []
    for attempt in range(3):
        topics = parse_topics(studio._parse_json_object(reply), videos)
        if len(topics) >= MIN_TOPICS:
            break
        if attempt == 2:
            break
        log(f"{writer}: only {len(topics)} usable topic(s) - asking again")
        reply = transport.ask(
            writer, "Your reply had too few usable topics. Reply with ONE "
            "JSON object {\"topics\": [...]} with at least 5 topics, using "
            "only the ids given, and nothing else.", (), new_chat=False,
            ready=ws._has_json)
    if len(topics) < MIN_TOPICS:
        raise ws.StageFailed(f"{writer} did not return usable topics")
    import datetime as _dt
    data = {"topics": topics, "genre": genre,
            "made_at": _dt.datetime.now().isoformat(timespec="minutes")}
    save_topics(cfg, data)
    log(f"topics: {len(topics)} topic(s) saved")
    return data


def topics_job(cfg, writer: str, genre: str, min_multiplier: float, log,
               should_stop: Callable[[], bool] = lambda: False,
               options: dict | None = None) -> None:
    from . import db, outliers, webstages as ws
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        rows = conn.execute(
            "SELECT v.video_id, v.channel_id, c.name AS channel_name,"
            " c.genre AS genre, v.title, v.url, v.published_at,"
            " v.view_count, v.duration, v.status FROM videos v"
            " JOIN channels c ON c.channel_id = v.channel_id"
            " WHERE c.active = 1 AND v.view_count IS NOT NULL").fetchall()
    finally:
        conn.close()
    videos = candidates(outliers.build(rows), genre, min_multiplier)
    with ws.web_transport(cfg, log, options) as t:
        log(f"settings: {t.options}")
        t.set_stop(should_stop)
        run_topics(cfg, t, videos, writer, log, genre)
