"""Best time to post, from the niche's own data: for the videos we track,
which publish weekday/hour (UTC) went with the strongest results.

Pure math on stored videos - the multiplier already normalises for each
channel's size, so slots are comparable. With too little data a slot is not
reported (an honest "not enough data" beats a confident guess)."""
from __future__ import annotations

import statistics

from . import outliers

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MIN_PER_SLOT = 3


def slots(items: list[dict], by: str = "weekday") -> list[dict]:
    """Median multiplier per slot, best first. `by`: "weekday" or "hour"."""
    groups: dict[int, list[float]] = {}
    for i in items:
        when = outliers.parse_date(i.get("published_at"))
        if when is None or i.get("is_short") or i.get("multiplier") is None:
            continue
        key = when.weekday() if by == "weekday" else when.hour
        groups.setdefault(key, []).append(float(i["multiplier"]))
    out = []
    for key, vals in groups.items():
        if len(vals) < MIN_PER_SLOT:
            continue
        out.append({"slot": key,
                    "label": DAYS[key] if by == "weekday" else f"{key:02d}:00",
                    "videos": len(vals),
                    "median": round(statistics.median(vals), 2)})
    out.sort(key=lambda s: (-s["median"], -s["videos"]))
    return out


def best(items: list[dict]) -> dict:
    """{"weekday": [...], "hour": [...], "ready": bool, "note": str}"""
    wd, hr = slots(items, "weekday"), slots(items, "hour")
    ready = bool(wd or hr)
    return {"weekday": wd, "hour": hr[:6], "ready": ready,
            "note": "Times are UTC. Based on how videos did against their "
                    "own channel's norm; slots with fewer than "
                    f"{MIN_PER_SLOT} videos are left out."
                    if ready else "Not enough dated videos yet."}
