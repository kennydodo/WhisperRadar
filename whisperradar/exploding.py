"""Exploding small channels: channels you do NOT watch yet whose recent uploads
beat THEIR OWN norm.

This is the part of "what to make" that the watched-channel Outliers tab cannot
do: it only sees channels you already follow. Here the YouTube Data API is used
to go and find the others, in three cheap stages:

  A  search      recent videos for your topics (search.list, 100 units a page)
  B  overview    subscribers / age / upload playlist of every channel found
                 (channels.list, 1 unit per 50)
  C  check       the newest ~50 uploads of the most promising small channels
                 (playlistItems + videos.list, about 2 units a channel), scored
                 with the same math as the Outliers tab (outliers.build):
                 views / median of the channel's previous uploads

Only stage A is expensive, so a run states its worst-case cost up front and the
quota guard in youtube_api stops it before the free daily limit (a partial
result is still saved).

Everything fetched is also kept on this computer (data/exploding_cache.json:
searches 1 day, channel sizes 3 days, upload histories 2 days, featured-channel
lists 14 days), so repeating a search costs no API units and `rescore` can
re-judge the saved histories with new thresholds for free.

Snowball: the strongest results become seeds. Their featured channels (1 unit
per 50) and searches for the titles of their best videos (100 units each) turn
up similar small channels, which go through the same check; new strong finds
seed the next round.

Honest limits: the score is a transparent heuristic over public numbers, not a
prediction; a single viral video on a tiny channel is shown with the evidence
(outliers in the last 10 uploads) so a fluke can be told from a repeatable
format; search only reaches what YouTube ranks for the phrase, so use several
specific phrases rather than one broad one.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import re
import statistics
from pathlib import Path

from . import outliers, youtube_api

FILE = "exploding.json"
KEEP_RUNS = 20
RECHECK_AFTER_DAYS = 2        # a channel checked this recently is not re-checked
MAX_QUERIES = 12
CHECK_UNITS = 2               # playlistItems page + videos.list for <=50 uploads
CACHE_FILE = "exploding_cache.json"
CACHE_TTL_DAYS = {"search": 1, "overview": 3, "uploads": 2, "featured": 14}
CACHE_KEEP_DAYS = 30          # older saved entries are dropped when saving
RESCORE_MAX_DAYS = 7          # rescore trusts saved upload histories this fresh
SNOWBALL = {
    "rounds": 2,              # expand, check, expand from the new finds, ...
    "phrases": 3,             # "like: <best title>" searches per round (100 units each)
    "min_seed_score": 60,     # only strong channels seed a round
    "max_seeds": 8,           # seeds per round
}

DEFAULTS = {
    "days": 14,               # search window: videos published in the last N days
    "max_subs": 100_000,      # "small": at most this many subscribers
    "min_mult": 3.0,          # a video must beat its channel's norm by this much
    "min_views": 3_000,       # ...and have at least this many views
    "pages": 1,               # search pages per query (100 units each)
    "check_limit": 60,        # channels whose uploads get checked (~2 units each)
    "min_videos": 8,          # fewer uploads than this = no reliable norm
    "english_only": True,
}


# ---- storage ----------------------------------------------------------------

def path(cfg) -> Path:
    return Path(cfg.db_path).parent / FILE


def empty() -> dict:
    return {"channels": {}, "checked": {}, "dismissed": [], "runs": [],
            "snowballed": {}}


def load(cfg) -> dict:
    try:
        data = json.loads(path(cfg).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {**empty(), **data}
    except (OSError, ValueError):
        pass
    return empty()


def save(cfg, data: dict) -> None:
    p = path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    data["runs"] = data.get("runs", [])[-KEEP_RUNS:]
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(p)


def dismiss(cfg, channel_id: str) -> None:
    data = load(cfg)
    if channel_id and channel_id not in data["dismissed"]:
        data["dismissed"].append(channel_id)
    data["channels"].pop(channel_id, None)
    save(cfg, data)


class Cache:
    """What the API already told us, kept between runs so the same question is
    never paid for twice. Safe to delete: the next run just refetches."""

    def __init__(self, cfg):
        self.path = Path(cfg.db_path).parent / CACHE_FILE
        self.d = {k: {} for k in CACHE_TTL_DAYS}
        self.dirty = False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for k in self.d:
                if isinstance(raw.get(k), dict):
                    self.d[k] = raw[k]
        except (OSError, ValueError, AttributeError):
            pass

    @staticmethod
    def _age_days(at, now):
        try:
            return (now - _dt.datetime.fromisoformat(at)).total_seconds() / 86400
        except (TypeError, ValueError):
            return None

    def get(self, kind, key, now, max_days=None):
        """The saved value, or None when missing or older than the limit."""
        e = self.d[kind].get(key)
        if not isinstance(e, dict) or "v" not in e:
            return None
        age = self._age_days(e.get("at"), now)
        limit = CACHE_TTL_DAYS[kind] if max_days is None else max_days
        return e["v"] if age is not None and age <= limit else None

    def put(self, kind, key, value, now):
        self.d[kind][key] = {"at": now.isoformat(timespec="seconds"), "v": value}
        self.dirty = True

    def fresh_keys(self, kind, now, max_days=None) -> list[str]:
        return [k for k in self.d[kind]
                if self.get(kind, k, now, max_days) is not None]

    def counts(self) -> dict:
        return {k: len(v) for k, v in self.d.items()}

    def save(self, now) -> None:
        for kind, entries in self.d.items():
            for key in [k for k, e in entries.items()
                        if (self._age_days(e.get("at"), now) or 0) > CACHE_KEEP_DAYS]:
                del entries[key]
                self.dirty = True
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, ensure_ascii=False,
                                  separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.path)
        self.dirty = False


# ---- planning ------------------------------------------------------------------

def clean_queries(text_or_list, limit: int = MAX_QUERIES) -> list[str]:
    """One phrase per line (or a list), trimmed, de-duplicated, capped."""
    raw = (text_or_list.splitlines() if isinstance(text_or_list, str)
           else list(text_or_list or []))
    out, seen = [], set()
    for line in raw:
        q = " ".join(str(line).split())[:80]
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out[:limit]


def expand_queries(seeds: list[str], suggest, per_seed: int = 3,
                   limit: int = MAX_QUERIES) -> list[str]:
    """The seeds plus the autocomplete phrases people really type that start
    with them (free - no API units). `suggest(phrase) -> list[str]`."""
    out = list(clean_queries(seeds, limit))
    seen = {q.lower() for q in out}
    for seed in list(out):
        added = 0
        for s in suggest(seed) or []:
            s = " ".join(str(s).split())
            if s and s.lower() not in seen and s.lower() != seed.lower():
                seen.add(s.lower())
                out.append(s)
                added += 1
                if added >= per_seed or len(out) >= limit:
                    break
        if len(out) >= limit:
            break
    return out[:limit]


def estimate_units(n_queries: int, pages: int, check_limit: int) -> int:
    """Worst-case API units for one run (what the page shows before you click)."""
    search = n_queries * pages * 100
    overview = math.ceil(n_queries * pages * 50 / 50)    # <=50 new channels a page
    checks = check_limit * CHECK_UNITS
    return int((search + overview + checks) * 1.05)


# ---- stage A: search --------------------------------------------------------------

def _published_after(now, days: int) -> str:
    return (now - _dt.timedelta(days=int(days))).strftime("%Y-%m-%dT%H:%M:%SZ")


def search_candidates(client, queries, days, pages, now, skip, log=None,
                      cancel=lambda: False, cache=None) -> dict[str, dict]:
    """{channel_id: {name, hits:[{video_id,title,query}], queries:[...]}} for
    channels (not in `skip`) with a video for a query published in the window,
    most-viewed first. A search already saved (same phrase and window, under a
    day old) is reused without calling the API."""
    found: dict[str, dict] = {}
    for q in queries:
        token = None
        for page in range(max(1, int(pages))):
            if cancel():
                return found
            key = f"{q.lower()}|{int(days)}|{page}"
            page_data = cache.get("search", key, now) if cache else None
            saved = page_data is not None
            if not saved:
                data = client.call(
                    "search", part="snippet", q=q, type="video",
                    order="viewCount", publishedAfter=_published_after(now, days),
                    maxResults=50, relevanceLanguage="en", pageToken=token)
                page_data = {"next": data.get("nextPageToken"), "items": [
                    {"cid": (it.get("snippet") or {}).get("channelId"),
                     "vid": (it.get("id") or {}).get("videoId"),
                     "title": (it.get("snippet") or {}).get("title", ""),
                     "name": (it.get("snippet") or {}).get("channelTitle", "")}
                    for it in data.get("items", [])]}
                if cache:
                    cache.put("search", key, page_data, now)
            n = 0
            for it in page_data["items"]:
                cid, vid = it.get("cid"), it.get("vid")
                if not cid or not vid or cid in skip:
                    continue
                c = found.setdefault(cid, {"channel_id": cid, "hits": [],
                                           "queries": [],
                                           "name": it.get("name", "")})
                if q not in c["queries"]:
                    c["queries"].append(q)
                c["hits"].append({"video_id": vid, "query": q,
                                  "title": it.get("title", "")})
                n += 1
            if log:
                log(f"search '{q}': {n} videos from channels not watched"
                    + (" (saved, no API units)" if saved else ""))
            token = page_data.get("next")
            if not token:
                break
    return found


# ---- stage B: channel overview -------------------------------------------------------

def channel_overview(client, ids: list[str], cache=None, now=None
                     ) -> dict[str, dict]:
    """Subscribers, size, creation date, language and upload playlist of each
    channel (1 unit per 50; saved ones are free). Hidden subscriber counts
    stay None, not 0."""
    out: dict[str, dict] = {}
    ids = [i for i in dict.fromkeys(ids) if i]
    if cache is not None:
        for cid in list(ids):
            saved = cache.get("overview", cid, now)
            if saved is not None:
                out[cid] = saved
        ids = [i for i in ids if i not in out]
    for n in range(0, len(ids), 50):
        data = client.call("channels", part="snippet,statistics,contentDetails",
                           id=",".join(ids[n:n + 50]), maxResults=50)
        for item in data.get("items", []):
            st = item.get("statistics") or {}
            sn = item.get("snippet") or {}

            def _int(key, _st=st):
                v = _st.get(key)
                try:
                    return int(v) if v is not None else None
                except (TypeError, ValueError):
                    return None

            out[item.get("id")] = {
                "channel_id": item.get("id"),
                "name": sn.get("title", ""),
                "about": (sn.get("description") or "")[:200],
                "created_at": sn.get("publishedAt"),
                "default_language": sn.get("defaultLanguage") or "",
                "country": sn.get("country") or "",
                "subscribers": None if st.get("hiddenSubscriberCount")
                else _int("subscriberCount"),
                "videos": _int("videoCount") or 0,
                "views": _int("viewCount"),
            }
            if cache is not None and out.get(item.get("id")):
                cache.put("overview", item.get("id"), out[item.get("id")], now)
    return out


def _lang_ok(ch: dict, titles: list[str]) -> bool:
    """True unless the channel is evidently not English. Uses the app's own
    conservative text check when available."""
    declared = (ch.get("default_language") or "").lower().split("-")[0]
    if declared:
        return declared == "en"
    try:
        from .channel_discovery import language_info
        code, _ = language_info({"default_language": "", "about": ch.get("about", ""),
                                 "sample": [{"title": t} for t in titles]})
        return code in ("en", "unknown")
    except Exception:  # noqa: BLE001 - no flask etc.: do not guess
        return True


def _eligible(ov: dict, titles: list[str], opts: dict) -> bool:
    """Small enough, enough uploads to have a norm, and English."""
    subs = ov.get("subscribers")
    if subs is not None and subs > opts["max_subs"]:
        return False
    if ov.get("videos", 0) < opts["min_videos"]:
        return False
    return not opts["english_only"] or _lang_ok(ov, titles)


def pick_for_check(overview: dict[str, dict], found: dict[str, dict], opts: dict,
                   checked: dict, now, cached=()) -> list[str]:
    """Small, English, big-enough-to-have-a-norm channels, best candidates
    first: more of the searched phrases hit, then smaller. Recently checked
    channels are skipped (saves quota) unless their upload history is saved -
    those are free, so they are always included and do not use up the limit."""
    ranked = []
    for cid, ov in overview.items():
        titles = [h["title"] for h in found.get(cid, {}).get("hits", [])]
        if not _eligible(ov, titles, opts):
            continue
        last = checked.get(cid)
        if last and cid not in cached:
            try:
                when = _dt.datetime.fromisoformat(last)
                if (now - when).days < RECHECK_AFTER_DAYS:
                    continue
            except ValueError:
                pass
        subs = ov.get("subscribers")
        hits = found.get(cid, {}).get("hits", [])
        ranked.append((-len(found.get(cid, {}).get("queries", [])), -len(hits),
                       subs if subs is not None else 10 ** 9, cid))
    ranked.sort()
    out, paid = [], 0
    for r in ranked:
        if r[3] in cached:
            out.append(r[3])
        elif paid < max(0, int(opts["check_limit"])):
            out.append(r[3])
            paid += 1
    return out


# ---- stage C: score a channel against its own norm -------------------------------------

def _smallness_points(subs) -> int:
    if subs is None:
        return 4
    for limit, pts in ((1_000, 15), (10_000, 12), (50_000, 8), (100_000, 5)):
        if subs <= limit:
            return pts
    return 2


def _freshness_points(age_days) -> int:
    if age_days is None:
        return 0
    for limit, pts in ((7, 15), (14, 12), (30, 8), (60, 4)):
        if age_days <= limit:
            return pts
    return 0


def score(rec: dict) -> tuple[int, list[str]]:
    """0-100 with the reasons, so the number is checkable. Strength of the best
    video, whether it repeats, how small the channel is, how fresh."""
    best = rec["best"]
    mult = max(best["multiplier"], 1.0)
    strength = min(40, int(round(10 * math.log2(mult))))
    extra = max(0, rec.get("outliers_last10", 1) - 1)
    repeat = min(20, 10 * extra)
    small = _smallness_points(rec.get("subscribers"))
    fresh = _freshness_points(best.get("age_days"))
    why = [f"best video {mult:.1f}x its channel's norm (+{strength})"]
    if repeat:
        why.append(f"{rec['outliers_last10']} of its last 10 long uploads beat "
                   f"the norm: repeatable, not a fluke (+{repeat})")
    else:
        why.append("only one outlier so far: could be a one-off (+0)")
    subs = rec.get("subscribers")
    why.append(("subscribers hidden" if subs is None
                else f"{subs:,} subscribers") + f" (+{small})")
    if best.get("age_days") is not None:
        why.append(f"best video is {best['age_days']:.0f} days old (+{fresh})")
    return min(100, strength + repeat + small + fresh), why


def summarize(items: list[dict], ov: dict, opts: dict, now) -> dict | None:
    """The channel's record when it has a recent outlier, else None. `items`
    are outliers.build() rows for the channel's uploads."""
    longs = [i for i in items if not i["is_short"]]
    recent = [i for i in longs if i["age_days"] is not None
              and i["age_days"] <= opts["days"] and i["views"] >= opts["min_views"]]
    if not recent:
        return None
    best = max(recent, key=lambda i: i["multiplier"])
    if best["multiplier"] < opts["min_mult"]:
        return None
    newest = sorted((i for i in longs if i["age_days"] is not None),
                    key=lambda i: i["age_days"])[:10]
    out10 = sum(1 for i in newest if i["multiplier"] >= opts["min_mult"])
    span = (newest[-1]["age_days"] - newest[0]["age_days"]) if len(newest) >= 2 else 0
    per_week = round(len(newest) / (span / 7.0), 1) if span > 0 else None
    created = None
    if ov.get("created_at"):
        try:
            created = (now - _dt.datetime.fromisoformat(
                ov["created_at"].replace("Z", "+00:00"))).days
        except ValueError:
            created = None
    rec = {
        "channel_id": ov["channel_id"], "name": ov.get("name", ""),
        "url": f"https://www.youtube.com/channel/{ov['channel_id']}",
        "about": ov.get("about", ""), "subscribers": ov.get("subscribers"),
        "videos": ov.get("videos"), "channel_age_days": created,
        "new_channel": created is not None and created <= 365,
        "country": ov.get("country", ""),
        "uploads_per_week": per_week,
        "outliers_recent": sum(1 for i in recent if i["multiplier"] >= opts["min_mult"]),
        "outliers_last10": out10,
        "norm_views": round(best["baseline"]),
        "best": {
            "video_id": best["video_id"], "title": best["title"],
            "views": best["views"], "multiplier": round(best["multiplier"], 2),
            "age_days": round(best["age_days"], 1),
            "vph": round(best["vph"], 1) if best.get("vph") is not None else None,
            "duration": best["duration"], "url": best["url"],
            "thumb": best["thumb"],
            "views_per_sub": (round(best["views"] / ov["subscribers"], 1)
                              if ov.get("subscribers") else None),
        },
    }
    rec["score"], rec["why"] = score(rec)
    return rec


