"""Frame density (minimal / moderate / rich): a channel setting with a
per-production override. The planner is told the limit and the shotlist audit
marks prompts that go past it as "too busy". Rich (the default) changes
nothing about the prompts.

Run: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

CHUNK = [{"asset": "S01.png", "cues": "1-1", "prompt": "a hut by a lake"}]
CUES = {1: "A man builds a hut."}


class PromptTests(unittest.TestCase):
    def test_rich_is_exactly_the_old_audit_prompt(self):
        base = studio.alignment_prompt(CHUNK, CUES, style_guide="flat")
        self.assertEqual(base, studio.alignment_prompt(
            CHUNK, CUES, style_guide="flat", density="rich"))
        self.assertNotIn("FRAME DENSITY", base)

    def test_minimal_and_moderate_add_a_too_busy_rule(self):
        mini = studio.alignment_prompt(CHUNK, CUES, density="minimal")
        mod = studio.alignment_prompt(CHUNK, CUES, density="moderate")
        for p in (mini, mod):
            self.assertIn("FRAME DENSITY", p)
            self.assertIn("too busy:", p)
            self.assertIn("short, sparse prompt is GOOD", p)
        self.assertIn("MINIMAL", mini)
        self.assertIn("TWO small supporting objects", mini)
        self.assertIn("MODERATE", mod)
        self.assertIn("FOUR supporting objects", mod)
        self.assertNotEqual(mini, mod)

    def test_the_rule_comes_before_the_reply_format(self):
        p = studio.alignment_prompt(CHUNK, CUES, density="minimal")
        self.assertLess(p.index("FRAME DENSITY"),
                        p.index("Reply with ONLY a JSON object"))

    def test_planner_is_told_the_limit(self):
        base = studio.shotlist_prompt("BRIEF", "1: hi (2s)", "style")
        self.assertEqual(base, studio.shotlist_prompt(
            "BRIEF", "1: hi (2s)", "style", density="rich"))
        mini = studio.shotlist_prompt("BRIEF", "1: hi (2s)", "style",
                                      density="minimal")
        self.assertIn("VISUAL DENSITY: MINIMAL", mini)
        self.assertIn("do not add props", mini)


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.oc = db.create_own_channel(conn, "Ch")
        self.pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, self.pid, own_channel_id=self.oc)
        conn.commit()
        conn.close()
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def _eff(self):
        return studio.visual_density(self.cfg, pid=self.pid)

    def test_default_is_rich(self):
        self.assertEqual(self._eff(), "rich")

    def test_channel_form_saves_and_clears(self):
        def post(v):
            data = {"id": str(self.oc), "name": "Ch"}
            if v is not None:
                data["visual_density"] = v
            self.assertIn(self.client.post("/my-channels/edit",
                                           data=data).status_code, (302, 303))
        post("minimal")
        self.assertEqual(self._eff(), "minimal")
        post("moderate")
        self.assertEqual(self._eff(), "moderate")
        post("bogus")
        self.assertEqual(self._eff(), "rich")

    def test_production_choice_beats_the_channel(self):
        conn = db.connect(self.cfg.db_path)
        db.update_own_channel(conn, self.oc, visual_density="minimal")
        conn.commit()
        conn.close()
        self.assertEqual(self._eff(), "minimal")
        r = self.client.post(f"/studio/{self.pid}/visual-density",
                             data={"visual_density": "moderate"})
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(self._eff(), "moderate")
        self.client.post(f"/studio/{self.pid}/visual-density",
                         data={"visual_density": ""})
        self.assertEqual(self._eff(), "minimal")

    def test_start_over_from_shots_drops_the_production_choice(self):
        conn = db.connect(self.cfg.db_path)
        db.update_own_channel(conn, self.oc, visual_density="minimal")
        db.update_production(conn, self.pid, visual_density="rich",
                             stage="images")
        for s in ("style", "script", "audio", "srt", "shots"):
            db.add_step(conn, self.pid, s, "auto", detail="d")
        conn.commit()
        conn.close()
        self.assertEqual(self._eff(), "rich")
        self.client.post(f"/studio/{self.pid}/start-over",
                         data={"from": "shots"})
        self.assertEqual(self._eff(), "minimal")

    def test_pages_render_the_picker(self):
        self.assertIn(b"visual_density", self.client.get("/my-channels").data)
        conn = db.connect(self.cfg.db_path)
        db.update_production(conn, self.pid, stage="shots")
        conn.commit()
        conn.close()
        self.assertIn(b"visual_density",
                      self.client.get(f"/studio/{self.pid}?stage=shots").data)


if __name__ == "__main__":
    unittest.main()
