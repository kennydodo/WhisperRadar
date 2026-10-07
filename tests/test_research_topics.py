"""View snapshots, momentum and the Topics tab."""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402
from tests.test_packaging import Fake  # noqa: E402
from whisperradar import db, outliers, topics  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

NOW = dt.datetime(2026, 10, 7, 12, tzinfo=dt.timezone.utc)


def at(hours_ago):
    return NOW - dt.timedelta(hours=hours_ago)


class MomentumTests(unittest.TestCase):
    def test_views_gained_per_day(self):
        snaps = [(at(48), 1000), (at(24), 1500), (at(0), 2500)]
        self.assertAlmostEqual(outliers.momentum(snaps), 750.0)

    def test_too_few_or_too_close_gives_none(self):
        self.assertIsNone(outliers.momentum([]))
        self.assertIsNone(outliers.momentum([(at(0), 5)]))
        self.assertIsNone(outliers.momentum([(at(2), 5), (at(0), 9)]))

    def test_old_snapshots_outside_the_window_are_ignored(self):
        snaps = [(at(24 * 20), 100), (at(0), 5000)]
        self.assertIsNone(outliers.momentum(snaps))

    def test_never_negative(self):
        self.assertEqual(outliers.momentum([(at(30), 900), (at(0), 800)]), 0.0)

    def test_trend(self):
        self.assertEqual(outliers.trend(900, 300), "rising")
        self.assertEqual(outliers.trend(100, 300), "cooling")
        self.assertEqual(outliers.trend(300, 300), "")
        self.assertEqual(outliers.trend(None, 300), "")

    def test_build_adds_momentum_and_sort(self):
        rows = [{"video_id": f"v{i}", "channel_id": "C", "channel_name": "C",
                 "genre": "g", "title": f"t{i}", "url": "u",
                 "published_at": None, "view_count": 1000,
                 "duration": 600, "status": "new"} for i in range(8)]
        rows.append(dict(rows[0], video_id="hot", view_count=5000))
        rows.append(dict(rows[0], video_id="cold", view_count=5000))
        snaps = {"hot": [(at(48), 3000), (at(0), 5000)],
                 "cold": [(at(48), 4990), (at(0), 5000)]}
        items = outliers.build(rows, NOW, snapshots=snaps)
        shown, _ = outliers.filter_sort(items, sort="momentum")
        self.assertEqual([i["video_id"] for i in shown], ["hot", "cold"])
        self.assertAlmostEqual(shown[0]["momentum"], 1000.0)


class SnapshotDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "wr.db")
        db.init_db(self.conn)
        db.add_channel(self.conn, "Chan", "UC1", genre="g")

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def test_refresh_records_snapshots_with_a_minimum_gap(self):
        db.upsert_videos(self.conn, "UC1", [
            {"video_id": "a", "title": "A", "url": "u", "view_count": 100}])
        self.assertEqual(len(db.list_snapshots(self.conn)["a"]), 1)
        # an immediate refresh does not add another
        db.set_view_counts(self.conn, [{"video_id": "a", "view_count": 120}])
        self.assertEqual(len(db.list_snapshots(self.conn)["a"]), 1)
        # later it does
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=7)
        self.assertEqual(db.record_snapshots(self.conn, [("a", 400)], later), 1)
        self.assertEqual([v for _t, v in db.list_snapshots(self.conn)["a"]],
                         [100, 400])

    def test_missing_views_are_not_stored(self):
        self.assertEqual(db.record_snapshots(self.conn, [("a", None)]), 0)