def evaluate(ov: dict, uploads: list[dict], opts: dict, now) -> dict | None:
    """Score a channel from its (fetched or saved) uploads."""
    rows = [{"video_id": u["video_id"], "channel_id": ov["channel_id"],
             "channel_name": ov.get("name", ""), "genre": "",
             "title": u["title"],
             "url": f"https://www.youtube.com/watch?v={u['video_id']}",
             "published_at": u["published_at"], "view_count": u["view_count"],
             "duration": u["duration"], "status": ""} for u in uploads]
    return summarize(outliers.build(rows, now=now), ov, opts, now)


def check_channel(client, ov: dict, opts: dict, now, cache=None) -> dict | None:
    """Fetch the newest uploads (or reuse the saved ones) and score them.
    None = no recent outlier."""
    uploads = cache.get("uploads", ov["channel_id"], now) if cache else None
    if uploads is None:
        uploads = youtube_api.channel_uploads(client, ov["channel_id"], limit=50)
        if cache:
            cache.put("uploads", ov["channel_id"], uploads, now)
    return evaluate(ov, uploads, opts, now)


# ---- the run -------------------------------------------------------------------------------

def _query_stats(queries, found, overview, picked, results, opts) -> dict:
    """Per phrase: how many channels turned up, how many small ones were
    checked and how many of those had a recent outlier - an honest read of
    "how hard is it for a small channel here"."""
    out = {}
    for q in queries:
        chans = [cid for cid, c in found.items() if q in c["queries"]]
        small = [cid for cid in chans if cid in overview and (
            overview[cid].get("subscribers") is None
            or overview[cid]["subscribers"] <= opts["max_subs"])]
        checked = [cid for cid in chans if cid in picked]
        winners = [cid for cid in checked if cid in results]
        out[q] = {"channels": len(chans), "small": len(small),
                  "checked": len(checked), "exploding": len(winners),
                  "rate": round(len(winners) / len(checked), 2) if checked else None}
    return out


