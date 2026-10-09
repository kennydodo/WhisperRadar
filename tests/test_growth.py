"""Channel growth: snapshot storage, parsing and the velocity maths."""
import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import db, growth, youtube_api  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

NOW = dt.datetime(2026, 10, 7, tzinfo=dt.timezone.utc)


def snap(subs, hours_ago=0.0):
    return {"taken_at": (NOW - dt.timedelta(hours=hours_ago))
            .isoformat(timespec="seconds"), "subscribers": subs,
            "views": None, "video_count": None}


class VelocityTests(unittest.TestCase):
    def test_two_snapshots_give_subs_per_day(self):
        g = growth.velocity([snap(1000, 24), snap(1100, 0)])
        self.assertAlmostEqual(g["delta"], 100)
        self.assertAlmostEqual(g["per_day"], 100)
        self.assertEqual(g["latest"], 1100)

    def test_one_snapshot_or_hidden_counts_are_no_verdict(self):
        self.assertIsNone(growth.velocity([snap(1000, 10)]))
        self.assertIsNone(growth.velocity([{"taken_at": "x"}]))
        self.assertIsNone(growth.velocity([]))

    def test_same_hour_snapshots_say_nothing(self):
        self.assertIsNone(growth.velocity([snap(1000, 1), snap(1001, 0)]))

    def test_bad_timestamps_are_skipped_not_crashed(self):
        g = growth.velocity([snap(1000, 48), snap(1060, 24),
                             {"taken_at": "not a date",
                              "subscribers": 1}])
        self.assertIsNotNone(g)


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def call(self, resource, **params):
        self.calls.append((resource, params))
        return self.payload


class ApiTests(unittest.TestCase):
    def test_channel_stats_parses_and_batches_per_50(self):
        payload = {"items": [
            {"id": "UC1", "statistics": {"subscriberCount": "123",
                                         "viewCount": "9",
                                         "videoCount": "4"}},
            {"id": "UC2", "statistics": {"hiddenSubscriberCount": True,
                                         "viewCount": "9"}}]}
        fake = FakeClient(payload)
        got = youtube_api.channel_stats(fake, ["UC1", "UC2", "UC1"])
        self.assertEqual(got["UC1"], {"subscribers": 123, "views": 9,
                                      "video_count": 4})
        self.assertIsNone(got["UC2"]["subscribers"])
        self.assertEqual(len(fake.calls), 1)

    def test_channel_stats_chunks_ids_of_more_than_50(self):
        payload = {"items": []}
        fake = FakeClient(payload)
        youtube_api.channel_stats(fake, [f"C{i}" for i in range(75)])
        self.assertEqual(len(fake.calls), 2)


class DbSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "wr.db")
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def test_record_then_list_round_trip_once_a_day(self):
        stats = [{"channel_id": "UC1", "subscribers": 1000, "views": 5,
                  "video_count": 2},
                 {"channel_id": "", "subscribers": 1}]
        self.assertEqual(db.record_channel_snapshots(self.conn, stats,
                                                     now=NOW), 1)
        self.assertEqual(db.record_channel_snapshots(
            self.conn, [{"channel_id": "UC1", "subscribers": 1010,
                         "views": 5, "video_count": 2}],
            now=NOW + dt.timedelta(hours=3)), 0)   # same day: one snapshot
        self.assertEqual(db.record_channel_snapshots(
            self.conn, [{"channel_id": "UC1", "subscribers": 1010,
                         "views": 5, "video_count": 2}],
            now=NOW + dt.timedelta(days=1)), 1)
        sn = db.list_channel_snapshots(self.conn, now=NOW + dt.timedelta(
            days=1))["UC1"]
        self.assertEqual([s["subscribers"] for s in sn], [1000, 1010])


class ChannelGrowthPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        db.add_channel(self.conn, "Home One", "UCH", genre="home")
        vids = [{"video_id": f"h{i}", "title": f"japanese home habit {i}",
                 "url": "u", "view_count": 1000} for i in range(10)]
        db.upsert_videos(self.conn, "UCH", vids)
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def test_snapshot_button_stores_and_the_table_shows_the_rate(self):
        from unittest import mock
        stats = {"UCH": {"subscribers": 1000, "views": 1, "video_count": 1}}
        with mock.patch("whisperradar.youtube_api.Client"), mock.patch(
                "whisperradar.youtube_api.channel_stats",
                return_value=stats) as cs:
            r = self.client.post("/research/channel-stats")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(cs.call_args[0][1], ["UCH"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM channel_snapshots").fetchone()[0], 1)
        self.conn.execute("DELETE FROM channel_snapshots")
        db.record_channel_snapshots(self.conn, [{"channel_id": "UCH",
            "subscribers": 1000, "views": 1, "video_count": 1}], now=NOW)
        db.record_channel_snapshots(self.conn, [{"channel_id": "UCH",
            "subscribers": 1100, "views": 1, "video_count": 1}],
            now=NOW + dt.timedelta(days=1))
        html = self.client.get("/research?tab=channels").get_data(
            as_text=True)
        self.assertIn("Snapshot channel sizes", html)
        self.assertIn("+100/day", html)

    def test_no_key_error_is_reported_not_crashed(self):
        from unittest import mock
        with mock.patch("whisperradar.youtube_api.Client",
                        side_effect=youtube_api.NoKey("no key")):
            r = self.client.post("/research/channel-stats")
        self.assertEqual(r.status_code, 302)
        self.assertIn("no%20key", r.headers["Location"])


if __name__ == "__main__":
    unittest.main()