class TopicParseTests(unittest.TestCase):
    def vids(self):
        def v(i, ch, mult):
            return {"video_id": i, "title": f"title {i}", "channel_id": ch,
                    "channel_name": ch, "genre": "g", "multiplier": mult,
                    "views": 1000 * int(mult)}
        return [v("a", "C1", 10), v("b", "C2", 6), v("c", "C1", 4),
                v("d", "C3", 20)]

    def test_numbers_come_from_code_and_bad_ids_are_dropped(self):
        raw = {"topics": [
            {"name": "Cold", "angle": "x", "video_ids": ["a", "b", "zzz"]},
            {"name": "Solo", "angle": "y", "video_ids": ["d"]},
            {"name": "Dup", "video_ids": ["a"]},         # already used
            {"name": "", "video_ids": ["c"]},
            {"name": "None", "video_ids": ["nope"]}]}
        got = topics.parse_topics(raw, self.vids())
        self.assertEqual([t["name"] for t in got], ["Cold", "Solo"])
        self.assertEqual(got[0]["channels"], 2)
        self.assertEqual(got[0]["best"], 10)
        self.assertEqual(got[0]["video_ids"], ["a", "b"])
        self.assertEqual(got[0]["views"], 16000)

    def test_more_channels_ranks_first(self):
        raw = {"topics": [{"name": "Big", "video_ids": ["d"]},
                          {"name": "Wide", "video_ids": ["a", "b"]}]}
        self.assertEqual(topics.parse_topics(raw, self.vids())[0]["name"],
                         "Wide")


class TopicsFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        db.add_channel(self.conn, "Chan", "UC1", genre="g")
        vids = [{"video_id": f"v{i}", "title": f"V{i}", "url": f"u{i}",
                 "view_count": 1000} for i in range(8)]
        vids += [{"video_id": f"hit{i}", "title": f"Hit {i}", "url": "u",
                  "view_count": 50000} for i in range(4)]
        db.upsert_videos(self.conn, "UC1", vids)
        self.own = db.create_own_channel(self.conn, "Mine")

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def videos(self):
        rows = self.conn.execute(
            "SELECT v.video_id, v.channel_id, c.name AS channel_name,"
            " c.genre AS genre, v.title, v.url, v.published_at,"
            " v.view_count, v.duration, v.status FROM videos v"
            " JOIN channels c ON c.channel_id = v.channel_id").fetchall()
        return topics.candidates(outliers.build(rows))

    def test_run_saves_topics(self):
        vids = self.videos()
        self.assertEqual(len(vids), 4)
        reply = json.dumps({"topics": [
            {"name": "A", "angle": "a", "video_ids": ["hit0", "hit1"]},
            {"name": "B", "angle": "b", "video_ids": ["hit2"]},
            {"name": "C", "angle": "c", "video_ids": ["hit3"]}]})
        t = Fake(zai=[reply])
        data = topics.run_topics(self.cfg, t, vids, log=lambda m: None)
        self.assertEqual(len(data["topics"]), 3)
        self.assertEqual(len(topics.load_topics(self.cfg)["topics"]), 3)
        self.assertIn("hit0", t.calls[0]["prompt"])

    def test_unusable_reply_is_asked_again_then_fails(self):
        from whisperradar import webstages
        t = Fake(zai=["nope", "still no", "nothing"])
        with self.assertRaises(webstages.StageFailed):
            topics.run_topics(self.cfg, t, self.videos(), log=lambda m: None)
        self.assertEqual(len(t.calls), 3)

    def test_too_few_outliers_fails_clearly(self):
        from whisperradar import webstages
        with self.assertRaises(webstages.StageFailed):
            topics.run_topics(self.cfg, Fake(), self.videos()[:2],
                              log=lambda m: None)

    def test_pages(self):
        client = create_app(self.cfg).test_client()
        html = client.get("/research?tab=topics").get_data(as_text=True)
        self.assertIn("Group the outliers into topics", html)
        topics.save_topics(self.cfg, {"made_at": "2026-10-07T10:00",
                                      "genre": "", "topics": topics.parse_topics(
            {"topics": [{"name": "Winners", "angle": "why", "video_ids":
                         ["hit0", "hit1"]}]}, self.videos())})
        html = client.get("/research?tab=topics").get_data(as_text=True)
        self.assertIn("Winners", html)
        self.assertIn('name="source_video_id" value="hit0"', html)
        out = client.get("/research?sort=momentum").get_data(as_text=True)
        self.assertIn("now / day", out)
        self.assertEqual(client.get("/research?tab=zzz").status_code, 200)


if __name__ == "__main__":
    unittest.main()