def _stamp(now) -> str:
    return now.isoformat(timespec="seconds")


def _process(data, client, found, opts, log, cancel, now, cache, state) -> None:
    """Stages B and C for the candidates in `found`; fills `state` as it goes
    so a quota stop part-way still reports what was done."""
    overview = channel_overview(client, list(found), cache, now)
    state["overview"].update(overview)
    saved = set(cache.fresh_keys("uploads", now)) if cache else set()
    picked = pick_for_check(overview, found, opts, data["checked"], now, saved)
    state["picked"].extend(picked)
    log(f"{len(picked)} small channels to check (of {len(overview)})")
    for n, cid in enumerate(picked, 1):
        if cancel():
            state["stopped"] = "cancelled"
            return
        try:
            rec = check_channel(client, overview[cid], opts, now, cache)
        except youtube_api.QuotaGuard:
            raise
        except youtube_api.ApiError as exc:
            log(f"skipped {overview[cid].get('name') or cid}: {exc}")
            continue
        data["checked"][cid] = _stamp(now)
        state["done"].append(cid)
        if rec:
            old = data["channels"].get(cid, {})
            queries = list(old.get("queries", []))
            queries += [q for q in found.get(cid, {}).get("queries", [])
                        if q not in queries]
            rec["queries"] = queries
            rec["first_seen"] = old.get("first_seen") or _stamp(now)
            rec["last_seen"] = _stamp(now)
            data["channels"][cid] = rec
            state["results"][cid] = rec
            log(f"[{n}/{len(picked)}] {rec['name']}: "
                f"{rec['best']['multiplier']}x, {rec['subscribers']} subs")


