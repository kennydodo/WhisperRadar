"""Finished productions move to their own page instead of staying mixed into
the in-progress Studio list.

Before this, an approved (status="ready") production just got a green badge
and stayed in the same flat /studio grid as everything still in progress -
there was no dedicated place to browse finished work by channel or date, and
no way to tell at a glance how many were waiting to be published.

Run: python -m unittest discover -s tests
"""

# Also covers the "published" state added on top of "ready": a production
# only says "ready to publish" until you explicitly mark it published (with
# an undo), and the source video's own reenable button works the same way
# regardless of which of the two states the production is in.
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class FinishedPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        self.chan_a = db.create_own_channel(self.conn, "Channel A")
        self.chan_b = db.create_own_channel(self.conn, "Channel B")
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _make(self, title, own_channel_id, status="active",
              source_video_id=None):
        pid = db.create_production(self.conn, title, "general",
                                   source_video_id, None)
        db.update_production(self.conn, pid, own_channel_id=own_channel_id,
                             status=status)
        return pid

    def test_a_ready_production_is_not_on_the_studio_page(self):
        pid = self._make("Ready One", self.chan_a, status="ready")
        resp = self.client.get("/studio")
        self.assertNotIn(b"Ready One", resp.data)

    def test_an_active_production_stays_on_the_studio_page(self):
        self._make("Still Working", self.chan_a, status="active")
        self.client.post("/studio/channel",
                         data={"own_channel_id": self.chan_a})
        resp = self.client.get("/studio")
        self.assertIn(b"Still Working", resp.data)

    def test_the_studio_page_links_to_finished_with_a_count(self):
        self._make("Ready One", self.chan_a, status="ready")
        self._make("Ready Two", self.chan_a, status="ready")
        self.client.post("/studio/channel",
                         data={"own_channel_id": self.chan_a})
        resp = self.client.get("/studio")
        self.assertIn(b"2 finished", resp.data)

    def test_a_ready_production_shows_up_on_the_finished_page(self):
        self._make("Ready One", self.chan_a, status="ready")
        resp = self.client.get("/finished")
        self.assertIn(b"Ready One", resp.data)

    def test_an_active_production_is_not_on_the_finished_page(self):
        self._make("Still Working", self.chan_a, status="active")
        resp = self.client.get("/finished")
        self.assertNotIn(b"Still Working", resp.data)

    def test_sorting_by_channel_orders_alphabetically(self):
        self._make("From B", self.chan_b, status="ready")
        self._make("From A", self.chan_a, status="ready")
        resp = self.client.get("/finished?sort=channel&dir=asc")
        body = resp.data.decode()
        self.assertLess(body.index("From A"), body.index("From B"))

    def test_sorting_by_channel_desc_reverses_the_order(self):
        self._make("From B", self.chan_b, status="ready")
        self._make("From A", self.chan_a, status="ready")
        resp = self.client.get("/finished?sort=channel&dir=desc")
        body = resp.data.decode()
        self.assertLess(body.index("From B"), body.index("From A"))

    def test_an_unknown_sort_value_falls_back_to_date_without_erroring(self):
        self._make("Ready One", self.chan_a, status="ready")
        resp = self.client.get("/finished?sort=nonsense")
        self.assertEqual(resp.status_code, 200)

    def test_reenable_button_only_shown_when_a_source_video_exists(self):
        self._make("No Source", self.chan_a, status="ready",
                  source_video_id=None)
        self._make("Has Source", self.chan_a, status="ready",
                  source_video_id="vid123")
        resp = self.client.get("/finished")
        body = resp.data.decode()
        self.assertIn('value="vid123"', body)
        # "No Source"'s row must not render a reenable form at all.
        no_source_section = body.split("No Source")[1].split("<tr>")[0]
        self.assertNotIn("reenable", no_source_section)

    def test_publish_moves_a_ready_production_to_published(self):
        pid = self._make("Ready One", self.chan_a, status="ready")
        resp = self.client.post(f"/studio/{pid}/publish")
        self.assertIn("msg=", resp.headers["Location"])
        prod = db.get_production(self.conn, pid)
        self.assertEqual(prod["status"], "published")
        self.assertIsNotNone(prod["published_at"])

    def test_publish_is_rejected_for_a_non_ready_production(self):
        pid = self._make("Still Working", self.chan_a, status="active")
        resp = self.client.post(f"/studio/{pid}/publish")
        self.assertIn("error=", resp.headers["Location"])
        prod = db.get_production(self.conn, pid)
        self.assertEqual(prod["status"], "active")

    def test_unpublish_moves_a_published_production_back_to_ready(self):
        pid = self._make("Live One", self.chan_a, status="published")
        resp = self.client.post(f"/studio/{pid}/unpublish")
        self.assertIn("msg=", resp.headers["Location"])
        prod = db.get_production(self.conn, pid)
        self.assertEqual(prod["status"], "ready")
        self.assertIsNone(prod["published_at"])

    def test_unpublish_is_rejected_for_a_ready_production(self):
        pid = self._make("Ready One", self.chan_a, status="ready")
        resp = self.client.post(f"/studio/{pid}/unpublish")
        self.assertIn("error=", resp.headers["Location"])
        prod = db.get_production(self.conn, pid)
        self.assertEqual(prod["status"], "ready")

    def test_a_published_production_still_counts_as_finished_not_in_studio(self):
        self._make("Live One", self.chan_a, status="published")
        self.client.post("/studio/channel",
                         data={"own_channel_id": self.chan_a})
        resp = self.client.get("/studio")
        self.assertNotIn(b"Live One", resp.data)
        self.assertIn(b"1 finished", resp.data)

    def test_published_production_appears_on_the_finished_page(self):
        self._make("Live One", self.chan_a, status="published")
        resp = self.client.get("/finished")
        self.assertIn(b"Live One", resp.data)
        self.assertIn(b"published", resp.data)

    def test_status_filter_ready_excludes_published(self):
        self._make("Ready One", self.chan_a, status="ready")
        self._make("Live One", self.chan_a, status="published")
        resp = self.client.get("/finished?status=ready")
        body = resp.data.decode()
        self.assertIn("Ready One", body)
        self.assertNotIn("Live One", body)

    def test_status_filter_published_excludes_ready(self):
        self._make("Ready One", self.chan_a, status="ready")
        self._make("Live One", self.chan_a, status="published")
        resp = self.client.get("/finished?status=published")
        body = resp.data.decode()
        self.assertIn("Live One", body)
        self.assertNotIn("Ready One", body)

    def test_status_filter_all_shows_both(self):
        self._make("Ready One", self.chan_a, status="ready")
        self._make("Live One", self.chan_a, status="published")
        resp = self.client.get("/finished?status=all")
        body = resp.data.decode()
        self.assertIn("Ready One", body)
        self.assertIn("Live One", body)


if __name__ == "__main__":
    unittest.main()
