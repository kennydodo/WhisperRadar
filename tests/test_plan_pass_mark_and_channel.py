"""Two changes:
1. The packaging plan / publish kit / thumbnail judges share one SETTABLE pass
   mark (Settings > Packaging), defaulting to 9.5 (was a hard-coded 8.0).
2. A production's own channel can be reassigned from its page even after it is
   set (the old UI locked it and only offered "leave the production")."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402
from whisperradar import db, packaging, plan as packplan, settings, thumbnails  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class PassMarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def test_default_is_9_5(self):
        self.assertEqual(settings.SPEC_BY_KEY["plan_min_rating"]["default"], 9.5)
        self.assertEqual(settings.load(self.conn)["plan_min_rating"], 9.5)

    def test_all_three_judges_read_the_same_setting(self):
        self.assertEqual(packplan.pass_mark(self.cfg), 9.5)
        self.assertEqual(packaging.pass_mark(self.cfg), 9.5)
        self.assertEqual(thumbnails.pass_mark(self.cfg), 9.5)

    def test_setting_is_honoured(self):
        db.set_setting(self.conn, "plan_min_rating", "8.0")
        self.assertEqual(packplan.pass_mark(self.cfg), 8.0)
        self.assertEqual(thumbnails.pass_mark(self.cfg), 8.0)

    def test_judge_prompt_states_the_bar(self):
        ctx = {"channel": "X", "genre": "g", "transcript": "", "refs": [],
               "source": None, "title": "t"}
        p = packplan.parse_plan({})
        self.assertIn("9.5 or higher",
                      packplan.judge_prompt(ctx, p, [], min_score=9.5))
        # default stays the old fallback for direct/programmatic calls
        self.assertIn("8 or higher", packplan.judge_prompt(ctx, p, []))


class ProductionChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        self.a = db.create_own_channel(self.conn, "Channel A")
        self.b = db.create_own_channel(self.conn, "Channel B")
        self.pid = db.create_production(self.conn, "A video", "general",
                                        None, None)
        db.update_production(self.conn, self.pid, own_channel_id=self.a)
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def test_change_channel_offered_even_when_already_assigned(self):
        html = self.client.get(f"/studio/{self.pid}").get_data(as_text=True)
        self.assertIn(f"/studio/{self.pid}/own-channel", html)
        self.assertIn("Channel B", html)          # the other channel is selectable

    def test_reassigning_a_production_channel(self):
        r = self.client.post(f"/studio/{self.pid}/own-channel",
                             data={"own_channel_id": str(self.b)})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(db.get_production(self.conn, self.pid)["own_channel_id"],
                         self.b)


if __name__ == "__main__":
    unittest.main()