def _new_state() -> dict:
    return {"overview": {}, "picked": [], "done": [], "results": {},
            "stopped": ""}


def _finish(cfg, data, cache, now, kind, labels, opts, before, found, state,
            extra=None) -> dict:
    units = youtube_api.quota_used(cfg) - before
    data["runs"].append({
        "kind": kind, "at": _stamp(now), "queries": labels, "options": opts,
        "units": units, "candidates": len(found), "checked": len(state["done"]),
        "exploding": len(state["results"]), "stopped": state["stopped"],
        "per_query": _query_stats(labels, found, state["overview"],
                                  set(state["picked"]), state["results"], opts),
        **(extra or {})})
    save(cfg, data)
    cache.save(now)
    return {"units": units, "candidates": len(found), "checked": len(state["done"]),
            "exploding": len(state["results"]), "stopped": state["stopped"]}


def run(cfg, client, queries, opts=None, watched=(), log=lambda s: None,
        cancel=lambda: False, now=None) -> dict:
    """One discovery run. Saves what it found even when the quota guard or a
    cancel stops it early. Returns a summary."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    opts = {**DEFAULTS, **(opts or {})}
    queries = clean_queries(queries)
    if not queries:
        raise youtube_api.ApiError("Enter at least one topic phrase.")
    data, cache = load(cfg), Cache(cfg)
    skip = set(watched) | set(data["dismissed"])
    before = youtube_api.quota_used(cfg)
    found, state = {}, _new_state()
    try:
        found = search_candidates(client, queries, opts["days"], opts["pages"],
                                  now, skip, log, cancel, cache)
        log(f"{len(found)} channels found; reading their sizes")
        _process(data, client, found, opts, log, cancel, now, cache, state)
    except youtube_api.QuotaGuard as exc:
        state["stopped"] = "quota"
        log(str(exc))
    return _finish(cfg, data, cache, now, "search", queries, opts, before,
                   found, state)


# ---- free re-judging of saved data -----------------------------------------------------------

def rescore(cfg, opts=None, watched=(), now=None) -> dict:
    """Judge every saved upload history again with the given thresholds. Costs
    no API units: use it to loosen or tighten the filters without searching."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    opts = {**DEFAULTS, **(opts or {})}
    data, cache = load(cfg), Cache(cfg)
    skip = set(watched) | set(data["dismissed"])
    seen = shown = 0
    for cid in cache.fresh_keys("uploads", now, RESCORE_MAX_DAYS):
        ov = cache.get("overview", cid, now, CACHE_KEEP_DAYS)
        if cid in skip or not ov:
            continue
        uploads = cache.get("uploads", cid, now, RESCORE_MAX_DAYS)
        seen += 1
        rec = (evaluate(ov, uploads, opts, now)
               if _eligible(ov, [u["title"] for u in uploads[:10]], opts) else None)
        old = data["channels"].get(cid)
        if rec:
            rec["queries"] = (old or {}).get("queries", [])
            rec["first_seen"] = (old or {}).get("first_seen") or _stamp(now)
            rec["last_seen"] = _stamp(now)
            data["channels"][cid] = rec
            shown += 1
        else:
            data["channels"].pop(cid, None)
    data["runs"].append({"kind": "rescore", "at": _stamp(now), "queries": [],
                         "options": opts, "units": 0, "candidates": seen,
                         "checked": seen, "exploding": shown, "stopped": "",
                         "per_query": {}})
    save(cfg, data)
    return {"units": 0, "channels": seen, "exploding": shown}


