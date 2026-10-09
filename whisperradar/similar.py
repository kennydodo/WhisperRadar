"""Channels that look like the ones we already watch.

The Data API has no "similar channels" endpoint, so this is plain term
overlap: every channel's title vocabulary becomes a TF-IDF vector over the
watched set and cosine similarity ranks the neighbours. Shared terms are
returned with each match so the claim is checkable, not magic.
"""
from __future__ import annotations

import math

from . import insights

MIN_VIDEOS = 5         # a two-video channel is not a profile
LIMIT_DEFAULT = 3
SHARED_TERMS = 4


def profiles(items: list[dict]) -> dict[str, dict]:
    """{channel_id: {name, genre, videos, terms{term: count}}}."""
    by: dict[str, dict] = {}
    for i in items:
        p = by.setdefault(i["channel_id"], {
            "channel_id": i["channel_id"], "name": i["channel_name"],
            "genre": i["genre"], "videos": 0, "terms": {}})
        p["videos"] += 1
        for t in insights._terms(i["title"] or ""):
            p["terms"][t] = p["terms"].get(t, 0) + 1
    return by


def _vectors(profs: dict[str, dict]) -> dict[str, dict]:
    """Unit-length TF-IDF vectors: term share within the channel weighted by
    log(1 + N/docs containing it), so niche words beat words everyone uses."""
    n = len(profs)
    df: dict[str, int] = {}
    for p in profs.values():
        for t in p["terms"]:
            df[t] = df.get(t, 0) + 1
    vecs = {}
    for cid, p in profs.items():
        total = sum(p["terms"].values()) or 1
        v = {t: (c / total) * math.log(1 + n / df[t])
             for t, c in p["terms"].items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs[cid] = {t: x / norm for t, x in v.items()}
    return vecs


def _cos(a: dict, b: dict) -> float:
    if len(b) < len(a):
        a, b = b, a
    return sum(x * b.get(t, 0.0) for t, x in a.items())


def _rank_from(cid: str, want: dict, vecs: dict, profs: dict,
               limit: int) -> list[dict]:
    scored = []
    for other, v in vecs.items():
        if other == cid:
            continue
        s = _cos(want, v)
        if s <= 0.01:
            continue
        shared = sorted((t for t in want if t in v),
                        key=lambda t: -(want[t] * v[t]))[:SHARED_TERMS]
        scored.append((s, other, shared))
    scored.sort(key=lambda r: -r[0])
    return [{"channel_id": o, "name": profs[o]["name"],
             "genre": profs[o]["genre"], "videos": profs[o]["videos"],
             "score": round(s, 3), "shared": sh}
            for s, o, sh in scored[:limit]]


def similar_to(items: list[dict], channel_id: str,
               limit: int = LIMIT_DEFAULT,
               min_videos: int = MIN_VIDEOS) -> list[dict]:
    """The watched channels most like this one, best first."""
    profs = profiles(items)
    if channel_id not in profs:
        return []
    pool = {cid: p for cid, p in profs.items() if p["videos"] >= min_videos}
    if channel_id not in pool or len(pool) < 2:
        return []
    vecs = _vectors(pool)
    return _rank_from(channel_id, vecs[channel_id], vecs, pool, limit)


def all_similar(items: list[dict], limit: int = LIMIT_DEFAULT,
                min_videos: int = MIN_VIDEOS) -> dict[str, list[dict]]:
    """{channel_id: similar list} for every big enough channel (one pass -
    the Channels tab annotates a whole page at once)."""
    pool = {cid: p for cid, p in profiles(items).items()
            if p["videos"] >= min_videos}
    if len(pool) < 2:
        return {}
    vecs = _vectors(pool)
    return {cid: _rank_from(cid, v, vecs, pool, limit)
            for cid, v in vecs.items()}
