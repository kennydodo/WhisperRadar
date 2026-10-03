"""The Studio's "Based on" topic picker greys out topics that THIS channel has
already built, and a second production on the same topic in the same channel
is refused. Per channel: the same source video used by another channel's
production stays free here."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        db.add_channel(self.conn, "Source", "UCsrc")
        self.a = db.create_own_channel(self.conn, "Channel A")
        self.b = db.create_own_channel(self.conn, "Channel B")
        for vid in ("v1", "v2", "v3", "v4"):
            db.upsert_video(self.conn, "UCsrc",
                            {"video_id": vid, "title": f"Topic {vid}"})
            db.set_video(self.conn, vid, status="transcribed")
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _prod(self, source, channel, status="active"):
        pid = db.create_production(self.conn, f"P {source}", "general",
                                   source, None)
        db.update_production(self.conn, pid, own_channel_id=channel,
                             status=status)
        return pid

    def _videos(self):
        return db.get_videos(self.conn, status="transcribed")


class TagTests(_Base):
    def test_a_topic_taken_in_channel_a_is_tagged_for_a_only(self):
        self._prod("v1", self.a)
        self.assertEqual(
            db.source_use_tags(self.conn, self.a, self._videos()),
            {"v1": "in production"})
        self.assertEqual(
            db.source_use_tags(self.conn, self.b, self._videos()), {})

    def test_each_status_has_its_label(self):
        self._prod("v1", self.a, "active")
        self._prod("v2", self.a, "ready")
        self._prod("v3", self.a, "published")
        self._prod("v4", self.a, "failed")
        self.assertEqual(
            db.source_use_tags(self.conn, self.a, self._videos()),
            {"v1": "in production", "v2": "ready", "v3": "published",
             "v4": "failed"})

    def test_an_active_retry_outranks_an_older_failed_one(self):
        self._prod("v1", self.a, "failed")
        self._prod("v1", self.a, "active")
        self.assertEqual(
            db.source_use_tags(self.conn, self.a, self._videos())["v1"],
            "in production")

    def test_a_ready_production_elsewhere_does_not_block_this_channel(self):
        # approving a production sets videos.produced for everyone; the picker
        # must still read the productions, not that flag
        self._prod("v1", self.a, "ready")
        db.set_video(self.conn, "v1", produced=1)
        self.assertEqual(
            db.source_use_tags(self.conn, self.b, self._videos()), {})
        self.assertEqual(
            db.source_use_tags(self.conn, self.a, self._videos()),
            {"v1": "ready"})

    def test_marked_produced_by_hand_is_taken_for_every_channel(self):
        db.set_video(self.conn, "v2", produced=1)
        for ch in (self.a, self.b, 0, None):
            self.assertEqual(
                db.source_use_tags(self.conn, ch, self._videos()),
                {"v2": "marked produced"})

    def test_no_channel_bucket_matches_productions_without_a_channel(self):
        self._prod("v3", None)
        self.assertEqual(
            db.source_use_tags(self.conn, 0, self._videos()),
            {"v3": "in production"})
        self.assertEqual(
            db.source_use_tags(self.conn, self.a, self._videos()), {})


class PageAndCreateTests(_Base):
    def _page(self, channel):
        self.client.post("/studio/channel", data={"own_channel_id": channel})
        return self.client.get("/studio").get_data(as_text=True)

    def test_the_picker_greys_out_only_this_channels_topics(self):
        self._prod("v1", self.a)
        page_a = self._page(self.a)
        self.assertIn('value="v1" data-channel="Source" '
                      'data-used="in production"', page_a)
        self.assertIn('value="v2" data-channel="Source" data-used=""', page_a)
        page_b = self._page(self.b)
        self.assertIn('value="v1" data-channel="Source" data-used=""', page_b)

    def test_a_ready_topic_is_listed_greyed_not_hidden(self):
        self._prod("v1", self.a, "ready")
        db.set_video(self.conn, "v1", produced=1)
        page = self._page(self.a)
        self.assertIn('value="v1"', page)
        self.assertIn('data-used="ready"', page)

    def test_a_duplicate_in_the_same_channel_is_refused(self):
        self._prod("v1", self.a)
        before = len(db.list_productions(self.conn))
        resp = self.client.post("/studio/new", data={
            "title": "Again", "source_video_id": "v1",
            "own_channel_id": str(self.a)})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])
        self.assertIn("already%20has%20a%20production",
                      resp.headers["Location"].replace("+", "%20"))
        self.assertEqual(len(db.list_productions(self.conn)), before)

    def test_the_same_topic_in_another_channel_is_allowed(self):
        self._prod("v1", self.a)
        before = len(db.list_productions(self.conn))
        resp = self.client.post("/studio/new", data={
            "title": "Other channel", "source_video_id": "v1",
            "own_channel_id": str(self.b)})
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("error=", resp.headers["Location"])
        self.assertEqual(len(db.list_productions(self.conn)), before + 1)

    def test_deleting_the_production_frees_the_topic(self):
        pid = self._prod("v1", self.a)
        db.delete_production(self.conn, pid)
        self.assertEqual(
            db.source_use_tags(self.conn, self.a, self._videos()), {})


if __name__ == "__main__":
    unittest.main()
