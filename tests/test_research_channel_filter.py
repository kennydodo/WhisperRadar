"""Research > Outliers: the channel filter is a searchable dropdown grouped by
genre that takes several channels (?channel=a&channel=b); none = all."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from whisperradar import db, outliers  # noqa: E402


def _items():
    base = {"views": 1000, "is_short": False, "genre": "g", "age_days": 10,
            "title": "t", "multiplier": 5.0}
    return [dict(base, video_id=f"v{i}", channel_id=c)
            for i, c in enumerate(["A", "B", "C", "A"])]


class FilterSortTests(unittest.TestCase):
    def ids(self, **kw):
        return sorted(i["video_id"] for i in
                      outliers.filter_sort(_items(), **kw)[0])

    def test_none_one_or_several_channels(self):
        self.assertEqual(self.ids(), ["v0", "v1", "v2", "v3"])
        self.assertEqual(self.ids(channel_id=""), ["v0", "v1", "v2", "v3"])
        self.assertEqual(self.ids(channel_id="A"), ["v0", "v3"])
        self.assertEqual(self.ids(channel_id=["A", "C"]), ["v0", "v2", "v3"])
        self.assertEqual(self.ids(channel_id=[]), ["v0", "v1", "v2", "v3"])


class ResearchChannelPickerTests(unittest.TestCase):
    def setUp(self):
        from tests import wr_tmp
        from whisperradar.config import load_config
        from whisperradar.webapp import create_app
        self.wr_tmp = wr_tmp
        root = Path(__file__).resolve().parents[1]
        self.tmp = tempfile.TemporaryDirectory()
        cfg = load_config(root / "config.yaml")
        cfg.db_path = Path(self.tmp.name) / "wr.db"
        cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        for cid, name, genre in (("UCa", "Alpha Chan", "science"),
                                 ("UCb", "Beta Chan", "science"),
                                 ("UCc", "Gamma Chan", "finance")):
            db.add_channel(conn, name, cid, genre=genre)
            for i in range(8):
                db.upsert_video(conn, cid, {
                    "video_id": f"{cid}-{i}", "title": f"Plain {name} {i}",
                    "url": f"https://youtu.be/{cid}-{i}", "view_count": 1000,
                    "published_at": "2026-01-01T00:00:00Z"})
            db.upsert_video(conn, cid, {
                "video_id": f"{cid}-big", "title": f"Huge {name}",
                "url": f"https://youtu.be/{cid}-big", "view_count": 900000,
                "published_at": "2026-01-01T00:00:00Z"})
        conn.close()
        self.client = create_app(cfg).test_client()

    def tearDown(self):
        self.wr_tmp.cleanup(self.tmp)

    def page(self, qs=""):
        return self.client.get("/research" + qs).get_data(as_text=True)

    def test_picker_is_grouped_by_genre_and_searchable(self):
        html = self.page()
        self.assertIn("data-wsel", html)
        self.assertIn('placeholder="Search channels or genres..."', html)
        self.assertIn("finance", html)
        self.assertIn("science", html)
        self.assertIn('name="channel" value="UCa"', html)

    def test_no_channel_picked_shows_everything(self):
        html = self.page()
        for n in ("Alpha", "Beta", "Gamma"):
            self.assertIn(f"Huge {n} Chan", html)

    def test_several_channels_are_combined(self):
        html = self.page("?channel=UCa&channel=UCc")
        self.assertIn("Huge Alpha Chan", html)
        self.assertIn("Huge Gamma Chan", html)
        self.assertNotIn("Huge Beta Chan", html)
        self.assertRegex(html, r'value="UCa"[^>]*checked')
        self.assertNotRegex(html, r'value="UCb"[^>]*checked')

    def test_a_single_old_style_link_still_works(self):
        html = self.page("?channel=UCb")
        self.assertIn("Huge Beta Chan", html)
        self.assertNotIn("Huge Alpha Chan", html)

    def test_paging_and_sort_links_keep_every_picked_channel(self):
        html = self.page("?channel=UCa&channel=UCb&per=25")
        self.assertIn("channel=UCa&amp;channel=UCb", html.replace("&channel", "&amp;channel")
                      if "&amp;" not in html else html)


if __name__ == "__main__":
    unittest.main()


class DashboardChannelPickerTests(unittest.TestCase):
    def setUp(self):
        from tests import wr_tmp
        from whisperradar.config import load_config
        from whisperradar.webapp import create_app
        self.wr_tmp = wr_tmp
        root = Path(__file__).resolve().parents[1]
        self.tmp = tempfile.TemporaryDirectory()
        cfg = load_config(root / "config.yaml")
        cfg.db_path = Path(self.tmp.name) / "wr.db"
        cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        for cid, name, genre in (("UCa", "Alpha Chan", "science"),
                                 ("UCb", "Beta Chan", "science"),
                                 ("UCc", "Gamma Chan", "finance")):
            db.add_channel(conn, name, cid, genre=genre)
            db.upsert_video(conn, cid, {
                "video_id": f"{cid}-1", "title": f"Video of {name}",
                "url": f"https://youtu.be/{cid}-1", "view_count": 10,
                "published_at": "2026-01-01T00:00:00Z"})
        conn.close()
        self.client = create_app(cfg).test_client()

    def tearDown(self):
        self.wr_tmp.cleanup(self.tmp)

    def page(self, qs=""):
        return self.client.get("/" + qs).get_data(as_text=True)

    def test_all_channels_by_default_and_picker_is_there(self):
        html = self.page()
        self.assertIn("data-wsel", html)
        self.assertIn("data-autosubmit", html)
        for n in ("Alpha", "Beta", "Gamma"):
            self.assertIn(f"Video of {n} Chan", html)

    def test_several_channels(self):
        html = self.page("?channel=UCa&channel=UCc")
        self.assertIn("Video of Alpha Chan", html)
        self.assertIn("Video of Gamma Chan", html)
        self.assertNotIn("Video of Beta Chan", html)

    def test_old_single_channel_link_and_name_still_work(self):
        self.assertNotIn("Video of Alpha Chan", self.page("?channel=UCb"))
        self.assertIn("Video of Beta Chan", self.page("?channel=UCb"))

    def test_export_all_takes_several_channels(self):
        r = self.client.get("/export_all?channel=UCa&channel=UCb")
        self.assertEqual(r.status_code, 200)
