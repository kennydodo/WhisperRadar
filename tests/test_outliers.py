"""Outlier research: views vs the channel's own norm, and the Research page."""
import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import db, outliers  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

NOW = dt.datetime(2026, 10, 7, tzinfo=dt.timezone.utc)


def row(vid, views, ch="C1", days_ago=None, dur=600, title=None, status="new"):
    when = (NOW - dt.timedelta(days=days_ago)).isoformat() \
        if days_ago is not None else None
    return {"video_id": vid, "channel_id": ch, "channel_name": ch,
            "genre": "g", "title": title or vid, "url": "u/" + vid,
            "published_at": when, "view_count": views, "duration": dur,
            "status": status}


class ScoreTests(unittest.TestCase):
    def _channel(self, n=10, base=1000):
        return [row(f"v{i}", base) for i in range(n)]

    def test_multiplier_is_views_over_the_median_of_the_others(self):
        rows = self._channel() + [row("hit", 10000)]
        by = {i["video_id"]: i for i in outliers.build(rows, NOW)}
        self.assertAlmostEqual(by["hit"]["multiplier"], 10.0)
        self.assertAlmostEqual(by["v0"]["multiplier"], 1.0)

    def test_one_viral_video_does_not_raise_the_norm(self):
        rows = self._channel(9) + [row("hit", 1_000_000), row("big", 8000)]
        by = {i["video_id"]: i for i in outliers.build(rows, NOW)}
        self.assertAlmostEqual(by["big"]["multiplier"], 8.0)   # median, not mean

    def test_too_little_context_gives_no_score(self):
        rows = [row(f"v{i}", 1000) for i in range(4)]
        self.assertEqual(outliers.build(rows, NOW), [])

    def test_videos_without_views_are_skipped(self):
        rows = self._channel() + [row("none", None)]
        ids = {i["video_id"] for i in outliers.build(rows, NOW)}
        self.assertNotIn("none", ids)

    def test_channels_are_scored_separately(self):
        rows = ([row(f"x{i}", 100, ch="C1") for i in range(8)]
                + [row("a", 1000, ch="C1")]
                + [row(f"y{i}", 5000, ch="C2") for i in range(8)]
                + [row("b", 5000, ch="C2")])
        by = {i["video_id"]: i for i in outliers.build(rows, NOW)}
        self.assertAlmostEqual(by["a"]["multiplier"], 10.0)   # vs C1's 100
        self.assertAlmostEqual(by["b"]["multiplier"], 1.0)    # vs C2's 5000

    def test_dated_videos_use_the_previous_uploads_as_the_norm(self):
        # a channel that grew: old videos ~100 views, recent ones ~1000
        old = [row(f"o{i}", 100, days_ago=400 + i) for i in range(8)]
        new = [row(f"n{i}", 1000, days_ago=10 + i) for i in range(8)]
        hit = row("hit", 1100, days_ago=1)
        by = {i["video_id"]: i
              for i in outliers.build(old + new + [hit], NOW, window=8)}
        self.assertEqual(by["hit"]["baseline_how"], "previous uploads")
        self.assertAlmostEqual(by["hit"]["multiplier"], 1.1)

    def test_age_and_views_per_day(self):
        rows = self._channel() + [row("hit", 10000, days_ago=10)]
        hit = {i["video_id"]: i for i in outliers.build(rows, NOW)}["hit"]
        self.assertAlmostEqual(hit["age_days"], 10.0)
        self.assertAlmostEqual(hit["vpd"], 1000.0)

    def test_views_per_hour_and_breakout(self):
        rows = self._channel() + [row("new", 9000, days_ago=2),
                                  row("old", 9000, days_ago=100)]
        by = {i["video_id"]: i for i in outliers.build(rows, NOW)}
        self.assertAlmostEqual(by["new"]["vph"], 9000 / 48.0)
        self.assertTrue(by["new"]["breakout"])
        self.assertFalse(by["old"]["breakout"])
        snaps = {"new": [(NOW - dt.timedelta(days=2), 4000), (NOW, 9000)]}
        got = {i["video_id"]: i for i in outliers.build(rows, NOW,
                                                         snapshots=snaps)}
        self.assertAlmostEqual(got["new"]["vph"], got["new"]["momentum"] / 24)

    def test_sort_by_vph_and_breakout(self):
        rows = self._channel() + [row("a", 9000, days_ago=2),
                                  row("b", 12000, days_ago=60)]
        items = outliers.build(rows, NOW)
        got = [i["video_id"] for i in outliers.filter_sort(
            items, sort="vph")[0]]
        self.assertEqual(got[0], "a")
        got = [i["video_id"] for i in outliers.filter_sort(
            items, sort="breakout")[0]]
        self.assertEqual(got[0], "a")

    def test_shorts_are_flagged_by_duration(self):
        rows = self._channel() + [row("s", 9000, dur=45), row("l", 9000, dur=600),
                                  row("u", 9000, dur=None)]
        by = {i["video_id"]: i for i in outliers.build(rows, NOW)}
        self.assertTrue(by["s"]["is_short"])
        self.assertFalse(by["l"]["is_short"])
        self.assertFalse(by["u"]["is_short"])