# ---- snowball ---------------------------------------------------------------------------------

def featured_channels(client, ids: list[str], cache=None, now=None
                      ) -> dict[str, list[str]]:
    """{channel_id: [channel ids it features on its page]} (1 unit per 50;
    saved ones are free). Many small channels feature none."""
    out: dict[str, list[str]] = {}
    todo = []
    for cid in dict.fromkeys(ids):
        saved = cache.get("featured", cid, now) if cache else None
        if saved is None:
            todo.append(cid)
        else:
            out[cid] = saved
    for n in range(0, len(todo), 50):
        batch = todo[n:n + 50]
        data = client.call("channels", part="brandingSettings",
                           id=",".join(batch), maxResults=50)
        got = {}
        for item in data.get("items", []):
            branding = (item.get("brandingSettings") or {}).get("channel") or {}
            got[item.get("id")] = [
                x for x in (branding.get("featuredChannelsUrls") or [])
                if isinstance(x, str) and x.startswith("UC")]
        for cid in batch:
            out[cid] = got.get(cid, [])
            if cache:
                cache.put("featured", cid, out[cid], now)
    return out


def phrase_from_title(title: str, words: int = 8) -> str:
    """A search phrase from a video title: punctuation and symbols dropped,
    the first few words kept."""
    cleaned = re.sub(r"[^\w\s'-]", " ", title or "")
    return " ".join(cleaned.split()[:words])[:80]


