"""Short proposals for a long video: planned after its subtitles, created only
when you choose.

`propose` picks the strongest moments of a video from its subtitles (the same
rule-based picker as the Shorts page, `shorts.plan_clips`) and saves them as
`shorts_plan.json` in the production folder. NOTHING is created from a
proposal: each one waits as "proposed" until you create it (it then becomes
"created" with the new production's id) or skip it.

The plan belongs to one version of the subtitles. If the subtitles change, the
old proposals no longer match the times, so `propose` replaces them; Shorts
that were already created are remembered under `previous_created`.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from . import shorts

PLAN_FILE = "shorts_plan.json"
MINUTES_PER_SHORT = 3.0     # about one Short for every 3 minutes of narration
MAX_SHORTS = 6
PROPOSED, CREATED, SKIPPED = "proposed", "created", "skipped"
STATUSES = (PROPOSED, CREATED, SKIPPED)
VERSION = 1


def count_for(duration_seconds: float, minutes_per=MINUTES_PER_SHORT,
              max_n=MAX_SHORTS) -> int:
    """How many Shorts a video of this length should get (at least 1)."""
    try:
        per = float(minutes_per)
    except (TypeError, ValueError):
        per = MINUTES_PER_SHORT
    if per <= 0:
        per = MINUTES_PER_SHORT
    try:
        cap = int(max_n)
    except (TypeError, ValueError):
        cap = MAX_SHORTS
    n = int(max(0.0, float(duration_seconds)) / 60.0 / per + 0.5)
    return max(1, min(max(1, cap), n))


def plan_path(pdir) -> Path:
    return Path(pdir) / PLAN_FILE


def fingerprint(srt_text: str) -> str:
    norm = "\n".join(l.rstrip() for l in (srt_text or "").replace("\r", "").strip().split("\n"))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def load(pdir) -> dict | None:
    """The saved plan, or None when there is none (or it cannot be read)."""
    try:
        data = json.loads(plan_path(pdir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    return data


def _save(pdir, plan: dict) -> None:
    path = plan_path(pdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def is_stale(plan: dict | None, srt_text: str) -> bool:
    """True when the subtitles changed since the plan was made."""
    return bool(plan) and plan.get("srt_hash") != fingerprint(srt_text)


def _overlaps(a0, a1, b0, b1) -> bool:
    return a0 < b1 and b0 < a1


def _next_number(items: list[dict]) -> int:
    best = 0
    for it in items:
        try:
            best = max(best, int(str(it.get("id", "s0"))[1:]))
        except ValueError:
            pass
    return best + 1


def _retitle(items: list[dict], source_title: str) -> None:
    """'<title> - Short 2 of 4' for the proposals, counted over the Shorts
    that are still wanted (skipped ones do not count)."""
    live = [it for it in items if it["status"] != SKIPPED]
    total = len(live)
    for rank, it in enumerate(live, 1):
        if it["status"] == PROPOSED:
            label = f"Short {rank} of {total}"
            it["title"] = (f"{source_title} - {label}" if source_title else label)[:300]


def propose(pdir, srt_text: str, *, source_title: str = "",
            minutes_per=MINUTES_PER_SHORT, max_n=MAX_SHORTS) -> dict:
    """(Re)make the proposals for a video and save them.

    Keeps the Shorts already created or skipped (when the subtitles are
    unchanged), never proposes a moment that overlaps one of them, and tops the
    list up so that created + proposed Shorts come to `count_for(...)`."""
    cues = shorts.parse_srt(srt_text)
    if not cues:
        raise ValueError("The subtitles file has no cues")
    duration = cues[-1]["end"]
    n = count_for(duration, minutes_per, max_n)
    fp = fingerprint(srt_text)

    old = load(pdir)
    kept: list[dict] = []
    previous: list[dict] = list((old or {}).get("previous_created") or [])
    if old:
        if old.get("srt_hash") == fp:
            kept = [it for it in old["items"] if it.get("status") in (CREATED, SKIPPED)]
        else:      # times no longer match: remember what was created, drop the rest
            previous += [{"title": it.get("title"), "pid": it.get("pid"),
                          "start": it.get("start"), "end": it.get("end")}
                         for it in old["items"] if it.get("status") == CREATED]

    created = [it for it in kept if it["status"] == CREATED]
    want = max(0, n - len(created))
    pool = shorts.plan_clips(cues, n=n + 2 * len(kept) + 8)
    pool = [c for c in pool
            if not any(_overlaps(c["start"], c["end"], k["start"], k["end"]) for k in kept)]
    pool.sort(key=lambda c: -c["score"])
    chosen = sorted(pool[:want], key=lambda c: c["start"])

    items = list(kept)
    num = _next_number(items)
    for c in chosen:
        items.append({"id": f"s{num}", "start": round(c["start"], 2),
                      "end": round(c["end"], 2), "score": c["score"],
                      "hook": c["hook"], "title": "", "status": PROPOSED, "pid": None})
        num += 1
    items.sort(key=lambda it: it["start"])
    _retitle(items, source_title)

    plan = {"version": VERSION, "srt_hash": fp, "duration": round(duration, 2),
            "wanted": n, "source_title": source_title,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "items": items, "previous_created": previous}
    _save(pdir, plan)
    return plan


def _item(plan: dict, item_id: str) -> dict:
    for it in plan["items"]:
        if it.get("id") == item_id:
            return it
    raise KeyError(f"No proposed Short '{item_id}'")


def parse_clock(text, default=None) -> float:
    """'83', '83.5', '1:23' or '1:02:03' -> seconds. Empty -> default."""
    t = str(text if text is not None else "").strip().replace(",", ".")
    if not t:
        if default is None:
            raise ValueError("A time is missing")
        return float(default)
    try:
        secs = 0.0
        for part in t.split(":"):
            secs = secs * 60 + float(part)
        return secs
    except ValueError:
        raise ValueError(f"'{text}' is not a time (use seconds or mm:ss)") from None


def update_item(pdir, item_id: str, **changes) -> dict:
    """Edit the times / title of a proposal that is still waiting."""
    plan = load(pdir)
    if plan is None:
        raise KeyError("There is no Shorts plan for this video")
    it = _item(plan, item_id)
    if it["status"] != PROPOSED:
        raise ValueError("Only a proposal that is still waiting can be edited")
    start = float(changes.get("start", it["start"]))
    end = float(changes.get("end", it["end"]))
    if start < 0 or end <= start:
        raise ValueError("The end must be after the start")
    it["start"], it["end"] = round(start, 2), round(end, 2)
    if changes.get("title") is not None:
        it["title"] = str(changes["title"]).strip()[:300] or it["title"]
    _save(pdir, plan)
    return it


def _set_status(pdir, item_id: str, status: str, pid=None) -> dict:
    plan = load(pdir)
    if plan is None:
        raise KeyError("There is no Shorts plan for this video")
    it = _item(plan, item_id)
    it["status"] = status
    it["pid"] = pid
    _retitle(plan["items"], plan.get("source_title", ""))
    _save(pdir, plan)
    return it


def mark_created(pdir, item_id: str, pid: int) -> dict:
    return _set_status(pdir, item_id, CREATED, pid)


def mark_skipped(pdir, item_id: str) -> dict:
    return _set_status(pdir, item_id, SKIPPED)


def unskip(pdir, item_id: str) -> dict:
    return _set_status(pdir, item_id, PROPOSED)


def waiting(plan: dict | None) -> list[dict]:
    """The proposals nobody has decided on yet."""
    return [it for it in (plan or {}).get("items", []) if it.get("status") == PROPOSED]


# ---------------------------------------------------------- integration ---

def enabled(cfg) -> bool:
    """studio.shorts_plan in config.yaml (on by default)."""
    return bool(getattr(cfg, "studio_shorts_plan", True))


def plan_for_production(cfg, pid: int, *, refresh: bool = False) -> dict | None:
    """Make the proposals for a production that has its subtitles.

    Returns the plan, or None when this production should not get Shorts: it
    has no subtitles yet, it is itself a Short (vertical, or made from a
    window of another video), or planning is switched off. An existing plan is
    left alone unless `refresh` is set - the subtitles may have been edited
    after you already chose, so a changed plan is offered, not forced."""
    if not enabled(cfg):
        return None
    from . import db, frames, studio

    pdir = studio.prod_dir(cfg, pid)
    srt = studio.find_srt(pdir)
    if not srt or (pdir / "short_source.json").exists():
        return None
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        prod = db.get_production(conn, pid)
        oc = (db.get_own_channel(conn, prod["own_channel_id"])
              if prod is not None and prod["own_channel_id"] else None)
    finally:
        conn.close()
    if prod is None or frames.is_vertical(prod, oc):
        return None
    existing = load(pdir)
    if existing is not None and not refresh:
        return existing
    return propose(
        pdir, srt.read_text(encoding="utf-8", errors="replace"),
        source_title=prod["title"] or "",
        minutes_per=getattr(cfg, "studio_shorts_minutes_per", MINUTES_PER_SHORT),
        max_n=getattr(cfg, "studio_shorts_max", MAX_SHORTS))


def plan_after_subtitles(cfg, pid: int) -> None:
    """Called when a production has just got new subtitles. Never raises:
    planning Shorts must not make the subtitles stage fail."""
    try:
        plan_for_production(cfg, pid, refresh=True)
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger("whisperradar").warning(
            "Shorts planning for production %s failed: %s", pid, exc)


def waiting_count(cfg, pid: int) -> int:
    """How many proposals wait for a decision (for the badge on the video page)."""
    try:
        from . import studio
        return len(waiting(load(studio.prod_dir(cfg, pid))))
    except Exception:  # noqa: BLE001 - a badge must never break a page
        return 0