class FilterTests(unittest.TestCase):
    def setUp(self):
        rows = [row(f"v{i}", 1000) for i in range(10)]
        rows += [row("big", 20000, days_ago=5, title="Why cats purr"),
                 row("mid", 6000, days_ago=200),
                 row("undated", 12000),
                 row("short", 9000, dur=30, days_ago=3)]
        self.items = outliers.build(rows, NOW)

    def ids(self, **kw):
        return [i["video_id"] for i in outliers.filter_sort(self.items, **kw)[0]]

    def test_default_sorts_by_multiplier_and_hides_shorts(self):
        got = self.ids()
        self.assertEqual(got[0], "big")
        self.assertNotIn("short", got)
        self.assertNotIn("v0", got)                  # 1x < 3x

    def test_age_window_drops_old_and_undated(self):
        self.assertEqual(self.ids(max_age_days=30), ["big"])

    def test_min_multiplier(self):
        self.assertEqual(self.ids(min_multiplier=10), ["big", "undated"])

    def test_text_filter_and_sort_by_views(self):
        self.assertEqual(self.ids(q="CATS"), ["big"])
        self.assertEqual(self.ids(sort="views")[:2], ["big", "undated"])

    def test_limit_reports_the_full_match_count(self):
        shown, matched = outliers.filter_sort(self.items, limit=1)
        self.assertEqual((len(shown), matched), (1, 3))


class NicheChipTests(unittest.TestCase):
    def test_genre_chips_then_generic_without_repeats(self):
        from whisperradar import niche_chips
        chips = niche_chips.for_genre("Finance", limit=50)
        self.assertIn("passive income", chips)
        self.assertIn("mistakes", chips)
        self.assertEqual(len(chips), len(set(chips)))
        self.assertEqual(niche_chips.for_genre("", limit=3),
                         niche_chips.GENERIC[:3])


class FormatTests(unittest.TestCase):
    def test_views_and_age(self):
        self.assertEqual(outliers.fmt_views(1_500_000), "1.5M")
        self.assertEqual(outliers.fmt_views(42_000), "42K")
        self.assertEqual(outliers.fmt_views(900), "900")
        self.assertEqual(outliers.fmt_age(None), "?")
        self.assertEqual(outliers.fmt_age(12), "12d")
        self.assertEqual(outliers.fmt_age(200), "6mo")


class ResearchPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        db.add_channel(self.conn, "Chan One", "UC1", genre="animals")
        vids = [{"video_id": f"v{i}", "title": f"Video {i}",
                 "url": f"https://youtu.be/v{i}", "view_count": 1000}
                for i in range(8)]
        vids.append({"video_id": "hit", "title": "The big one",
                     "url": "https://youtu.be/hit", "view_count": 50000})
        db.upsert_videos(self.conn, "UC1", vids)
        self.own = db.create_own_channel(self.conn, "My Channel")
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def test_page_lists_the_outlier_with_a_make_this_button(self):
        r = self.client.get("/research")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("The big one", html)
        self.assertIn("50.0x", html)
        self.assertIn('name="source_video_id" value="hit"', html)
        self.assertIn("Make this", html)
        self.assertNotIn("Video 3", html)           # 1x is not an outlier

    def test_filters_reach_the_page(self):
        html = self.client.get("/research?mult=100").get_data(as_text=True)
        self.assertIn("No outliers match", html)
        html = self.client.get("/research?q=big").get_data(as_text=True)
        self.assertIn("The big one", html)

    def test_bad_filter_values_do_not_break_the_page(self):
        r = self.client.get("/research?mult=abc&age=x&sort=zzz")
        self.assertEqual(r.status_code, 200)

    def test_make_this_starts_a_production_from_the_video(self):
        r = self.client.post("/studio/new", data={
            "title": "The big one", "source_video_id": "hit",
            "genre": "animals", "own_channel_id": str(self.own)})
        self.assertEqual(r.status_code, 302)
        self.assertRegex(r.headers["Location"], r"/studio/\d+")

    def test_nav_has_the_research_link_everywhere(self):
        for path in ("/", "/watched", "/studio", "/finished", "/my-channels",
                     "/settings"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn('href="/research"', html, path)


if __name__ == "__main__":
    unittest.main()
