"""Publish link, 24h/7d/28d results, the learning loop, keywords/analyze."""
import datetime as dt
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_external_prompts import Base  # noqa: E402
from whisperradar import db, insights, learning, outliers, results  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

UTC = dt.timezone.utc


def local_text(when_utc):
    return when_utc.astimezone().strftime("%Y-%m-%d %H:%M:%S")


class IdTests(unittest.TestCase):
    def test_ids_from_links(self):
        for t in ("dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ?t=5",
                  "https://www.youtube.com/watch?v=dQw4w9WgXcQ&x=1",
                  "https://youtube.com/shorts/dQw4w9WgXcQ"):
            self.assertEqual(results.parse_video_id(t), "dQw4w9WgXcQ")
        for t in ("", "nope", "https://example.com/x"):
            self.assertIsNone(results.parse_video_id(t))


class ResultTests(Base):
    def publish(self, pid, hours_ago, vid, now):
        conn = db.connect(self.cfg.db_path)
        db.update_production(conn, pid, status="published",
                             youtube_video_id=vid)
        conn.execute("UPDATE productions SET published_at = ? WHERE id = ?",
                     (local_text(now - dt.timedelta(hours=hours_ago)), pid))
        conn.commit()
        return conn

    def test_due_reads_only_the_newest_passed_horizon(self):
        now = dt.datetime.now(UTC)
        conn = self.publish(self.pid, 200, "aaaaaaaaaaa", now)
        self.assertEqual(results.due(conn, now),
                         [(self.pid, "7d", "aaaaaaaaaaa")])
        young = self.publish(self.pid, 5, "aaaaaaaaaaa", now)
        self.assertEqual(results.due(young, now), [])
        conn.close()

    def test_update_records_once_with_lateness(self):
        now = dt.datetime.now(UTC)
        conn = self.publish(self.pid, 800, "aaaaaaaaaaa", now)
        calls = []

        def fetcher(ids):
            calls.append(ids)
            return {"aaaaaaaaaaa": 4321}
        self.assertEqual(results.update(self.cfg, conn, now, fetcher), 1)
        self.assertEqual(results.update(self.cfg, conn, now, fetcher), 0)
        rep = results.report(conn, self.pid)
        self.assertEqual(rep[0]["horizon"], "28d")
        self.assertEqual(rep[0]["views"], 4321)
        self.assertFalse(rep[0]["late"])
        conn.close()

    def test_a_late_read_is_flagged(self):
        now = dt.datetime.now(UTC)
        conn = self.publish(self.pid, 24 * 20, "aaaaaaaaaaa", now)   # 20 days
        conn.execute("DELETE FROM prod_results")
        results.update(self.cfg, conn, now, lambda ids: {ids[0]: 9})
        rep = results.report(conn, self.pid)
        self.assertEqual(rep[0]["horizon"], "7d")
        self.assertTrue(rep[0]["late"])
        conn.close()

    def test_missing_views_record_nothing(self):
        now = dt.datetime.now(UTC)
        conn = self.publish(self.pid, 800, "aaaaaaaaaaa", now)
        self.assertEqual(results.update(self.cfg, conn, now, lambda i: {}), 0)
        conn.close()

    def test_norm_needs_peers_then_gives_a_multiplier(self):
        conn = db.connect(self.cfg.db_path)
        oc = db.create_own_channel(conn, "Mine")
        pids = [db.create_production(conn, f"P{i}", "g", None, None)
                for i in range(5)]
        for i, p in enumerate(pids):
            db.update_production(conn, p, own_channel_id=oc,
                                 status="published")
            conn.execute(
                "INSERT INTO prod_results VALUES (?, '7d', 'x', 168, ?)",
                (p, [1000, 1000, 1000, 1000, 4000][i]))
        conn.commit()
        rep = results.report(conn, pids[4])
        self.assertAlmostEqual(rep[0]["multiplier"], 4.0)
        few = results.report(conn, pids[0])
        self.assertAlmostEqual(few[0]["multiplier"], 1.0)
        conn.execute("DELETE FROM prod_results WHERE production_id IN (?, ?)",
                     (pids[1], pids[2]))
        self.assertIsNone(results.report(conn, pids[4])[0]["multiplier"])
        conn.close()

    def test_set_link_stores_id_and_the_packaging_snapshot(self):
        (self.pdir / "kit_dummy").write_text("x")
        conn = db.connect(self.cfg.db_path)
        self.assertEqual(results.set_link(self.cfg, conn, self.pid,
                                          "https://youtu.be/dQw4w9WgXcQ"),
                         "dQw4w9WgXcQ")
        self.assertIsNone(results.set_link(self.cfg, conn, self.pid, "junk"))
        self.assertEqual(db.get_production(conn, self.pid)["youtube_video_id"],
                         "dQw4w9WgXcQ")
        self.assertEqual(results.load_snapshot(self.cfg, self.pid)
                         ["youtube_video_id"], "dQw4w9WgXcQ")
        conn.close()


