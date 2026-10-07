"""Research insights computed from the stored videos, no LLM: which words
keep appearing in winning titles, why one video might have won, and which
watched channels produce the most outliers."""
from __future__ import annotations

import math
import re
import statistics

STOP = set("""a an the and or but of to in on at for from with without by as is are was
were be been it its this that these those you your we our they them their i me my he
she his her not no yes do does did done have has had can could will would should
just so than then there here what when where which who whom why how all any some more
most very into out up down over under about after before again also only own same too
vs video videos new best top full""".split())
_WORD = re.compile(r"[a-z0-9']+")


def tokens(title: str) -> list[str]:
    return [w.strip("'") for w in _WORD.findall((title or "").lower())]


def _terms(title: str) -> set[str]:
    words = tokens(title)
    out = {w for w in words if len(w) >= 3 and w not in STOP and not w.isdigit()}
    for a, b in zip(words, words[1:]):
        if a not in STOP and b not in STOP and len(a) >= 3 and len(b) >= 3:
            out.add(f"{a} {b}")
    return out


def keywords(items: list[dict], min_multiplier: float = 3.0, top: int = 40,
             min_out: int = 3, min_all: int = 5) -> list[dict]:
    """Terms over-represented in outlier titles: lift = share of outlier
    titles with the term / share of all titles with it."""
    outs = [i for i in items if i["multiplier"] >= min_multiplier
            and not i.get("is_short")]
    if len(outs) < min_out or not items:
        return []
    count_all: dict[str, int] = {}
    count_out: dict[str, int] = {}
    example: dict[str, str] = {}
    for i in items:
        for t in _terms(i["title"]):
            count_all[t] = count_all.get(t, 0) + 1
    for i in outs:
        for t in _terms(i["title"]):
            count_out[t] = count_out.get(t, 0) + 1
            example.setdefault(t, i["title"])
    rows = []
    for t, c in count_out.items():
        a = count_all.get(t, c)
        if c < min_out or a < min_all:
            continue
        lift = (c / len(outs)) / (a / len(items))
        if lift < 1.3:
            continue
        rows.append({"term": t, "outliers": c, "all": a, "lift": lift,
                     "example": example[t],
                     "score": lift * math.log(c + 1)})
    rows.sort(key=lambda r: -r["score"])
    return rows[:top]


def explain(item: dict, items: list[dict], terms: list[dict] | None = None
            ) -> list[str]:
    """Plain-language observations about why one video beat its norm,
    compared with the rest of its channel. Facts, not guesses."""
    out = []
    mates = [i for i in items if i["channel_id"] == item["channel_id"]
             and i["video_id"] != item["video_id"]]
    out.append(f"It got {item['multiplier']:.1f}x the channel's typical views "
               f"({item['views']:,} vs about {item['baseline']:,.0f}).")
    title = item["title"] or ""
    if mates:
        med_len = statistics.median(len(i["title"] or "") for i in mates)
        diff = len(title) - med_len
        if abs(diff) >= 12:
            out.append(f"Its title is {'longer' if diff > 0 else 'shorter'} "
                       f"than the channel's usual ({len(title)} vs about "
                       f"{med_len:.0f} characters).")
        durs = [i["duration"] for i in mates if i.get("duration")]
        if item.get("duration") and len(durs) >= 3:
            md = statistics.median(durs)
            if item["duration"] > md * 1.4 or item["duration"] < md * 0.7:
                out.append(f"Length: {item['duration'] // 60} min against a "
                           f"channel median of {md / 60:.0f} min.")
    if re.search(r"\d", title):
        out.append("The title has a number.")
    if title.strip().endswith("?"):
        out.append("The title is a question.")
    if terms:
        hits = [t["term"] for t in terms if t["term"] in _terms(title)][:4]
        if hits:
            out.append("It uses words that keep appearing in winning "
                       "titles: " + ", ".join(hits) + ".")
    if item.get("trend") == "rising":
        out.append("It is still gaining faster than its lifetime average.")
    elif item.get("trend") == "cooling":
        out.append("Its growth has slowed against its lifetime average.")
    related = [i for i in items if i["video_id"] != item["video_id"]
               and i["multiplier"] >= 3 and len(
                   _terms(title) & _terms(i["title"])) >= 2][:5]
    if related:
        chans = {i["channel_name"] for i in related}
        out.append(f"{len(related)} other outlier(s) share its subject"
                   + (f" on {len(chans)} channel(s)" if chans else "")
                   + ": " + "; ".join(i["title"][:50] for i in related[:3]))
    return out


def rank_channels(items: list[dict], outlier_at: float = 5.0) -> list[dict]:
    """Watched channels by how often they beat their own norm."""
    by: dict[str, list[dict]] = {}
    for i in items:
        by.setdefault(i["channel_id"], []).append(i)
    rows = []
    for cid, vids in by.items():
        wins = [v for v in vids if v["multiplier"] >= outlier_at]
        recent = sorted((v for v in vids if v["age_days"] is not None),
                        key=lambda v: v["age_days"])[:10]
        rows.append({
            "channel_id": cid, "name": vids[0]["channel_name"],
            "genre": vids[0]["genre"], "scored": len(vids),
            "outliers": len(wins),
            "rate": len(wins) / len(vids),
            "best": max(v["multiplier"] for v in vids),
            "recent_median": (statistics.median(v["multiplier"]
                                                for v in recent)
                              if len(recent) >= 3 else None),
            "rising": sum(1 for v in vids if v.get("trend") == "rising")})
    rows.sort(key=lambda r: (-r["rate"], -r["best"]))
    return rows
