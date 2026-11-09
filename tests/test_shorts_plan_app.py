"""Short proposals after subtitles: nothing is created until the user picks."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import db, shorts_plan, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402

WORDS = ["harbor", "lantern", "winter", "ledger", "compass", "orchard", "tunnel",
         "mirror", "anchor", "falcon", "garden", "saddle"]


def make_srt(cues=36, each=10):
    out = []
    for i in range(cues):
        a, b = i * each, (i + 1) * each
        fmt = lambda s: f"00:{s // 60:02d}:{s % 60:02d},000"  # noqa: E731
        w = WORDS[i % len(WORDS)]
        out.append(f"{i + 1}\n{fmt(a)} --> {fmt(b)}\nThe {w} story number {i} holds a surprising twist.\n\n")
    return "".join(out)


class ShortsPlanAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        root = Path(self.tmp.name) / "productions"
        root.mkdir()
        db.set_setting(conn, "productions_root", str(root))
        self.ch = db.create_own_channel(conn, "Ch")
        self.src = db.create_production(conn, "Big video", "general", None, None)
        db.update_production(conn, self.src, own_channel_id=self.ch, stage="review")
        conn.commit()
        conn.close()
        self.pdir = studio.prod_dir(self.cfg, self.src)
        self.pdir.mkdir(parents=True, exist_ok=True)
        (self.pdir / "subtitles.srt").write_text(make_srt(), encoding="utf-8")

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def add_audio(self):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=300:duration=360", str(self.pdir / "audio.mp3")],
                       check=True)

    def test_planning_proposes_but_creates_nothing(self):
        plan = shorts_plan.plan_for_production(self.cfg, self.src)
        self.assertIsNotNone(plan)
        self.assertGreaterEqual(len(plan["items"]), 1)
        for it in plan["items"]:
            self.assertEqual(it["status"], shorts_plan.PROPOSED)
            self.assertFalse(it.get("pid"))
        self.assertEqual(shorts_plan.waiting_count(self.cfg, self.src), len(plan["items"]))

    def test_existing_plan_is_not_replaced_without_refresh(self):
        first = shorts_plan.plan_for_production(self.cfg, self.src)
        again = shorts_plan.plan_for_production(self.cfg, self.src)
        self.assertEqual(first["items"], again["items"])

    def test_no_plan_without_subtitles_for_shorts_or_when_off(self):
        (self.pdir / "subtitles.srt").unlink()
        self.assertIsNone(shorts_plan.plan_for_production(self.cfg, self.src))
        (self.pdir / "subtitles.srt").write_text(make_srt(), encoding="utf-8")
        self.cfg.studio_shorts_plan = False
        self.assertIsNone(shorts_plan.plan_for_production(self.cfg, self.src))
        self.cfg.studio_shorts_plan = True
        conn = db.connect(self.cfg.db_path)
        db.update_production(conn, self.src, frame="9:16")
        conn.commit()
        conn.close()
        self.assertIsNone(shorts_plan.plan_for_production(self.cfg, self.src))

    def test_plan_after_subtitles_never_raises(self):
        shorts_plan.plan_after_subtitles(self.cfg, 99999)   # unknown production
        shorts_plan.plan_after_subtitles(self.cfg, self.src)
        self.assertIsNotNone(shorts_plan.load(self.pdir))


class ShortsPlanRouteTests(ShortsPlanAppTests):
    def setUp(self):
        super().setUp()
        from whisperradar.webapp import create_app
        self.client = create_app(self.cfg).test_client()

    def test_page_shows_the_proposals_and_the_video_page_the_badge(self):
        page = self.client.get(f"/studio/{self.src}/shorts").get_data(as_text=True)
        self.assertIn("Short ideas for this video", page)
        self.assertIn('name="pick"', page)
        detail = self.client.get(f"/studio/{self.src}").get_data(as_text=True)
        self.assertIn("choose which to create", detail)

    def test_skip_and_bring_back(self):
        plan = shorts_plan.plan_for_production(self.cfg, self.src)
        iid = plan["items"][0]["id"]
        r = self.client.post(f"/studio/{self.src}/shorts/plan/{iid}/skip")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(shorts_plan.load(self.pdir)["items"][0]["status"], shorts_plan.SKIPPED)
        self.client.post(f"/studio/{self.src}/shorts/plan/{iid}/unskip")
        self.assertEqual(shorts_plan.load(self.pdir)["items"][0]["status"], shorts_plan.PROPOSED)

    def test_create_with_nothing_ticked_creates_nothing(self):
        shorts_plan.plan_for_production(self.cfg, self.src)
        r = self.client.post(f"/studio/{self.src}/shorts/plan/create", data={})
        self.assertIn("error=", r.headers["Location"])
        for it in shorts_plan.load(self.pdir)["items"]:
            self.assertEqual(it["status"], shorts_plan.PROPOSED)

    def test_only_the_ticked_short_is_created(self):
        self.add_audio()
        plan = shorts_plan.plan_for_production(self.cfg, self.src)
        self.assertGreaterEqual(len(plan["items"]), 2)
        first = plan["items"][0]
        r = self.client.post(f"/studio/{self.src}/shorts/plan/create", data={
            "pick": first["id"],
            f"start_{first['id']}": f"{first['start']:.2f}",
            f"end_{first['id']}": f"{first['end']:.2f}",
            f"title_{first['id']}": "My chosen Short"})
        self.assertEqual(r.status_code, 302)
        items = {it["id"]: it for it in shorts_plan.load(self.pdir)["items"]}
        self.assertEqual(items[first["id"]]["status"], shorts_plan.CREATED)
        self.assertEqual(items[first["id"]]["title"], "My chosen Short")
        for iid, it in items.items():
            if iid != first["id"]:
                self.assertEqual(it["status"], shorts_plan.PROPOSED)
        conn = db.connect(self.cfg.db_path)
        try:
            new = db.get_production(conn, items[first["id"]]["pid"])
        finally:
            conn.close()
        self.assertEqual((new["frame"], new["stage"]), ("9:16", "shots"))

    def test_a_bad_time_is_reported_not_created(self):
        self.add_audio()
        plan = shorts_plan.plan_for_production(self.cfg, self.src)
        iid = plan["items"][0]["id"]
        r = self.client.post(f"/studio/{self.src}/shorts/plan/create", data={
            "pick": iid, f"start_{iid}": "abc", f"end_{iid}": "1:00"})
        self.assertIn("error=", r.headers["Location"])
        self.assertEqual(shorts_plan.load(self.pdir)["items"][0]["status"], shorts_plan.PROPOSED)


if __name__ == "__main__":
    unittest.main()
