"""Exploding small channels: scoring, filtering, quota guard, persistence.
No network: a fake HTTP layer answers the YouTube Data API calls."""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import exploding, youtube_api  # noqa: E402

NOW = dt.datetime(2026, 10, 29, 12, 0, tzinfo=dt.timezone.utc)


def iso(days_ago):
    return (NOW - dt.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def uploads(prefix, recent):
    """12 older uploads at ~1000 views, then `recent` = [(days_ago, views, secs)]."""
    out = [(f"{prefix}o{i}", 20 + 6 * i, 900 + 20 * i, 600) for i in range(12)]
    out += [(f"{prefix}r{i}", d, v, s) for i, (d, v, s) in enumerate(recent)]
    return out


class Cfg:
    def __init__(self, tmp):
        self.db_path = str(Path(tmp) / "wr.db")


class FakeYouTube:
    """channels: {cid: dict(name, subs, lang, videos, uploads=[(vid, days_ago, views, secs)])}"""

    def __init__(self, channels, hits, featured=None):
        self.ch, self.hits = channels, hits   # hits: {query: [(channel_id, video_id)]}
        self.featured = featured or {}        # {channel_id: [featured channel ids]}
        self.calls = []

    def __call__(self, url):
        parsed = urllib.parse.urlparse(url)
        res = parsed.path.rsplit("/", 1)[-1]
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        self.calls.append(res)
        if res == "search":
            items = []
            for cid, vid in self.hits.get(q["q"], []):
                items.append({"id": {"videoId": vid},
                              "snippet": {"channelId": cid, "title": f"title {vid}",
                                          "channelTitle": self.ch[cid]["name"]}})
            return {"items": items}
        if res == "channels" and q.get("part") == "brandingSettings":
            return {"items": [{"id": cid, "brandingSettings": {"channel": {
                "featuredChannelsUrls": self.featured.get(cid, [])}}}
                for cid in q["id"].split(",")]}
        if res == "channels":
            items = []
            for cid in q["id"].split(","):
                c = self.ch.get(cid)
                if not c:
                    continue
                st = {"videoCount": str(len(c["uploads"])), "viewCount": "1"}
                if c["subs"] is None:
                    st["hiddenSubscriberCount"] = True
                else:
                    st["subscriberCount"] = str(c["subs"])
                items.append({"id": cid, "snippet": {
                    "title": c["name"], "description": "about",
                    "publishedAt": iso(c.get("age", 200)),
                    "defaultLanguage": c.get("lang", "en")},
                    "statistics": st,
                    "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid[2:]}}})
            return {"items": items}
        if res == "playlistItems":
            cid = "UC" + q["playlistId"][2:]
            ups = sorted(self.ch[cid]["uploads"], key=lambda u: u[1])   # newest first
            return {"items": [{"contentDetails": {"videoId": u[0]}} for u in ups]}
        if res == "videos":
            by = {}
            for cid, c in self.ch.items():
                for vid, days, views, secs in c["uploads"]:
                    by[vid] = (cid, days, views, secs)
            items = []
            for vid in q["id"].split(","):
                if vid in by:
                    cid, days, views, secs = by[vid]
                    items.append({"id": vid, "snippet": {
                        "title": f"title {vid}", "channelId": cid, "publishedAt": iso(days)},
                        "contentDetails": {"duration": f"PT{secs // 60}M{secs % 60}S"},
                        "statistics": {"viewCount": str(views)}})
            return {"items": items}
        raise AssertionError("unexpected call " + url)


def world():
    chans = {
        "UCsmall": {"name": "Small Boomer", "subs": 4000, "age": 800, "uploads": uploads("a", [(5, 9000, 600), (10, 4200, 600)])},
        "UCbig": {"name": "Big Channel", "subs": 2_000_000, "uploads": uploads("b", [(3, 80000, 600)])},
        "UCflat": {"name": "Flat", "subs": 2000, "uploads": uploads("c", [(4, 1100, 600)])},
        "UCshort": {"name": "Shorts Only Win", "subs": 3000, "uploads": uploads("d", [(2, 90000, 30)])},
        "UClow": {"name": "Tiny Views", "subs": 500, "uploads": [(f"e{i}", 20 + 6 * i, 20 + i, 600) for i in range(12)] + [("er", 3, 400, 600)]},
        "UCwatched": {"name": "Already Watched", "subs": 1000, "uploads": uploads("f", [(2, 50000, 600)])},
        "UCspanish": {"name": "Otro Canal", "subs": 1500, "lang": "es", "uploads": uploads("g", [(2, 50000, 600)])},
        "UCnew1": {"name": "Featured Newcomer", "subs": 700, "uploads": uploads("n", [(3, 9000, 600)])},
        "UCnew2": {"name": "Lookalike Newcomer", "subs": 1200, "uploads": uploads("m", [(4, 9500, 600)])},
        "UCrepeat": {"name": "Repeatable", "subs": 900, "age": 120, "uploads": uploads("h", [(2, 8000, 600), (6, 7000, 600), (9, 6500, 600)])},
    }
    hits = {"topic one": [(c, c[2:] + "x") for c in ("UCsmall", "UCbig", "UCflat", "UCshort", "UClow", "UCwatched", "UCspanish")],
            "topic two": [("UCsmall", "ax2"), ("UCrepeat", "hx")],
            "title ar0": [("UCnew2", "n2x"), ("UCsmall", "ax3")]}
    return chans, hits


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Cfg(self.tmp.name)
        self._env = os.environ.get(youtube_api.ENV_KEY)
        os.environ[youtube_api.ENV_KEY] = "K" * 30
        self.addCleanup(self._restore)
        self.chans, self.hits = world()
        self.http = FakeYouTube(self.chans, self.hits, {"UCsmall": ["UCnew1", "UCbig", "UCsmall"]})
        self.client = youtube_api.Client(self.cfg, http=self.http)

    def _restore(self):
        if self._env is None:
            os.environ.pop(youtube_api.ENV_KEY, None)
        else:
            os.environ[youtube_api.ENV_KEY] = self._env
        self.tmp.cleanup()

    def run_it(self, queries=("topic one", "topic two"), **opts):
        return exploding.run(self.cfg, self.client, list(queries), opts,
                             watched={"UCwatched"}, now=NOW)


class PlanTests(unittest.TestCase):
    def test_clean_queries_trims_dedupes_and_caps(self):
        got = exploding.clean_queries("  a  b \n\nA B\nc\n" + "\n".join(f"q{i}" for i in range(30)))
        self.assertEqual(got[:2], ["a b", "c"])
        self.assertEqual(len(got), exploding.MAX_QUERIES)

    def test_expand_adds_autocomplete_without_duplicates(self):
        sug = {"megaproject": ["megaproject", "megaproject explained", "megaproject failures", "Megaproject Explained"]}
        got = exploding.expand_queries(["megaproject"], lambda q: sug.get(q, []), per_seed=3)
        self.assertEqual(got, ["megaproject", "megaproject explained", "megaproject failures"])

    def test_estimate_is_worst_case_and_grows(self):
        a = exploding.estimate_units(4, 1, 50)
        self.assertGreaterEqual(a, 4 * 100 + 50 * 2)
        self.assertGreater(exploding.estimate_units(4, 2, 50), a)


class ScoreTests(unittest.TestCase):
    def rec(self, mult, last10, subs, age):
        return {"best": {"multiplier": mult, "age_days": age}, "outliers_last10": last10, "subscribers": subs}

    def test_more_repeats_smaller_and_fresher_all_score_higher(self):
        base, _ = exploding.score(self.rec(7, 1, 50_000, 20))
        self.assertGreater(exploding.score(self.rec(7, 3, 50_000, 20))[0], base)
        self.assertGreater(exploding.score(self.rec(7, 1, 800, 20))[0], base)
        self.assertGreater(exploding.score(self.rec(7, 1, 50_000, 3))[0], base)
        self.assertGreater(exploding.score(self.rec(20, 1, 50_000, 20))[0], base)

    def test_a_one_off_says_so_and_the_score_is_capped(self):
        pts, why = exploding.score(self.rec(3, 1, 80_000, 40))
        self.assertTrue(any("one-off" in w for w in why))
        self.assertLessEqual(exploding.score(self.rec(500, 10, 10, 1))[0], 100)

    def test_hidden_subscribers_are_not_treated_as_zero(self):
        pts, why = exploding.score(self.rec(7, 1, None, 5))
        self.assertTrue(any("hidden" in w for w in why))


class RunTests(Base):
    def test_finds_only_small_english_unwatched_channels_with_a_recent_long_outlier(self):
        res = self.run_it()
        saved = exploding.load(self.cfg)
        self.assertEqual(set(saved["channels"]), {"UCsmall", "UCrepeat"})
        self.assertEqual(res["exploding"], 2)
        small = saved["channels"]["UCsmall"]
        self.assertGreaterEqual(small["best"]["multiplier"], 7)
        self.assertEqual(small["best"]["video_id"], "ar0")
        self.assertEqual(small["queries"], ["topic one", "topic two"])

    def test_repeatable_channel_outranks_a_one_off_of_similar_size(self):
        self.run_it()
        order = [c["channel_id"] for c in exploding.ranked(exploding.load(self.cfg))]
        self.assertEqual(order[0], "UCrepeat")
        self.assertGreaterEqual(exploding.load(self.cfg)["channels"]["UCrepeat"]["outliers_last10"], 3)

    def test_filters_do_not_cost_check_calls_for_big_or_watched_channels(self):
        self.run_it()
        # only the checked channels triggered upload fetches: small, flat, short, low, repeat (not big/watched/spanish)
        self.assertEqual(self.http.calls.count("playlistItems"), 5)

    def test_quota_units_and_per_query_success_rate(self):
        res = self.run_it()
        self.assertGreater(res["units"], 200)
        run = exploding.load(self.cfg)["runs"][-1]
        q1 = run["per_query"]["topic one"]
        self.assertEqual(q1["exploding"], 1)
        self.assertEqual(q1["checked"], 4)          # small, flat, short, low
        self.assertAlmostEqual(q1["rate"], 0.25)

    def test_recently_checked_channels_are_not_checked_again(self):
        self.run_it()
        before = self.http.calls.count("playlistItems")
        self.run_it()
        self.assertEqual(self.http.calls.count("playlistItems"), before)

    def test_quota_guard_saves_a_partial_result(self):
        # allow search (100) + overview (1) + one channel check (2); the next check is refused
        old = youtube_api.DAILY_LIMIT
        youtube_api.DAILY_LIMIT = youtube_api.SAFETY_MARGIN + 100 + 1 + 2
        self.addCleanup(setattr, youtube_api, "DAILY_LIMIT", old)
        res = self.run_it(queries=["topic two"])
        self.assertEqual(res["stopped"], "quota")
        self.assertEqual(exploding.load(self.cfg)["runs"][-1]["stopped"], "quota")

    def test_cancel_stops_between_channels(self):
        res = exploding.run(self.cfg, self.client, ["topic one"], {}, watched={"UCwatched"},
                            cancel=lambda: True, now=NOW)
        self.assertEqual(res["exploding"], 0)

    def test_no_query_is_an_error_not_a_silent_run(self):
        with self.assertRaises(youtube_api.ApiError):
            exploding.run(self.cfg, self.client, ["  "], {}, now=NOW)

    def test_dismissed_channels_come_back_never(self):
        self.run_it()
        exploding.dismiss(self.cfg, "UCsmall")
        self.run_it()
        self.assertNotIn("UCsmall", exploding.load(self.cfg)["channels"])

    def test_stricter_thresholds_drop_channels(self):
        self.run_it(min_mult=8.0)
        got = set(exploding.load(self.cfg)["channels"])
        self.assertNotIn("UCrepeat", got)           # its best video is only ~8x or less


class CacheTests(Base):
    def test_the_same_search_twice_costs_no_api_calls(self):
        self.run_it()
        calls, used = len(self.http.calls), youtube_api.quota_used(self.cfg)
        res = self.run_it()
        self.assertEqual(len(self.http.calls), calls)
        self.assertEqual(youtube_api.quota_used(self.cfg), used)
        self.assertEqual(res["units"], 0)
        self.assertEqual(res["exploding"], 2)               # still judged, from saved data
        self.assertTrue((Path(self.cfg.db_path).parent / exploding.CACHE_FILE).exists())

    def test_a_new_process_reads_the_saved_data_too(self):
        self.run_it()
        calls = len(self.http.calls)
        self.client = youtube_api.Client(self.cfg, http=self.http)   # fresh objects, same files
        self.run_it()
        self.assertEqual(len(self.http.calls), calls)

    def test_saved_entries_expire_per_kind(self):
        cache = exploding.Cache(self.cfg)
        cache.put("search", "k", {"x": 1}, NOW)
        cache.put("featured", "k", ["UC1"], NOW)
        later = NOW + dt.timedelta(days=3)
        self.assertIsNone(cache.get("search", "k", later))        # 1 day
        self.assertEqual(cache.get("featured", "k", later), ["UC1"])   # 14 days
        self.assertEqual(cache.get("search", "k", later, max_days=5), {"x": 1})

    def test_rescore_rejudges_saved_histories_for_free(self):
        self.run_it()
        calls = len(self.http.calls)
        res = exploding.rescore(self.cfg, {"min_mult": 100.0}, watched={"UCwatched"}, now=NOW)
        self.assertEqual(res["exploding"], 0)
        self.assertEqual(exploding.load(self.cfg)["channels"], {})
        res = exploding.rescore(self.cfg, {"min_mult": 3.0}, watched={"UCwatched"}, now=NOW)
        self.assertEqual(set(exploding.load(self.cfg)["channels"]), {"UCsmall", "UCrepeat"})
        self.assertEqual(len(self.http.calls), calls)
        self.assertEqual(exploding.load(self.cfg)["runs"][-1]["kind"], "rescore")

    def test_rescore_respects_dismissed_and_watched(self):
        self.run_it()
        exploding.dismiss(self.cfg, "UCsmall")
        exploding.rescore(self.cfg, {"min_mult": 100.0}, watched={"UCrepeat"}, now=NOW)
        # dismissed: gone; watched: not judged at all, left as saved (the page hides it)
        self.assertEqual(set(exploding.load(self.cfg)["channels"]), {"UCrepeat"})


class SnowballTests(Base):
    def snow(self, **so):
        return exploding.snowball(self.cfg, self.client, {}, {"rounds": 1, "min_seed_score": 1, **so},
                                  watched={"UCwatched"}, now=NOW)

    def test_finds_featured_and_lookalike_channels_from_the_seeds(self):
        self.run_it()
        res = self.snow()
        saved = exploding.load(self.cfg)
        self.assertEqual(res["exploding"], 2)
        self.assertEqual(saved["channels"]["UCnew1"]["queries"], ["featured by Small Boomer"])
        self.assertEqual(saved["channels"]["UCnew2"]["queries"], ["like: title ar0"])
        self.assertEqual(set(saved["snowballed"]), {"UCsmall", "UCrepeat"})
        run = saved["runs"][-1]
        self.assertEqual(run["kind"], "snowball")
        self.assertEqual(run["per_query"]["like: title ar0"]["exploding"], 1)
        self.assertEqual(run["rounds"][0]["new"], 2)

    def test_big_or_watched_featured_channels_are_not_followed(self):
        self.run_it()
        self.snow()
        self.assertNotIn("UCbig", exploding.load(self.cfg)["channels"])
        # UCbig is featured but too big: its overview is read, its uploads are not
        self.assertEqual(self.http.calls.count("playlistItems"), 5 + 2)

    def test_new_finds_seed_the_next_round_and_seeds_are_not_reused(self):
        self.run_it()
        self.snow()
        calls = len(self.http.calls)
        res = self.snow()                       # seeds now: the two new finds, nothing else
        self.assertEqual(set(exploding.load(self.cfg)["snowballed"]),
                         {"UCsmall", "UCrepeat", "UCnew1", "UCnew2"})
        self.assertEqual(res["exploding"], 0)
        res = self.snow()                       # nothing left to expand from
        self.assertEqual(res["candidates"], 0)

    def test_two_rounds_run_in_one_call(self):
        self.run_it()
        res = self.snow(rounds=2)
        self.assertEqual(res["rounds"], 2)

    def test_second_snowball_over_the_same_seeds_is_free(self):
        self.run_it()
        self.snow()
        exploding.dismiss(self.cfg, "UCnew1")
        d = exploding.load(self.cfg)
        d["snowballed"].pop("UCsmall")          # as if it had not been expanded yet
        d["snowballed"]["UCnew2"] = "x"         # the other new find is already expanded
        exploding.save(self.cfg, d)
        calls = len(self.http.calls)
        self.snow()
        self.assertEqual(len(self.http.calls), calls)     # featured, searches, uploads all saved

    def test_quota_stop_keeps_partial_progress_and_does_not_mark_the_seeds(self):
        self.run_it()
        old = youtube_api.DAILY_LIMIT
        youtube_api.DAILY_LIMIT = youtube_api.SAFETY_MARGIN + youtube_api.quota_used(self.cfg) + 1
        self.addCleanup(setattr, youtube_api, "DAILY_LIMIT", old)
        res = self.snow()
        self.assertEqual(res["stopped"], "quota")
        self.assertEqual(exploding.load(self.cfg)["snowballed"], {})

    def test_no_seeds_is_a_clean_no_op(self):
        res = self.snow()
        self.assertEqual(res["candidates"], 0)
        self.assertEqual(self.http.calls, [])

    def test_helpers(self):
        self.assertEqual(exploding.phrase_from_title("763B AI: Running at Home! (Full Guide) 🚀 now ok yes"),
                         "763B AI Running at Home Full Guide now")
        self.assertGreater(exploding.estimate_snowball(5, 2, 3, 60), exploding.estimate_snowball(5, 1, 3, 60))
        data = {"channels": {"a": {"channel_id": "a", "score": 70}, "b": {"channel_id": "b", "score": 50},
                             "c": {"channel_id": "c", "score": 90}}, "snowballed": {"c": "x"}, "dismissed": []}
        self.assertEqual([s["channel_id"] for s in exploding.seeds_for(data)], ["a"])


class RankedTests(Base):
    def test_filters_and_sorts(self):
        self.run_it()
        data = exploding.load(self.cfg)
        self.assertEqual([c["channel_id"] for c in exploding.ranked(data, max_subs=1000)], ["UCrepeat"])
        self.assertEqual(exploding.ranked(data, sort="subs")[0]["channel_id"], "UCrepeat")
        self.assertEqual(exploding.ranked(data, watched={"UCsmall", "UCrepeat"}), [])
        self.assertEqual([c["channel_id"] for c in exploding.ranked(data, new_only=True)], ["UCrepeat"])


if __name__ == "__main__":
    unittest.main()
