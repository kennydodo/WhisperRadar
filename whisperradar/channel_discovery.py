"""User-triggered topic discovery seeded by a watched channel's titles."""
import json
from collections import Counter
from pathlib import Path
from urllib.parse import quote, urlencode

from flask import Blueprint, abort, render_template, request

from . import db, insights, youtube_api

DISCOVERY_STOP = insights.STOP | set("""things never people everyone always really
secrets secret know knowing learned learn reasons reason instead items americans
women men many still use used using daily every good replaced don't buy keep
stop make makes get gets""".split())


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
        if request.method == "POST":
            query = (request.form.get("q") or "").strip()[:200]
            if not query:
                error = "Enter a topic to search."
            else:
                try:
                    found = rank_matches(youtube_api.discover_channels(
                        youtube_api.Client(cfg), query, watched), query)
                    previous = {"query": query, "channels": found}
                    searches[channel_id] = previous
                    cache.write_text(json.dumps(searches, ensure_ascii=False), encoding="utf-8")
                except youtube_api.ApiError as exc:
                    error = str(exc)
        matches = [c for c in previous.get("channels", []) if c["channel_id"] not in watched]
        return render_template("similar_channels.html", channel=channel,
                               titles=titles[:8], query=query, matches=matches,
                               searched=bool(previous), error=error,
                               youtube_search="https://www.youtube.com/results?" + urlencode(
                                   {"search_query": query}),
                               return_url="/watched/" + quote(channel_id, safe="") + "/similar")

    app.register_blueprint(routes)