class LearningTests(unittest.TestCase):
    def rows(self):
        def r(i, title, views, layout="host", h="7d"):
            return {"pid": i, "title": title, "horizon": h, "views": views,
                    "thumb_layout": layout, "thumb_text": "WOW"}
        return [r(1, "7 facts about cats", 5000, "character"),
                r(2, "Why dogs bark", 3000, "host"),
                r(3, "A long boring title about nothing at all in particular x", 500, "host"),
                r(4, "Cats and 3 tricks", 6000, "character"),
                r(5, "Plain words here", 1000, "host")]

    def test_too_little_data_says_nothing(self):
        self.assertEqual(learning.summary(self.rows()[:2])["best"], [])

    def test_summary_ranks_and_compares(self):
        s = learning.summary(self.rows())
        self.assertEqual(s["best"][0]["pid"], 4)
        names = {f["name"]: f for f in s["features"]}
        self.assertGreater(names["has a number"]["avg"],
                           names["has a number"]["others"])
        lay = {l["layout"]: l["avg"] for l in s["layouts"]}
        self.assertGreater(lay["character"], lay["host"])

    def test_horizons_are_never_mixed(self):
        rows = self.rows()
        for r in rows[:3]:
            r["horizon"] = "24h"
            r["views"] //= 100
        s = learning.summary(rows)           # 24h group of 3, 7d group of 2
        self.assertEqual(s["n"], 3)

    def test_prompt_text_marks_a_small_sample(self):
        class C:                      # context_text via collect is exercised
            pass                      # in the integration test below
        s = learning.summary(self.rows())
        self.assertEqual(s["n"], 5)


class LearningIntegration(Base):
    def test_context_text_reaches_the_kit_and_plan_prompts(self):
        now = dt.datetime.now(UTC)
        conn = db.connect(self.cfg.db_path)
        oc = db.create_own_channel(conn, "Mine")
        db.update_production(conn, self.pid, own_channel_id=oc)
        for i, (title, views) in enumerate([("Top 5 cat facts", 9000),
                                            ("Dog stuff", 1000),
                                            ("Bird things", 1200),
                                            ("Fish", 800)]):
            p = (self.pid if i == 0 else
                 db.create_production(conn, title, "g", None, None))
            db.update_production(conn, p, own_channel_id=oc,
                                 status="published", title=title)
            conn.execute("INSERT OR REPLACE INTO prod_results VALUES"
                         " (?, '7d', 'x', 168, ?)", (p, views))
        conn.commit()
        text = learning.context_text(self.cfg, conn, oc)
        self.assertIn("Top 5 cat facts", text)
        self.assertIn("SMALL SAMPLE", text)
        conn.close()
        from whisperradar import packaging, plan
        ctx = packaging.context(self.cfg, self.pid)
        self.assertIn("WHAT HAS WORKED", packaging.writer_prompt(ctx))
        self.assertIn("WHAT HAS WORKED", plan.writer_prompt(plan.context(
            self.cfg, self.pid)))


def item(i, title, mult, ch="C1", dur=600, trend=""):
    return {"video_id": i, "channel_id": ch, "channel_name": ch,
            "genre": "g", "title": title, "url": "u", "views": int(1000 * mult),
            "baseline": 1000.0, "multiplier": mult, "is_short": False,
            "duration": dur, "age_days": 10.0, "trend": trend}


