"""Research outliers: paging, sorting by column, paused channels."""
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


def item(i, mult, title=None, ch="C1", age=None, mo=None):
    return {"video_id": f"v{i}", "channel_id": ch, "channel_name": ch,
            "genre": "g", "title": title or f"t{i}", "url": "u",
            "views": int(1000 * mult), "baseline": 1000.0,
            "multiplier": mult, "is_short": False, "duration": 600,
            "age_days": age, "vpd": None, "momentum": mo, "trend": ""}


class SortPageTests(unittest.TestCase):
    def setUp(self):
        self.items = [item(i, 3 + i, age=(None if i == 2 else i),
                           mo=(None if i == 0 else float(i)))
                      for i in range(10)]

    def ids(self, **kw):
        return [i["video_id"] for i in outliers.filter_sort(self.items, **kw)[0]]

    def test_paging_slices_and_reports_the_full_count(self):
        shown, n = outliers.filter_sort(self.items, limit=4, offset=4)
        self.assertEqual(n, 10)
        self.assertEqual([i["video_id"] for i in shown],
                         ["v5", "v4", "v3", "v2"])

    def test_reverse_flips_the_direction(self):
        self.assertEqual(self.ids(reverse=True)[0], "v0")

    def test_missing_values_stay_last_when_flipped(self):
        got = self.ids(sort="recent", reverse=True)
        self.assertEqual(got[-1], "v2")                  # undated
        self.assertEqual(got[0], "v9")                   # oldest first
        got = self.ids(sort="momentum", reverse=True)
        self.assertEqual(got[-1], "v0")

    def test_title_and_channel_sorts(self):
        items = [item(1, 5, "b", "Y"), item(2, 4, "a", "Z"),
                 item(3, 9, "c", "X")]
        t = [i["title"] for i in outliers.filter_sort(items, sort="title")[0]]
        self.assertEqual(t, ["a", "b", "c"])
        c = [i["channel_id"] for i in outliers.filter_sort(
            items, sort="channel")[0]]
        self.assertEqual(c, ["X", "Y", "Z"])


class PageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        db.add_channel(self.conn, "Active One", "UC1", genre="g")
        db.add_channel(self.conn, "Paused Two", "UC2", genre="g")
        for cid in ("UC1", "UC2"):
            vids = [{"video_id": f"{cid}n{i}", "title": f"quiet {cid} {i}",
                     "url": "u", "view_count": 1000} for i in range(45)]
            vids += [{"video_id": f"{cid}h{i}", "title": f"hit {cid} {i:02d}",
                      "url": "u", "view_count": 20000 + i * 1000}
                     for i in range(30)]
            db.upsert_videos(self.conn, cid, vids)
        db.update_channel(self.conn, "UC2", active=0)
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def get(self, qs=""):
        return self.client.get("/research?" + qs).get_data(as_text=True)

    def test_pages_and_per_page(self):
        html = self.get("per=25")
        self.assertIn("page 1 of 2", html)
        self.assertIn("page=2", html)
        p2 = self.get("per=25&page=2")
        self.assertIn("page 2 of 2", p2)
        self.assertIn("hit UC1", p2)
        self.assertIn("page 1 of 2", self.get("per=25&page=99").replace(
            "page 2 of 2", "page 1 of 2"))        # out of range is clamped

    def test_sorting_links_flip_on_the_active_column(self):
        html = self.get("sort=views")
        import re
        links = re.findall(r'href="(/research\?[^"]+)"', html)
        views = [l for l in links if "sort=views" in l]
        self.assertTrue(all("dir=flip" in l for l in views))   # flip link
        mult = [l for l in links if "sort=multiplier" in l]
        self.assertTrue(mult and not any("dir=flip" in l for l in mult))
        flipped = self.get("sort=views&dir=flip")
        self.assertLess(flipped.index("hit UC1 00"), flipped.index("hit UC1 29"))

    def test_dropdown_lists_every_channel_and_paused_ones_only_show_when_picked(self):
        html = self.get()
        self.assertIn("Active One", html)
        self.assertIn("Paused Two (paused)", html)
        self.assertNotIn("hit UC2", html)
        picked = self.get("channel=UC2&per=100")
        self.assertIn("hit UC2", picked)
        self.assertNotIn("hit UC1", picked)


class MakeInChannelTests(PageTests):
    def test_the_make_in_channel_pick_survives_filter_paging_and_tabs(self):
        a = db.create_own_channel(self.conn, "Alpha")
        b = db.create_own_channel(self.conn, "Beta")
        html = self.get(f"own={b}&per=25")
        self.assertIn(f'<option value="{b}" selected>', html)
        self.assertIn(f'name="own" value="{b}"', html)        # filter form
        self.assertIn(f"own={b}", html.split("page 1 of")[1])  # page links
        self.assertIn(f"tab=topics&own={b}", html)
        self.assertIn(f'name="own_channel_id" class="own-id" value="{b}"', html)
        self.assertIn("form.submit()", html)                 # auto-apply
        junk = self.get("own=9999")
        self.assertNotIn('name="own" value="9999"', junk)


if __name__ == "__main__":
    unittest.main()
