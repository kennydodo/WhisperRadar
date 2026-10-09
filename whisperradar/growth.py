"""Channel growth from our own daily channel snapshots: subscribers per day
over the window we have. Rising channels get noticed before their videos
look like outliers; not enough snapshots (or a hidden count) is no verdict,
never a guess."""
from __future__ import annotations

MIN_SPAN_DAYS = 0.5          # two snapshots the same hour say nothing


def velocity(snaps: list[dict]) -> dict | None:
    """[{taken_at, subscribers, ...}] oldest first -> {per_day, delta,
    span_days, latest} over subscriber counts, or None."""
    dated = []
    for s in snaps or []:
        when, subs = s.get("taken_at"), s.get("subscribers")
        if not when or subs is None:
            continue
        try:
            import datetime as _dt
            dated.append((_dt.datetime.fromisoformat(when), int(subs)))
        except (ValueError, TypeError):
            continue
    if len(dated) < 2:
        return None
    (t0, s0), (t1, s1) = dated[0], dated[-1]
    span = (t1 - t0).total_seconds() / 86400.0
    if span < MIN_SPAN_DAYS:
        return None
    return {"per_day": (s1 - s0) / span, "delta": s1 - s0,
            "span_days": span, "latest": s1}
