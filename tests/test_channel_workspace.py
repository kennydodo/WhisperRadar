"""The studio works inside ONE channel at a time, and a channel's job never
blocks another channel's: one job slot per own channel, a channel picker, a
production page locked to its channel, and watched channels linked to a
channel narrowing its source list."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import autorun, db, producer  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        self.a = db.create_own_channel(self.conn, "Channel A")
        self.b = db.create_own_channel(self.conn, "Channel B")
        self.pa1 = self._prod("Alpha one", self.a)
        self.pa2 = self._prod("Alpha two", self.a)
        self.pb = self._prod("Bravo one", self.b)
        self.p0 = self._prod("Loose one", None)
        self.app = create_app(self.cfg)
        self.client = self.app.test_client()

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def _prod(self, title, channel):
        pid = db.create_production(self.conn, title, "general", None, None)
        db.update_production(self.conn, pid, own_channel_id=channel)
        return pid

    def select(self, cid, client=None):
        (client or self.client).post("/studio/channel",
                                     data={"own_channel_id": cid})


class PickerTests(Base):
    def test_without_a_channel_the_studio_asks_to_pick_one(self):
        page = self.client.get("/studio").data
        self.assertTrue(b"Choose a channel" in page)
        self.assertTrue(b"Channel A" in page and b"Channel B" in page)
        self.assertFalse(b"Alpha one" in page)
        self.assertFalse(b"New production" in page)

    def test_a_selected_channel_shows_only_its_productions(self):
        self.select(self.a)
        page = self.client.get("/studio").data
        self.assertTrue(b"Alpha one" in page and b"Alpha two" in page)
        self.assertFalse(b"Bravo one" in page)
        self.assertFalse(b"Loose one" in page)
        self.assertTrue(b"Switch channel" in page)

    def test_switching_shows_the_other_channel(self):
        self.select(self.a)
        self.client.post("/studio/channel/leave")
        self.assertTrue(b"Choose a channel" in self.client.get("/studio").data)
        self.select(self.b)
        page = self.client.get("/studio").data
        self.assertTrue(b"Bravo one" in page)
        self.assertFalse(b"Alpha one" in page)

    def test_no_channel_bucket_is_offered_only_when_needed(self):
        page = self.client.get("/studio").data
        self.assertTrue(b"No channel" in page)       # Loose one exists
        self.select(0)
        page = self.client.get("/studio").data
        self.assertTrue(b"Loose one" in page)
        self.assertFalse(b"Alpha one" in page)

    def test_new_productions_land_in_the_selected_channel(self):
        self.select(self.b)
        page = self.client.get("/studio").data
        self.assertTrue(b'name="own_channel_id" value="%d"' % self.b in page)
        self.client.post("/studio/new", data={"title": "Fresh",
                                              "own_channel_id": self.b})
        rows = [r for r in db.list_productions(self.conn)
                if r["title"] == "Fresh"]
        self.assertEqual(rows[0]["own_channel_id"], self.b)

    def test_an_unknown_channel_cannot_be_selected(self):
        r = self.client.post("/studio/channel", data={"own_channel_id": 999})
        self.assertIn("error", r.headers["Location"])

    def test_with_no_own_channels_nothing_changes(self):
        tmp = tempfile.TemporaryDirectory()
        cfg = load_config(ROOT / "config.yaml")
        cfg.db_path = Path(tmp.name) / "x.db"
        cfg.studio_dir = Path(tmp.name) / "studio"
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        pid = db.create_production(conn, "Solo", "general", None, None)
        conn.close()
        page = create_app(cfg).test_client().get("/studio").data
        self.assertTrue(b"Solo" in page)
        self.assertFalse(b"Choose a channel" in page)
        wr_tmp.cleanup(tmp)

    def test_finished_page_is_scoped_to_the_selected_channel(self):
        db.update_production(self.conn, self.pa1, status="ready")
        db.update_production(self.conn, self.pb, status="ready")
        self.select(self.a)
        page = self.client.get("/finished").data
        self.assertTrue(b"Alpha one" in page)
        self.assertFalse(b"Bravo one" in page)
        self.assertTrue(b"show all channels" in page)
        page = self.client.get("/finished?all=1").data
        self.assertTrue(b"Alpha one" in page and b"Bravo one" in page)


class LockedProductionTests(Base):
    def test_opening_a_production_selects_and_locks_its_channel(self):
        r = self.client.get(f"/studio/{self.pb}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(b"Leave the production" in r.data)
        self.assertFalse(b"change channel" in r.data)
        # the studio list now opens inside that production's channel
        page = self.client.get("/studio").data
        self.assertTrue(b"Bravo one" in page)
        self.assertFalse(b"Alpha one" in page)

    def test_a_production_with_no_channel_can_still_be_assigned(self):
        r = self.client.get(f"/studio/{self.p0}")
        self.assertTrue(b"change channel" in r.data)


class ParallelJobTests(Base):
    def _run_blocking(self, started, release):
        def fake(cfg, pid, job=None, log=None, provider=None, **kw):
            started.append(pid)
            release.wait(10)
            return "ok"
        return fake

    def test_two_channels_run_at_once_but_one_channel_does_one_thing(self):
        started, release = [], threading.Event()
        plan = [{"action": "run", "stage": "style", "detail": "x"}]
        with mock.patch.object(autorun, "build_plan", return_value=plan), \
                mock.patch.object(autorun, "run_pipeline",
                                  self._run_blocking(started, release)):
            ca, cb = self.app.test_client(), self.app.test_client()
            self.select(self.a, ca)
            self.select(self.b, cb)
            r1 = ca.post(f"/studio/{self.pa1}/auto-run")
            self.assertIn("started", r1.headers["Location"])
            # a second job in the SAME channel is refused...
            r2 = ca.post(f"/studio/{self.pa2}/auto-run")
            self.assertIn("already", r2.headers["Location"].replace("+", " "))
            # ...but another channel is not blocked
            r3 = cb.post(f"/studio/{self.pb}/auto-run")
            self.assertIn("started", r3.headers["Location"])
            for _ in range(100):
                if len(started) == 2:
                    break
                threading.Event().wait(0.05)
            self.assertEqual(sorted(started), sorted([self.pa1, self.pb]))
            # each channel reports its own job
            ja = ca.get(f"/studio/job?pid={self.pa1}").get_json()
            jb = cb.get(f"/studio/job?pid={self.pb}").get_json()
            self.assertTrue(ja["running"] and jb["running"])
            self.assertEqual(ja["pid"], self.pa1)
            self.assertEqual(jb["pid"], self.pb)
            # the list in channel B notes that channel A is busy
            page = cb.get("/studio").data
            self.assertTrue(b"Also running in" in page and b"Channel A" in page)
            release.set()

    def test_a_job_in_one_channel_does_not_show_busy_in_another(self):
        started, release = [], threading.Event()
        plan = [{"action": "run", "stage": "style", "detail": "x"}]
        with mock.patch.object(autorun, "build_plan", return_value=plan), \
                mock.patch.object(autorun, "run_pipeline",
                                  self._run_blocking(started, release)):
            ca = self.app.test_client()
            self.select(self.a, ca)
            ca.post(f"/studio/{self.pa1}/auto-run")
            other = self.app.test_client().get(
                f"/studio/job?pid={self.pb}").get_json()
            self.assertFalse(other["running"])
            release.set()


class ProducerScopeTests(Base):
    def test_the_producer_plan_can_be_limited_to_one_channel(self):
        conn = db.connect(self.cfg.db_path)
        try:
            full = producer.build_plan(self.cfg, conn)
            only = producer.build_plan(self.cfg, conn, self.a)
        finally:
            conn.close()
        names = {e.get("own_channel") for e in only if e.get("own_channel")}
        self.assertTrue(names <= {"Channel A"}, names)
        allnames = {e.get("own_channel") for e in full if e.get("own_channel")}
        self.assertTrue(len(allnames) >= len(names))


class WatchedChannelTests(Base):
    def setUp(self):
        super().setUp()
        for cid, name in (("UC1", "Watch One"), ("UC2", "Watch Two")):
            self.conn.execute(
                "INSERT INTO channels (name, channel_id) VALUES (?, ?)",
                (name, cid))
        self.conn.commit()

    def _video(self, vid, ch, title):
        self.conn.execute(
            "INSERT INTO videos (video_id, channel_id, title, url, status)"
            " VALUES (?, ?, ?, ?, 'transcribed')",
            (vid, ch, title, f"https://example.test/{vid}"))
        self.conn.commit()

    def test_channel_form_saves_the_watched_list(self):
        r = self.client.post("/my-channels/edit", data={
            "id": self.a, "watched_present": "1",
            "watched": ["UC2", "UNKNOWN"]})
        self.assertEqual(r.status_code, 302)
        row = db.get_own_channel(self.conn, self.a)
        self.assertEqual(db.own_channel_watched(row), ["UC2"])
        self.client.post("/my-channels/edit", data={
            "id": self.a, "watched_present": "1"})
        row = db.get_own_channel(self.conn, self.a)
        self.assertEqual(db.own_channel_watched(row), [])

    def test_source_list_is_narrowed_to_the_linked_channels(self):
        self._video("v1", "UC1", "Video from one")
        self._video("v2", "UC2", "Video from two")
        self.select(self.a)
        both = self.client.get("/studio").data
        self.assertTrue(b"Video from one" in both and b"Video from two" in both)
        db.update_own_channel(self.conn, self.a,
                              watched_channels=json.dumps(["UC2"]))
        page = self.client.get("/studio").data
        self.assertFalse(b"Video from one" in page)
        self.assertTrue(b"Video from two" in page)

    def test_channels_page_lists_watched_channels_to_tick(self):
        page = self.client.get("/my-channels").data
        self.assertTrue(b"Watch One" in page and b'name="watched"' in page)


if __name__ == "__main__":
    unittest.main()
