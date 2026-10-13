"""Why a source video works: a scored read of its transcript.

Modelled on the dimensions a script analysis tool reports (hook strength,
curiosity loops, pacing and structure, emotional triggers, pattern
interrupts, promise delivery, ending / call to action). It runs in a web
chat like the other research jobs, so it costs no credits, and is saved
next to the transcripts. The result is advice for the writer about STRUCTURE
and technique - never wording to copy - and is copied into a production made
from that video.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Callable

DIMENSIONS = [
    ("hook", "Hook strength", "The opening 15 seconds: does it create a "
     "stake or an open question the viewer must see answered?"),
    ("curiosity", "Curiosity loops", "Questions opened and deferred, teases "
     "of what comes next, 'but first' bridges that keep people watching."),
    ("structure", "Pacing & structure", "Order of the beats, clarity of "
     "transitions, where it speeds up or slows down."),
    ("emotion", "Emotional triggers", "Fear of loss, relief, status, "
     "surprise: where the script makes the viewer feel something."),
    ("interrupts", "Pattern interrupts", "Changes of rhythm or format that "
     "reset attention: a switch to a number, a story, a contrast."),
    ("promise", "Promise delivery", "Does the video deliver exactly what the "
     "title and opening promised, and is it specific?"),
    ("ending", "Ending & call to action", "How it lands and what it asks of "
     "the viewer."),
]
KEYS = [k for k, _, _ in DIMENSIONS]
LABELS = {k: label for k, label, _ in DIMENSIONS}
MAX_CHARS = 60000


def path_for(cfg, video_id: str) -> Path:
    return Path(cfg.db_path).parent / "analysis" / f"{video_id}.json"


def load(cfg, video_id: str) -> dict | None:
    try:
        data = json.loads(path_for(cfg, video_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def prompt(title: str, transcript: str) -> str:
    dims = "\n".join(f"- {k} ({label}): {desc}"
                     for k, label, desc in DIMENSIONS)
    text = transcript.strip()
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n[...transcript cut...]"
    return (
        "You analyse why a YouTube video's SCRIPT works. Read the transcript "
        "and score it on each dimension from 0 to 10 (one decimal), be "
        "honest: most scripts have weak spots.\n\n"
        f"Dimensions:\n{dims}\n\n"
        "For every dimension give: a score, ONE short key quote from the "
        "transcript (under 20 words, copied exactly) that shows it best, and "
        "ONE sentence of analysis about the TECHNIQUE used (not what the "
        "video is about). Then give 3-5 'takeaways': techniques a writer of a "
        "different video on a different topic could reuse, described in your "
        "own words, no phrases from the transcript.\n\n"
        "Reply with ONE JSON object and nothing else:\n"
        '{"summary": "two sentences", "dimensions": [{"key": "hook", '
        '"score": 8.5, "quote": "...", "analysis": "..."}], '
        '"takeaways": ["..."]}\n'
        "Use exactly these keys: " + ", ".join(KEYS) + ".\n\n"
        f"TITLE: {title}\n\nTRANSCRIPT:\n{text}\n")


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(min(10.0, max(0.0, v)), 1)


def parse(raw) -> dict:
    """Clean a reply: only the known keys, scores 0-10, short strings."""
    raw = raw if isinstance(raw, dict) else {}
    by_key = {}
    for d in raw.get("dimensions") or []:
        if isinstance(d, dict) and str(d.get("key")) in KEYS:
            score = _num(d.get("score"))
            if score is None:
                continue
            by_key[str(d["key"])] = {
                "key": str(d["key"]), "label": LABELS[str(d["key"])],
                "score": score,
                "quote": re.sub(r"\s+", " ", str(d.get("quote") or "")).strip()[:200],
                "analysis": re.sub(r"\s+", " ", str(d.get("analysis") or "")).strip()[:400]}
    dims = [by_key[k] for k in KEYS if k in by_key]
    take = [re.sub(r"\s+", " ", str(t)).strip()[:300]
            for t in (raw.get("takeaways") or []) if str(t).strip()][:6]
    overall = (round(sum(d["score"] for d in dims) / len(dims), 1)
               if dims else None)
    return {"summary": str(raw.get("summary") or "").strip()[:500],
            "dimensions": dims, "takeaways": take, "overall": overall}


def writer_note(analysis: dict | None) -> str:
    """Technique advice for the writer: structure only, nothing to copy."""
    if not analysis or not analysis.get("dimensions"):
        return ""
    lines = ["WHAT MADE THE SOURCE WORK (technique only - do not reuse its "
             "wording, examples or structure beat for beat):"]
    for t in analysis.get("takeaways") or []:
        lines.append(f"- {t}")
    weak = [d for d in analysis["dimensions"] if d["score"] < 6.5]
    for d in weak:
        lines.append(f"- The source was weak on {d['label'].lower()}; do it "
                     "better.")
    return "\n".join(lines)


def run(cfg, video_id: str, writer: str = "zai", log: Callable = print,
        should_stop: Callable[[], bool] = lambda: False,
        options: dict | None = None) -> dict:
    from . import db, studio, webstages as ws
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        row = db.get_video(conn, video_id)
    finally:
        conn.close()
    if not row or not row["transcript_path"] or not Path(row["transcript_path"]).exists():
        raise ws.StageFailed("this video has no transcript yet")
    text = Path(row["transcript_path"]).read_text(encoding="utf-8")
    log(f"analysis: reading {row['title']}")
    with ws.web_transport(cfg, log, options) as t:
        t.set_stop(should_stop)
        reply = ws._send(t, writer, lambda f: prompt(row["title"], text), log,
                         ready=ws._has_json)
        data = parse(studio._parse_json_object(reply))
        if len(data["dimensions"]) < 5:
            log(f"{writer}: only {len(data['dimensions'])} dimension(s) - asking again")
            reply = t.ask(writer, "Your reply was missing dimensions. Reply with "
                          "ONE JSON object with all seven keys: "
                          + ", ".join(KEYS) + ".", (), new_chat=False,
                          ready=ws._has_json)
            data = parse(studio._parse_json_object(reply))
    if len(data["dimensions"]) < 5:
        raise ws.StageFailed(f"{writer} did not return a usable analysis")
    data.update({"video_id": video_id, "title": row["title"], "writer": writer,
                 "made_at": _dt.datetime.now().isoformat(timespec="minutes")})
    p = path_for(cfg, video_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"analysis: saved ({data['overall']}/10 overall)")
    return data
