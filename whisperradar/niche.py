"""What to make next: niche-level cards, computed from the watched-genre
data we already store. Nexlev-style "exploding niches" without their dataset.

Pure math over `outliers.build` items (grouped by genre): a niche's newest
videos beat their channels better or worse than the niche ever has (heat),
how crowded it is (uploads/week across the recent set) and whether anything
is breaking out right now. Honest verdicts only - too little dated data is
reported as "undated", not guessed.
"""
from __future__ import annotations

import statistics

RECENT = 30            # newest videos per niche that define "now"
OUTLIER_AT = 3.0       # multiplier that counts as an outlier
MIN_SCORED = 5         # fewer scored videos than this = no card
RISING_HEAT = 1.25     # recent vs all-time median that says "rising"
CROWD_SUPPLY = 8.0     # uploads/week that counts as crowded


def _verdict(heat, rate, breaks, upw, med_recent):
    """(label, plain-language why)."""
    if med_recent is None:
        return ("undated", "too few videos with publish dates to judge "
                           "what is happening now")
    if heat is not None and heat >= RISING_HEAT and (breaks or
                                                     rate >= 0.15):
        return ("rising", f"its newest videos beat their channels {heat}x "
                          "better than this niche ever has"
                + (f"; {breaks} video(s) breaking out right now"
                   if breaks else ""))
    if (upw is not None and upw >= CROWD_SUPPLY and heat is not None
            and heat < 0.9):
        return ("crowded", f"about {upw:.0f} uploads a week and performing "
                           "below its own norm")
    if med_recent >= OUTLIER_AT or rate >= 0.2:
        return ("strong", "recent videos regularly beat their channel's norm")
    return ("steady", "performing close to its usual - nothing says now")


def cards(items: list[dict], recent_n: int = RECENT,
          min_scored: int = MIN_SCORED) -> list[dict]:
    """One card per genre (needs >= min_scored scored videos), best first."""
    by: dict[str, list[dict]] = {}
    for i in items:
        g = (i.get("genre") or "").strip()
        if g:
            by.setdefault(g, []).append(i)
    out = []
    for genre, vids in by.items():
        if len(vids) < min_scored:
            continue
        dated = sorted((v for v in vids if v.get("age_days") is not None),
                       key=lambda v: v["age_days"])
        recent = dated[:recent_n]
        med_all = statistics.median(v["multiplier"] for v in vids)
        med_rec = (statistics.median(v["multiplier"] for v in recent)
                   if len(recent) >= 3 else None)
        heat = (round(med_rec / med_all, 2)
                if med_rec is not None and med_all else None)
        rate = (sum(1 for v in recent if v["multiplier"] >= OUTLIER_AT)
                / len(recent)) if recent else 0.0
        breaks = sum(1 for v in recent if v.get("breakout"))
        upw = None
        if len(recent) >= 2:
            span = recent[-1]["age_days"] - recent[0]["age_days"]
            if span > 0:
                upw = len(recent) / (span / 7.0)
        verdict, why = _verdict(heat, round(rate, 3), breaks, upw, med_rec)
        out.append({"genre": genre,
                    "channels": len({v["channel_id"] for v in vids}),
                    "videos": len(vids), "recent": len(recent),
                    "median_all": round(med_all, 2),
                    "recent_median": (round(med_rec, 2)
                                      if med_rec is not None else None),
                    "heat": heat, "outlier_rate": round(rate, 3),
                    "breakouts": breaks,
                    "uploads_per_week": round(upw, 1) if upw else None,
                    "verdict": verdict, "why": why})
    out.sort(key=lambda c: (-(c["recent_median"] or 0), -c["videos"]))
    return out
