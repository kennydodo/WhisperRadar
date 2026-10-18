import json, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from whisperradar import handover


class AllChannelsTests(unittest.TestCase):
    """Hand-over templates are visible to every channel (no per-channel opt-in)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = SimpleNamespace(db_path=Path(self.tmp.name) / "x.db")

    def ids(self, cid):
        return {t["id"] for t in handover.available_for(self.cfg, cid)}

    def test_new_template_is_offered_to_every_channel(self):
        tid = handover.save_user_template(self.cfg, "Mine", True, True, [])
        for cid in (1, 2, None):
            self.assertIn(tid, self.ids(cid))

    def test_older_template_with_a_channel_list_is_still_offered_to_other_channels(self):
        # the reported bug: a template saved for channels [3, 4] was hidden elsewhere
        handover.template_path(self.cfg).write_text(json.dumps(
            {"old": {"name": "Old", "channels": [3, 4], "optin": True}}), encoding="utf-8")
        for cid in (1, 3, 4, 9, None):
            self.assertIn("old", self.ids(cid))


if __name__ == "__main__":
    unittest.main()
