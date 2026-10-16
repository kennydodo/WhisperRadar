"""Watched-channel "Get style & bible": frames from videos, answer split into
style + bible, saved with the watched channel only."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402,F401

from whisperradar import channel_look, db  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

REPLY = ("intro\n=== STYLE GUIDE ===\nSoft 3D, teal and orange.\n"
         "=== BIBLE ===\nA tall robot; a neon city.")


class FakeTransport:
    def __init__(self):
        self.calls = []

    def ask(self, site, prompt, files=(), **kw):
        self.calls.append((site, prompt, list(files)))
        return REPLY


class LookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = t / "wr.db"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        conn.execute("INSERT INTO channels (name, channel_id, kind, genre, "
                     "active, added_at) VALUES ('Rival','UCrival','source',"
                     "'x',1,'2026-01-01')")
        for i in range(3):
            conn.execute(
                "INSERT INTO videos (channel_id, video_id, title, url, "
                "published_at, view_count, duration) VALUES "
                "('UCrival', ?, ?, ?, '2026-01-0'||?, ?, 600)",
                (f"v{i}", f"Title {i}", f"https://y/{i}", i + 1, i * 10))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _grab(self, video, at, out):
        out.write_bytes(b"jpg")
        return True

    def test_parse_reply(self):
        s, b = channel_look.parse_reply(REPLY)
        self.assertEqual((s, b), ("Soft 3D, teal and orange.",
                                  "A tall robot; a neon city."))
        self.assertEqual(channel_look.parse_reply("just text"),
                         ("just text", ""))

    def test_frame_times_have_an_early_frame_and_shift(self):
        a = channel_look.frame_times(600, 4, 0.0)
        b = channel_look.frame_times(600, 4, 0.7)
        self.assertEqual(len(a), 4)
        self.assertLess(a[0], 30)
        self.assertNotEqual(a, b)
        self.assertTrue(all(0 < t < 600 for t in a + b))
        self.assertEqual(len(channel_look.frame_times(0, 2)), 2)

    def test_similar_titles_are_not_all_picked(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE videos (video_id, channel_id, title, "
                     "view_count, published_at)")
        rows = [("a", "Why Humans Never Eat Hippos Meat", 100),
                ("b", "Why Humans Never Eat Hippos Meat Today", 90),
                ("c", "Cheetahs Are Friendly Pets", 80)]
        for vid, t, v in rows:
            conn.execute("INSERT INTO videos VALUES (?,?,?,?,?)",
                         (vid, "C", t, v, "2026"))
        got = [v["video_id"] for v in channel_look.pick_videos(conn, "C", 2)]
        self.assertEqual(got, ["a", "c"])

    def test_split_counts_within_range(self):
        self.assertEqual(sum(channel_look.split_counts(8, 3)), 8)
        self.assertEqual(channel_look.split_counts(5, 9), [1] * 5)

    def test_run_saves_only_with_the_watched_channel(self):
        tr = FakeTransport()
        logs = []
        meta = channel_look.run(
            self.cfg, "UCrival", "chatgpt", tr, logs.append, n_frames=6,
            download=lambda url, d: (d / "x.mp4", 600), grab=self._grab,
            hint="has a cartoon narrator", thumbs=None)
        self.assertEqual(meta["frames"], 6)
        site, prompt, files = tr.calls[0]
        self.assertIn("has a cartoon narrator", prompt)
        self.assertEqual(len(files), 6)
        self.assertIn("Title 2", prompt)  # most viewed is picked first
        got = channel_look.load(self.cfg, "UCrival")
        self.assertIn("teal", got["style"])
        self.assertIn("robot", got["bible"])
        self.assertEqual(len(got["files"]), 6)
        # nothing else in the data folder besides channel_look
        names = {p.name for p in Path(self.cfg.db_path).parent.iterdir()}
        self.assertTrue(names <= {"wr.db", "channel_look", "wr.db-wal",
                                  "wr.db-shm", "wr.db-journal"}, names)

    def test_page_shows_the_chat_models_on_the_same_line(self):
        from whisperradar import webchat
        client = create_app(self.cfg).test_client()   # loads saved sites
        site = {"key": "paidchat", "name": "Paid Chat", "paid": True,
                "url": "https://example.com/", "box": "textarea",
                "reply": ".r", "models": [{"id": "m1", "label": "Model One"}],
                "levels": [{"id": "hi", "label": "High"}],
                "level_label": "Thinking", "toggles": []}
        defs, _ = webchat.sanitize_sites([site])
        keys = webchat.set_custom_sites(defs)
        webchat._UI["paidchat"] = {k: defs[0][k] for k in (
            "key", "name", "models", "levels", "level_label", "toggles",
            "paid")}
        try:
            r = client.get("/watched/UCrival/look")
            html = r.get_data(as_text=True)
            self.assertIn('name="wc_paidchat_model"', html)
            self.assertIn("Model One", html)
            self.assertIn('name="wc_paidchat_level"', html)
        finally:
            webchat.set_custom_sites([])
            webchat._UI.clear()

    def test_total_attachments_are_capped(self):
        tr = FakeTransport()
        channel_look.run(
            self.cfg, "UCrival", "chatgpt", tr, lambda m: None, n_frames=50,
            download=lambda url, d: (d / "x.mp4", 600), grab=self._grab,
            thumbs=lambda vids, dest, log: [
                {"file": f"t{i}.jpg", "video": "v", "title": "t"}
                for i in range(3)])
        self.assertLessEqual(len(tr.calls[0][2]), channel_look.MAX_ATTACH)

    def test_failed_refresh_keeps_the_saved_result(self):
        kw = dict(download=lambda url, d: (d / "x.mp4", 600),
                  grab=self._grab, thumbs=None)
        channel_look.run(self.cfg, "UCrival", "chatgpt", FakeTransport(),
                         lambda m: None, n_frames=6, **kw)
        first = channel_look.load(self.cfg, "UCrival")

        class Boom:
            def ask(self, *a, **k):
                raise RuntimeError("chat failed")
        with self.assertRaises(RuntimeError):
            channel_look.run(self.cfg, "UCrival", "chatgpt", Boom(),
                             lambda m: None, n_frames=6, **kw)
        again = channel_look.load(self.cfg, "UCrival")
        self.assertEqual(again["style"], first["style"])
        self.assertEqual(again["files"], first["files"])
        # a successful refresh replaces everything
        channel_look.run(self.cfg, "UCrival", "chatgpt", FakeTransport(),
                         lambda m: None, n_frames=5, **kw)
        self.assertEqual(len(channel_look.load(self.cfg, "UCrival")["files"]),
                         5)

    def test_too_few_frames_fails(self):
        with self.assertRaises(RuntimeError):
            channel_look.run(
                self.cfg, "UCrival", "chatgpt", FakeTransport(), print,
                download=lambda u, d: (_ for _ in ()).throw(OSError("no")),
                grab=self._grab, thumbs=None)

    def test_pages_render(self):
        client = create_app(self.cfg).test_client()
        r = client.get("/watched/UCrival/look")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Style", r.data)
        r = client.get("/watched")
        self.assertIn(b"/watched/UCrival/look", r.data)
        # research / watched-channel work has its own job slot
        j = client.get("/research/job").get_json()
        self.assertFalse(j["running"])
        self.assertFalse(client.get("/studio/job").get_json()["running"])
        r = client.post("/watched/UCrival/look/run", data={"site": "nope"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("error=", r.headers["Location"])

    def _kw(self):
        return dict(download=lambda url, d: (d / "x.mp4", 600),
                    grab=self._grab, thumbs=None)

    def test_collects_a_big_pool_and_sends_a_random_15(self):
        import random
        tr = FakeTransport()
        meta = channel_look.run(self.cfg, "UCrival", "chatgpt", tr,
                                lambda m: None, n_frames=45,
                                rng=random.Random(1), **self._kw())
        got = channel_look.load(self.cfg, "UCrival")
        self.assertEqual(len(got["files"]), 45)       # the whole pool is kept
        self.assertEqual(len(tr.calls[0][2]), 15)     # 15 are sent
        self.assertEqual(meta["frames"], 45)
        self.assertEqual(sorted(Path(f).name for f in tr.calls[0][2]),
                         got["sent"])

    def test_pool_can_reach_fifty(self):
        channel_look.run(self.cfg, "UCrival", "chatgpt", FakeTransport(),
                         lambda m: None, n_frames=50, **self._kw())
        self.assertEqual(len(channel_look.load(self.cfg, "UCrival")["files"]),
                         50)

    def test_choose_files(self):
        pool = [f"f{i:02d}.jpg" for i in range(1, 41)]
        self.assertEqual(len(channel_look.choose_files(pool)), 15)
        self.assertEqual(channel_look.choose_files(pool, ["f03.jpg", "zz.jpg"]),
                         ["f03.jpg"])
        with self.assertRaises(ValueError):
            channel_look.choose_files(pool, ["nope.jpg"])
        with self.assertRaises(ValueError):
            channel_look.choose_files(pool, pool[:19])
        self.assertEqual(len(channel_look.choose_files(pool, None, 99)),
                         channel_look.MAX_ATTACH)

    def test_regenerate_uses_the_chosen_frames_without_downloading(self):
        channel_look.run(self.cfg, "UCrival", "chatgpt", FakeTransport(),
                         lambda m: None, n_frames=30, **self._kw())
        tr = FakeTransport()
        tr.ask = lambda site, prompt, files=(), **kw: (
            tr.calls.append((site, prompt, list(files))) or
            REPLY.replace("teal", "plum"))
        meta = channel_look.regenerate(
            self.cfg, "UCrival", "chatgpt", tr, lambda m: None,
            selected=["f02.jpg", "f05.jpg", "f09.jpg"], hint="mascot")
        self.assertEqual([Path(f).name for f in tr.calls[0][2]],
                         ["f02.jpg", "f05.jpg", "f09.jpg"])
        self.assertIn("mascot", tr.calls[0][1])
        self.assertIn("Title 2", tr.calls[0][1])      # video context kept
        got = channel_look.load(self.cfg, "UCrival")
        self.assertEqual(got["sent"], ["f02.jpg", "f05.jpg", "f09.jpg"])
        self.assertEqual(len(got["files"]), 30)       # pool untouched
        self.assertEqual(meta["frames"], 30)

    def test_regenerate_without_ticks_draws_new_random_ones(self):
        channel_look.run(self.cfg, "UCrival", "chatgpt", FakeTransport(),
                         lambda m: None, n_frames=30, **self._kw())
        tr = FakeTransport()
        channel_look.regenerate(self.cfg, "UCrival", "chatgpt", tr,
                                lambda m: None, send_count=7)
        self.assertEqual(len(tr.calls[0][2]), 7)

    def test_failed_regenerate_keeps_the_saved_result(self):
        channel_look.run(self.cfg, "UCrival", "chatgpt", FakeTransport(),
                         lambda m: None, n_frames=20, **self._kw())
        first = channel_look.load(self.cfg, "UCrival")

        class Boom:
            def ask(self, *a, **k):
                raise RuntimeError("chat failed")
        with self.assertRaises(RuntimeError):
            channel_look.regenerate(self.cfg, "UCrival", "chatgpt", Boom(),
                                    lambda m: None)
        again = channel_look.load(self.cfg, "UCrival")
        self.assertEqual((again["style"], again["sent"]),
                         (first["style"], first["sent"]))

    def test_regenerate_needs_collected_frames(self):
        with self.assertRaises(RuntimeError):
            channel_look.regenerate(self.cfg, "UCrival", "chatgpt",
                                    FakeTransport(), lambda m: None)

    def test_page_has_regenerate_and_frame_ticks(self):
        channel_look.run(self.cfg, "UCrival", "chatgpt", FakeTransport(),
                         lambda m: None, n_frames=20, **self._kw())
        from whisperradar import webchat
        client = create_app(self.cfg).test_client()   # loads saved sites
        defs, _ = webchat.sanitize_sites([{
            "key": "paidchat", "name": "Paid Chat", "paid": False,
            "url": "https://example.com/", "box": "textarea", "reply": ".r"}])
        webchat.set_custom_sites(defs)
        webchat._UI["paidchat"] = {k: defs[0].get(k) for k in (
            "key", "name", "models", "levels", "level_label", "toggles",
            "paid")}
        try:
            html = client.get("/watched/UCrival/look").get_data(as_text=True)
            self.assertIn("/look/regen", html)
            self.assertIn('name="pick"', html)
            self.assertIn('name="send"', html)
            self.assertEqual(html.count('class="pick"'), 20)
            self.assertEqual(html.count(" checked>"), 15)
        finally:
            webchat.set_custom_sites([])
            webchat._UI.clear()

    def test_regen_route_rejects_bad_requests(self):
        client = create_app(self.cfg).test_client()
        r = client.post("/watched/UCrival/look/regen", data={"site": "nope"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("error=", r.headers["Location"])
        r = client.post("/watched/UCrival/look/regen", data={
            "site": "nope", "pick": [f"f{i}.jpg" for i in range(30)]})
        self.assertIn("at%20most", r.headers["Location"])


if __name__ == "__main__":
    unittest.main()