class InsightTests(unittest.TestCase):
    def items(self):
        items = [item(f"n{i}", f"quiet video {i}", 1.0) for i in range(30)]
        items += [item(f"w{i}", f"secret of the deep sea {i}", 8.0, ch="C2")
                  for i in range(5)]
        return items

    def test_keywords_find_over_represented_terms(self):
        kw = {k["term"] for k in insights.keywords(self.items())}
        self.assertIn("secret", kw)
        self.assertIn("deep sea", kw)
        self.assertNotIn("quiet", kw)
        self.assertNotIn("the", kw)

    def test_keywords_need_enough_outliers(self):
        self.assertEqual(insights.keywords(self.items()[:31]), [])

    def test_explain_states_facts(self):
        items = self.items()
        win = item("x", "7 secrets of the deep sea that nobody tells you?",
                   9.0, dur=1500, trend="rising")
        notes = insights.explain(win, items + [win],
                                 insights.keywords(items))
        text = " ".join(notes)
        self.assertIn("9.0x", text)
        self.assertIn("number", text)
        self.assertIn("question", text)
        self.assertIn("still gaining", text)
        self.assertIn("share its subject", text)

    def test_rank_channels(self):
        rows = insights.rank_channels(self.items())
        self.assertEqual(rows[0]["channel_id"], "C2")
        self.assertEqual(rows[0]["outliers"], 5)


class PageTests(Base):
    publish = ResultTests.publish
    def setUp(self):
        super().setUp()
        conn = db.connect(self.cfg.db_path)
        db.add_channel(conn, "Chan", "UC1", genre="g")
        vids = [{"video_id": f"v{i}", "title": f"quiet {i}", "url": "u",
                 "view_count": 1000} for i in range(10)]
        vids += [{"video_id": f"hit{i}", "title": f"secret deep sea {i}",
                  "url": "u", "view_count": 50000} for i in range(6)]
        db.upsert_videos(conn, "UC1", vids)
        db.update_production(conn, self.pid, status="ready")
        conn.commit()
        conn.close()
        self.client = create_app(self.cfg).test_client()

    def test_research_tabs_render(self):
        for tab in ("keywords", "channels", "analyze", "topics"):
            r = self.client.get(f"/research?tab={tab}")
            self.assertEqual(r.status_code, 200, tab)
        html = self.client.get("/research?tab=keywords").get_data(as_text=True)
        self.assertTrue("secret" in html)
        html = self.client.get("/research?tab=analyze&v=hit1").get_data(
            as_text=True)
        self.assertIn("secret deep sea 1", html)
        self.assertTrue("typical views" in html)
        html = self.client.get("/research?tab=analyze&v=zzzzzzzzzzz").get_data(
            as_text=True)
        self.assertIn("not among the scored", html)
        html = self.client.get("/research?tab=channels").get_data(as_text=True)
        self.assertIn("Chan", html)

    def test_publish_with_a_link_tracks_it(self):
        r = self.client.post(f"/studio/{self.pid}/publish",
                             data={"youtube_url": "https://youtu.be/dQw4w9WgXcQ"})
        self.assertEqual(r.status_code, 302)
        conn = db.connect(self.cfg.db_path)
        self.assertEqual(db.get_production(conn, self.pid)["youtube_video_id"],
                         "dQw4w9WgXcQ")
        conn.close()

    def test_publish_without_or_with_a_bad_link_still_publishes(self):
        r = self.client.post(f"/studio/{self.pid}/publish",
                             data={"youtube_url": "nonsense"})
        self.assertIn("no%20YouTube%20video%20id", r.headers["Location"])
        conn = db.connect(self.cfg.db_path)
        self.assertEqual(db.get_production(conn, self.pid)["status"],
                         "published")
        conn.close()
        r = self.client.post(f"/studio/{self.pid}/youtube-link",
                             data={"youtube_url": "x"})
        self.assertIn("error=", r.headers["Location"])
        r = self.client.post(f"/studio/{self.pid}/youtube-link",
                             data={"youtube_url": "dQw4w9WgXcQ"})
        self.assertIn("msg=", r.headers["Location"])

    def test_finished_page_shows_results_and_the_due_banner(self):
        now = dt.datetime.now(UTC)
        conn = db.connect(self.cfg.db_path)
        db.update_production(conn, self.pid, status="published",
                             youtube_video_id="dQw4w9WgXcQ")
        conn.execute("UPDATE productions SET published_at = ? WHERE id = ?",
                     (local_text(now - dt.timedelta(hours=30)), self.pid))
        conn.commit()
        conn.close()
        html = self.client.get("/finished").get_data(as_text=True)
        self.assertIn("1 result due", html)
        conn = db.connect(self.cfg.db_path)
        results.update(self.cfg, conn, now, lambda ids: {ids[0]: 1234})
        conn.close()
        html = self.client.get("/finished").get_data(as_text=True)
        self.assertIn("24h: 1234 views", html)
        self.assertNotIn("result due", html)


if __name__ == "__main__":
    unittest.main()
