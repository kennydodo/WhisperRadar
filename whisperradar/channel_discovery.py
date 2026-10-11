"""User-triggered topic discovery seeded by a watched channel's titles."""
import json
import datetime as dt
import threading
from collections import Counter
from pathlib import Path
from urllib.parse import quote, urlencode

from flask import Blueprint, abort, redirect, render_template, request

from . import db, insights, youtube_api

DISCOVERY_STOP = insights.STOP | set("""things never people everyone always really
secrets secret know knowing learned learn reasons reason instead items americans
women men many still use used using daily every good replaced don't buy keep
stop make makes get gets""".split())
_CACHE_LOCK = threading.RLock()


def language_info(channel):
    """Metadata or conservative text evidence; never claims to verify speech."""
    declared = (channel.get("default_language") or "").lower().split("-")[0]
    if declared:
        return declared, "English (channel metadata)" if declared == "en" else "Other language (channel metadata)"
    text = " ".join(v.get("title", "") for v in channel.get("sample", [])) + " " + channel.get("about", "")
    letters = [c for c in text if c.isalpha()]
    if not letters or sum(c.isascii() for c in letters) / len(letters) < .85:
        return "unknown", "Language uncertain"
    words = set(insights.tokens(text))
    # A romanized title alone can look English. Require several English
    # function words and reject common mixed-language signals.
    non_english = set("mein aapka swagat hoon mera ghar hamesha saaf hai rahega namastey aur ke ki ka".split())
    english = set("the and your this that with without how why our their these those from for you we is are to of".split())
    if len(words & non_english) >= 2:
        return "unknown", "Mixed or uncertain language"
    if len(words & english) >= 4:
        return "en", "English (text estimate)"
    return "unknown", "Language uncertain"


def merge_search(previous, query, found):
    """Keep historical snapshots and accumulate unique discovered channels."""
    history = list(previous.get("history", []))
    if previous and not history:
        history.append({"query": previous.get("query", ""), "searched_at": None,
                        "channels": previous.get("channels", [])})
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    history.append({"query": query, "searched_at": timestamp, "channels": found})
    channels = {c["channel_id"]: dict(c) for c in previous.get("channels", [])}
    for candidate in found:
        cid = candidate["channel_id"]
        old = channels.get(cid, {})
        samples = {v.get("video_id") or v.get("title"): v
                   for v in old.get("sample", []) + candidate.get("sample", [])}
        channels[cid] = {**old, **candidate, "sample": list(samples.values()),
                         "shared": sorted(set(old.get("shared", [])) |
                                          set(candidate.get("shared", [])))}
    return {"query": query, "searched_at": timestamp,
            "channels": list(channels.values()), "history": history}


def filters_from(values):
    filters = {}
    for key in ("min_subs", "max_subs", "min_videos", "max_videos"):
        raw = values.get(key, "100000" if key == "max_subs" else "")
        filters[key] = "" if raw == "" else str(max(0, int(raw)))
    filters["hidden"] = "include" if values.get("hidden") == "include" else "exclude"
    filters["language"] = "all" if values.get("language") == "all" else "en"
    for low, high in (("min_subs", "max_subs"), ("min_videos", "max_videos")):
        if filters[low] and filters[high] and int(filters[low]) > int(filters[high]):
            raise ValueError("Minimum must not exceed maximum.")
    return filters


def filter_matches(channels, filters):
    def matches(channel):
        if filters.get("language", "all") == "en" and language_info(channel)[0] != "en":
            return False
        for field, low, high in (("subscribers", "min_subs", "max_subs"),
                                 ("videos", "min_videos", "max_videos")):
            value = channel.get(field)
            if value is None:
                if field == "subscribers" and filters["hidden"] == "include":
                    continue
                return False
            if filters[low] and value < int(filters[low]):
                return False
            if filters[high] and value > int(filters[high]):
                return False
        return True
    return [c for c in channels if matches(c)]


def seed_query(titles, genre=""):
    counts = Counter()
    for title in titles:
        counts.update({{"homes": "home", "households": "home", "house": "home"}.get(w, w)
                       for w in insights.tokens(title)
                       if len(w) >= 3 and w not in DISCOVERY_STOP and not w.isdigit()})
    words = sorted(counts, key=lambda w: (-counts[w], w))[:4]
    return " ".join(words) or genre or ""


def rank_matches(found, query):
    terms = set(insights.tokens(query)) - insights.STOP
    for channel in found:
        text = " ".join(v.get("title", "") for v in channel.get("sample", []))
        shared = sorted(terms & set(insights.tokens(text)))
        channel["shared"] = shared
    return sorted(found, key=lambda c: -len(c["shared"]))


def register(app, cfg):
    routes = Blueprint("channel_discovery", __name__)

    @routes.route("/watched/<channel_id>/similar", methods=["GET", "POST"])
    def find_similar(channel_id):
        conn = db.connect(cfg.db_path)
        try:
            channel = db.get_channel(conn, channel_id)
            if channel is None:
                abort(404)
            titles = [r["title"] for r in conn.execute(
                "SELECT title FROM videos WHERE channel_id=? "
                "ORDER BY COALESCE(published_at,discovered_at) DESC LIMIT 200",
                (channel_id,))]
            watched = {c["channel_id"] for c in db.list_channels(conn)}
        finally:
            conn.close()
        cache = Path(cfg.db_path).parent / "similar_channel_searches.json"
        try:
            searches = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            searches = {}
        previous = searches.get(channel_id, {})
        query = previous.get("query") or seed_query(titles, channel["genre"])
        error = ""
        try:
            filters = filters_from(request.values)
        except ValueError:
            filters = filters_from({})
            error = "Use non-negative whole numbers; minimum must not exceed maximum."
        if request.method == "POST":
            query = (request.form.get("q") or "").strip()[:200]
            if not query:
                error = "Enter a topic to search."
            elif not error:
                try:
                    found = rank_matches(youtube_api.discover_channels(
                        youtube_api.Client(cfg), query, watched), query)
                    with _CACHE_LOCK:
                        if cache.exists():
                            searches = json.loads(cache.read_text(encoding="utf-8"))
                        previous = merge_search(searches.get(channel_id, {}), query, found)
                        searches[channel_id] = previous
                        pending = cache.with_suffix(".json.tmp")
                        pending.write_text(json.dumps(searches, ensure_ascii=False), encoding="utf-8")
                        pending.replace(cache)
                    return redirect(request.path + "?" + urlencode(filters))
                except youtube_api.ApiError as exc:
                    error = str(exc)
                except (OSError, ValueError):
                    error = "Could not save search results. Existing saved searches were retained."
        saved = [c for c in previous.get("channels", []) if c["channel_id"] not in watched]
        matches = filter_matches(saved, filters)
        matches = [dict(c, language_label=language_info(c)[1]) for c in matches]
        return render_template("similar_channels.html", channel=channel,
                               titles=titles[:8], query=query, matches=matches,
                               searched=bool(previous), error=error,
                               filters=filters, saved_count=len(saved),
                               history=previous.get("history", []) or ([{
                                   "query": previous.get("query", ""),
                                   "searched_at": previous.get("searched_at"),
                                   "channels": previous.get("channels", [])}] if previous else []),
                               searched_at=previous.get("searched_at"),
                               quota_used=youtube_api.quota_used(cfg),
                               quota_left=youtube_api.quota_left(cfg),
                               youtube_search="https://www.youtube.com/results?" + urlencode(
                                   {"search_query": query}),
                               return_url="/watched/" + quote(channel_id, safe="") + "/similar")

    app.register_blueprint(routes)
