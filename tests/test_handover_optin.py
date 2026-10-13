import tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from whisperradar import handover


class OptInTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = SimpleNamespace(db_path=Path(self.tmp.name) / "x.db")

    def ids(self, cid):
        return {t["id"] for t in handover.available_for(self.cfg, cid)}

    def test_new_template_is_listed_but_used_by_no_channel(self):
        tid = handover.save_user_template(self.cfg, "Mine", True, True, [])
        self.assertIn(tid, handover.all_templates(self.cfg))
        self.assertNotIn(tid, self.ids(1))

    def test_channel_opts_in_and_out(self):
        tid = handover.save_user_template(self.cfg, "Mine", True, True, [])
        handover.set_channel_templates(self.cfg, 1, [tid], [1, 2])
        self.assertIn(tid, self.ids(1))
        self.assertNotIn(tid, self.ids(2))
        handover.set_channel_templates(self.cfg, 1, [], [1, 2])
        self.assertNotIn(tid, self.ids(1))

    def test_older_template_for_every_channel_stays_until_unticked(self):
        import json
        handover.template_path(self.cfg).write_text(json.dumps(
            {"old": {"name": "Old", "channels": []}}), encoding="utf-8")
        self.assertIn("old", self.ids(1))
        handover.set_channel_templates(self.cfg, 1, [], [1, 2])
        self.assertNotIn("old", self.ids(1))
        self.assertIn("old", self.ids(2))


if __name__ == "__main__":
    unittest.main()