def seeds_for(data: dict, skip=(), sopts=None) -> list[dict]:
    """The strongest saved channels not expanded from yet, best first."""
    so = {**SNOWBALL, **(sopts or {})}
    done, skip = data.get("snowballed", {}), set(skip) | set(data.get("dismissed", []))
    seeds = [c for c in data.get("channels", {}).values()
             if c["score"] >= so["min_seed_score"]
             and c["channel_id"] not in done and c["channel_id"] not in skip]
    seeds.sort(key=lambda c: -c["score"])
    return seeds[:max(0, int(so["max_seeds"]))]


def estimate_snowball(n_seeds: int, rounds: int, phrases: int,
                      check_limit: int) -> int:
    """Worst-case API units (saved data makes the real cost lower)."""
    n_seeds = max(n_seeds, 1)
    per_round = (math.ceil(n_seeds / 50) + phrases * 100
                 + math.ceil((n_seeds * 20 + phrases * 50) / 50)
                 + check_limit * CHECK_UNITS)
    return int(per_round * max(1, int(rounds)) * 1.05)


def _merge_found(dst: dict, src: dict) -> None:
    for cid, c in src.items():
        d = dst.setdefault(cid, {"channel_id": cid, "hits": [], "queries": [],
                                 "name": c.get("name", "")})
        d["hits"] += [h for h in c.get("hits", []) if h not in d["hits"]]
        d["queries"] += [q for q in c.get("queries", []) if q not in d["queries"]]
        d["name"] = d["name"] or c.get("name", "")


