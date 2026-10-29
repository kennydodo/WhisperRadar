"""Research > Exploding tab: page, run guard, dismiss (no network)."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402
from whisperradar import db, exploding, youtube_api  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


def rec(cid, name, score, subs=3000):
    return {"channel_id": cid, "name": name, "url": "https://youtube.com/channel/" + cid,
            "about": "", "subscribers": subs, "videos": 30, "channel_age_days": 100,
            "new_channel": True, "country": "US", "uploads_per_week": 2.0,
            "outliers_recent": 1, "outliers_last10": 2, "norm_views": 1000,
            "best": {"video_id": "v" + cid, "title": "Hit " + name, "views": 9000,
                     "multiplier": 9.0, "age_days": 4, "vph": 5.0, "duration": 600,
                     "url": "https://youtu.be/v" + cid, "thumb": "", "views_per_sub": 3.0},
            "score": score, "why": ["9.0x its norm"], "queries": ["topic"]}


class ExplodingRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.add_channel(conn, "Watched", "UCw", genre="g")
        conn.close()
        self._env = os.environ.get(youtube_api.ENV_KEY)
        os.environ.pop(youtube_api.ENV_KEY, None)
        data = exploding.empty()
        data["channels"] = {c["channel_id"]: c for c in (
            rec("UCa", "Alpha", 80), rec("UCb", "Beta", 60), rec("UCw", "Watched", 90))}
        exploding.save(self.cfg, data)
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        if self._env is None:
            os.environ.pop(youtube_api.ENV_KEY, None)
        else:
            os.environ[youtube_api.ENV_KEY] = self._env
        wr_tmp.cleanup(self.tmp)

    def page(self, qs=""):
        return self.client.get("/research?tab=exploding" + qs).get_data(as_text=True)

    def test_tab_lists_saved_channels_best_first_and_hides_watched(self):
        html = self.page()
        self.assertIn(">Exploding</a>", html)
        self.assertLess(html.index("Alpha"), html.index("Beta"))
        self.assertNotIn("Hit Watched", html)
        self.assertIn("9.0x", html)

    def test_without_a_key_the_page_explains_instead_of_failing(self):
        self.assertIn("needs the YouTube API key", self.page())

    def test_filters_apply(self):
        html = self.page("&xscore=70")
        self.assertIn("Alpha", html)
        self.assertNotIn("Hit Beta", html)

    def test_empty_topics_are_refused(self):
        os.environ[youtube_api.ENV_KEY] = "K" * 30
        r = self.client.post("/research/exploding/run", data={"queries": "  "})
        self.assertIn("error=", r.headers["Location"])

    def test_over_quota_run_is_refused_unless_forced(self):
        os.environ[youtube_api.ENV_KEY] = "K" * 30
        with mock.patch.object(youtube_api, "quota_left", return_value=50), \
                mock.patch.object(exploding, "run") as run:
            r = self.client.post("/research/exploding/run", data={"queries": "a topic"})
        self.assertIn("API+units", r.headers["Location"].replace("%20", "+"))
        run.assert_not_called()

    def test_run_starts_a_job_with_the_form_options(self):
        os.environ[youtube_api.ENV_KEY] = "K" * 30
        seen = {}

        def fake_run(cfg, client, queries, opts, watched=(), log=None, cancel=None, **kw):
            seen.update(queries=queries, opts=opts, watched=set(watched))
            return {"units": 1, "candidates": 0, "checked": 0, "exploding": 0, "stopped": ""}

        with mock.patch.object(exploding, "run", fake_run):
            r = self.client.post("/research/exploding/run", data={
                "queries": "alpha topic\nbeta topic", "days": "7", "max_subs": "5000"})
            for _ in range(50):
                if seen:
                    break
                time.sleep(0.05)
        self.assertIn("msg=", r.headers["Location"])
        self.assertEqual(seen["queries"], ["alpha topic", "beta topic"])
        self.assertEqual(seen["opts"]["days"], 7)
        self.assertEqual(seen["opts"]["max_subs"], 5000)
        self.assertIn("UCw", seen["watched"])

    def test_dismiss_hides_the_channel(self):
        self.client.post("/research/exploding/dismiss", data={"channel_id": "UCa"})
        self.assertNotIn("Hit Alpha", self.page())

    def test_page_offers_snowball_and_shows_saved_data(self):
        html = self.page()
        self.assertIn("Snowball: find more channels like these", html)
        self.assertIn("2 strong channel(s): Alpha, Beta", html.replace("Ready to expand from ", ""))
        self.assertIn("Saved on this computer", html)
        self.assertIn("Re-score saved data (free)", html)

    def test_snowball_starts_a_job_from_the_strong_channels(self):
        os.environ[youtube_api.ENV_KEY] = "K" * 30
        seen = {}

        def fake(cfg, client, opts, sopts, watched=(), log=None, cancel=None, **kw):
            seen.update(opts=opts, sopts=sopts, watched=set(watched))
            return {"units": 5, "candidates": 0, "checked": 0, "exploding": 0,
                    "stopped": "", "rounds": 1}

        with mock.patch.object(exploding, "snowball", fake):
            r = self.client.post("/research/exploding/snowball", data={
                "rounds": "9", "phrases": "2", "min_seed_score": "50", "check_limit": "30"})
            for _ in range(50):
                if seen:
                    break
                time.sleep(0.05)
        self.assertIn("msg=", r.headers["Location"])
        self.assertEqual(seen["sopts"]["rounds"], 3)            # clamped
        self.assertEqual(seen["sopts"]["phrases"], 2)
        self.assertEqual(seen["opts"]["check_limit"], 30)
        self.assertIn("UCw", seen["watched"])

    def test_snowball_without_strong_channels_or_quota_is_refused(self):
        os.environ[youtube_api.ENV_KEY] = "K" * 30
        with mock.patch.object(exploding, "snowball") as sb:
            r = self.client.post("/research/exploding/snowball", data={"min_seed_score": "100"})
            self.assertIn("error=", r.headers["Location"])
            with mock.patch.object(youtube_api, "quota_left", return_value=10):
                r = self.client.post("/research/exploding/snowball", data={})
            self.assertIn("error=", r.headers["Location"])
        sb.assert_not_called()

    def test_rescore_route_uses_the_form_thresholds_and_no_api(self):
        with mock.patch.object(exploding, "rescore",
                               return_value={"units": 0, "channels": 4, "exploding": 1}) as rs:
            r = self.client.post("/research/exploding/rescore", data={"min_mult": "6"})
        self.assertEqual(rs.call_args[0][1]["min_mult"], 6.0)
        self.assertIn("no+API+units", r.headers["Location"].replace("%20", "+"))

    def test_suggest_returns_expanded_phrases(self):
        with mock.patch("whisperradar.demand.suggestions",
                        return_value=["tower failures", "tower failures explained"]):
            j = self.client.get("/research/exploding/suggest?q=tower+failures").get_json()
        self.assertIn("tower failures explained", j["queries"])


if __name__ == "__main__":
    unittest.main()
