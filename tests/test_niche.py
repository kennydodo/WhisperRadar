"""Niche cards: what to make next, from the watched-genre data we store."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import db, niche  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


def vid(i, genre="home", mult=1.0, age=50.0, brk=False, ch="A"):
    return {"video_id": f"v{i}", "channel_id": ch, "channel_name": ch,
            "genre": genre, "title": f"t{i}", "url": "u", "views": 1000,
            "multiplier": mult, "age_days": age, "breakout": brk}


class CardTests(unittest.TestCase):
    def test_small_genres_get_no_card(self):
        items = [vid(i) for i in range(niche.MIN_SCORED - 1)]
        self.assertEqual(niche.cards(items), [])

    def test_rising_when_newest_beat_the_niche_norm_and_something_pops(self):
        items = ([vid(i, mult=1.0, age=100 + i) for i in range(20)]
                 + [vid(f"r{i}", mult=3.0, age=1 + i) for i in range(8)]
                 + [vid("x", mult=5.0, age=2, brk=True)])
        got = niche.cards(items, recent_n=9)
        self.assertEqual(len(got), 1)
        card = got[0]
        self.assertEqual(card["verdict"], "rising")
        self.assertGreaterEqual(card["heat"], niche.RISING_HEAT)
        self.assertEqual(card["breakouts"], 1)

    def test_crowded_and_cooling_beats_steady(self):
        items = ([vid(i, mult=1.5, age=300 + i) for i in range(15)]
                 + [vid(f"n{i}", mult=0.8, age=i * 0.5) for i in range(12)])
        card = niche.cards(items, recent_n=12)[0]
        self.assertEqual(card["verdict"], "crowded")
        self.assertGreaterEqual(card["uploads_per_week"],
                                niche.CROWD_SUPPLY)

    def test_undated_when_dates_are_missing(self):
        items = [vid(i, age=None) for i in range(8)]
        card = niche.cards(items)[0]
        self.assertEqual(card["verdict"], "undated")
        self.assertIsNone(card["recent_median"])

    def test_steady_and_strong_verdicts(self):
        steady = niche.cards([vid(i) for i in range(10)])[0]
        self.assertEqual(steady["verdict"], "steady")
        strong = niche.cards([vid(i, mult=4.0, age=2 + i)
                              for i in range(10)])[0]
        self.assertEqual(strong["verdict"], "strong")

    def test_sorted_by_recent_median_then_size(self):
        a = [vid(i, genre="a", mult=2.0) for i in range(6)]
        b = [vid(i, genre="b", mult=8.0, age=2 + i) for i in range(6)]
        b += [vid(i + 20, genre="b", mult=1.0, age=90 + i)
              for i in range(6)]
        got = niche.cards(a + b)
        self.assertEqual([c["genre"] for c in got], ["b", "a"])

    def test_channels_are_counted_once_each(self):
        items = [vid(i, ch=f"C{i % 3}") for i in range(9)]
        self.assertEqual(niche.cards(items)[0]["channels"], 3)


class NichePageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        db.add_channel(self.conn, "Home One", "UCH", genre="home")
        db.add_channel(self.conn, "Home Two", "UHH", genre="home")
        db.add_channel(self.conn, "Money Man", "UCM", genre="finance")
        for cid, words in (("UCH", "japanese home habits"),
                           ("UHH", "japanese home declutter"),
                           ("UCM", "investing dividend compound")):
            vids = [{"video_id": f"{cid}q{i}",
                     "title": f"{words} guide {i}", "url": "u",
                     "view_count": 1000,
                     "published_at": "2026-09-20T00:00:00Z"}
                    for i in range(45)]
            vids += [{"video_id": f"{cid}h{i}",
                      "title": f"{words} secret {i:02d}", "url": "u",
                      "view_count": 20000 + i * 1000,
                      "published_at": "2026-10-05T00:00:00Z"}
                     for i in range(20)]
            db.upsert_videos(self.conn, cid, vids)
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def get(self, qs=""):
        return self.client.get("/research?" + qs).get_data(as_text=True)

    def test_niches_tab_lists_cards_with_heat_and_verdict(self):
        html = self.get("tab=niches")
        self.assertIn("What to make next", html)
        self.assertIn(">home<", html)
        self.assertIn(">finance<", html)
        self.assertIn("recent median", html)

    def test_niches_tab_links_genres_to_filtered_outliers(self):
        html = self.get("tab=niches")
        self.assertIn('href="/research?genre=home', html)

    def test_channels_tab_annotates_similar_channels(self):
        html = self.get("tab=channels")
        self.assertIn("also watch", html)
        self.assertIn('title="shares: ', html)


if __name__ == "__main__":
    unittest.main()