def snowball(cfg, client, opts=None, sopts=None, watched=(),
             log=lambda s: None, cancel=lambda: False, now=None) -> dict:
    """Expand from the strongest saved channels: the channels they feature and
    the channels behind videos like their best one. Each round's new strong
    finds seed the next. Saves partial progress on a quota stop or cancel."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    opts = {**DEFAULTS, **(opts or {})}
    so = {**SNOWBALL, **(sopts or {})}
    data, cache = load(cfg), Cache(cfg)
    skip = set(watched) | set(data["dismissed"])
    before = youtube_api.quota_used(cfg)
    all_found, labels, rounds, state = {}, [], [], _new_state()
    try:
        for rnd in range(1, max(1, int(so["rounds"])) + 1):
            if cancel():
                state["stopped"] = "cancelled"
                break
            seeds = seeds_for(data, skip, so)
            if not seeds:
                log("no strong channels left to expand from")
                break
            log(f"round {rnd}: expanding from {len(seeds)} strong channel(s)")
            found: dict[str, dict] = {}
            featured = featured_channels(
                client, [s["channel_id"] for s in seeds], cache, now)
            for s in seeds:
                label = f"featured by {s['name']}"
                for fid in featured.get(s["channel_id"], []):
                    if fid in skip or fid == s["channel_id"]:
                        continue
                    c = found.setdefault(fid, {"channel_id": fid, "hits": [],
                                               "queries": [], "name": ""})
                    if label not in c["queries"]:
                        c["queries"].append(label)
            for s in seeds[:max(0, int(so["phrases"]))]:
                if cancel():
                    break
                phrase = phrase_from_title(s["best"]["title"])
                if not phrase:
                    continue
                got = search_candidates(
                    client, [phrase], opts["days"], 1, now,
                    skip | {s["channel_id"]}, log, cancel, cache)
                for c in got.values():
                    c["queries"] = [f"like: {phrase}"]
                _merge_found(found, got)
            log(f"{len(found)} candidate channels from the seeds")
            new_before = set(data["channels"])
            _merge_found(all_found, found)
            labels += [q for c in found.values() for q in c["queries"]
                       if q not in labels]
            _process(data, client, found, opts, log, cancel, now, cache, state)
            if state["stopped"]:
                break
            for s in seeds:
                data["snowballed"][s["channel_id"]] = _stamp(now)
            rounds.append({
                "seeds": [s["name"] for s in seeds], "candidates": len(found),
                "new": len(set(state["results"]) - new_before)})
    except youtube_api.QuotaGuard as exc:
        state["stopped"] = "quota"
        log(str(exc))
    out = _finish(cfg, data, cache, now, "snowball", labels, opts, before,
                  all_found, state, {"rounds": rounds})
    out["rounds"] = len(rounds)
    return out


# ---- reading the saved result ----------------------------------------------------------------

SORTS = ("score", "multiplier", "subs", "fresh", "views")


def ranked(data: dict, *, min_score: int = 0, max_subs: int | None = None,
           min_mult: float = 0.0, sort: str = "score", watched=(),
           new_only: bool = False) -> list[dict]:
    """The saved channels, filtered and sorted for the page."""
    rows = [c for c in data.get("channels", {}).values()
            if c["channel_id"] not in set(watched)
            and c["channel_id"] not in set(data.get("dismissed", []))
            and c["score"] >= min_score
            and c["best"]["multiplier"] >= min_mult
            and (max_subs is None or c["subscribers"] is None
                 or c["subscribers"] <= max_subs)
            and (not new_only or c.get("new_channel"))]
    keys = {
        "score": lambda c: -c["score"],
        "multiplier": lambda c: -c["best"]["multiplier"],
        "subs": lambda c: (c["subscribers"] is None, c["subscribers"] or 0),
        "fresh": lambda c: c["best"]["age_days"],
        "views": lambda c: -c["best"]["views"],
    }
    rows.sort(key=keys.get(sort, keys["score"]))
    return rows
