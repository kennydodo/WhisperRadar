"""The image batch sheet is built from shotlist.json, never written by the LLM.

The planning brief, the external-LLM planner prompt and the cut-off
continuation prompt ask for the shotlist JSON only; the readable sheet is
generated locally (the Studio's "View batch sheet" link, same format as
before), so it cannot go stale and costs no output tokens.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import briefs, db, external_prompts as ep, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

DATA = {
    "style": "Soft hand-drawn anime-style illustration.",
    "refs": {"CH_REIKO": "refs/CH_REIKO.png"},
    "shots": [
        {"cues": "1-4", "asset": "S01_01_SCN_ZI.png", "scene": "S01",
         "motion": "ZI"},
        {"cues": "5-8", "asset": "S02_01_HYB_PD.png", "scene": "S02",
         "motion": "PD"},
    ],
    "images": [
        {"file": "S01_01_SCN_ZI.png", "prompt": "A packed train car.",
         "refs": ["CH_REIKO", "BG_TRAIN"]},
        {"file": "S02_01_HYB_PD.png", "prompt": "A tall composition."},
    ],
}


class BriefTests(unittest.TestCase):
    def test_the_brief_asks_for_the_json_only(self):
        cfg = load_config(ROOT / "config.yaml")
        for profile in (briefs.STANDARD, briefs.get_profile("static")):
            text = studio.load_manifest_brief(cfg, profile)
            self.assertNotIn("DOCUMENT 1", text)
            self.assertNotIn("IMAGE BATCH SHEET", text)
            self.assertNotIn("two documents", text)
            self.assertIn("exactly one document", text)
            # the canvas sizes stay: the images are composed for them
            self.assertIn("2304x1296", text)

    def test_the_cut_off_prompt_does_not_ask_for_a_sheet(self):
        text = studio.continuation_prompt("base", '{"shots": [')
        self.assertNotIn("BATCH SHEET", text)
        self.assertIn("Write nothing after the JSON", text)

    def test_an_old_reply_that_still_has_a_sheet_parses(self):
        reply = (json.dumps(DATA) + "\n\n=== DOCUMENT 1: IMAGE BATCH SHEET ===\n"
                 "=== MASTER PROMPT ===\nx")
        data, tail = studio.parse_shotlist_output(reply)
        self.assertEqual(len(data["shots"]), 2)
        self.assertIn("IMAGE BATCH SHEET", tail)


class SheetTextTests(unittest.TestCase):
    def setUp(self):
        self.text = studio.batch_sheet_text(DATA)

    def test_master_prompt_and_canvas_spec(self):
        self.assertIn("=== MASTER PROMPT ===\n" + DATA["style"], self.text)
        self.assertIn("ZI: 2304x1296", self.text)
        self.assertIn("PD: 2304x2160", self.text)
        self.assertNotIn("PV: 3840", self.text)   # only the codes in use

    def test_images_are_grouped_by_beat_with_size_and_refs(self):
        self.assertIn("=== S01 ===\nS01_01_SCN_ZI.png [2304x1296] — "
                      "A packed train car. · refs: CH_REIKO, BG_TRAIN",
                      self.text)
        self.assertIn("=== S02 ===\nS02_01_HYB_PD.png [2304x2160] — "
                      "A tall composition.\n", self.text)

    def test_every_image_appears_once(self):
        for img in DATA["images"]:
            self.assertEqual(self.text.count(img["file"]), 1)


class PlannerPromptAndRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        chan = db.create_own_channel(conn, "Ch")
        self.pid = db.create_production(conn, "Coin Jar", "finance", None, None)
        db.update_production(conn, self.pid, own_channel_id=chan)
        conn.commit()
        conn.close()
        self.pdir = studio.prod_dir(self.cfg, self.pid)
        self.pdir.mkdir(parents=True, exist_ok=True)
        (self.pdir / "subtitles.srt").write_text(
            "1\n00:00:00,000 --> 00:00:05,000\nHello.\n\n"
            "2\n00:00:05,000 --> 00:00:10,000\nWorld.\n", "utf-8")
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_external_planner_prompt_asks_for_json_only(self):
        for files in (None, []):
            text = ep.shotlist_planner_prompt(self.cfg, self.pid, files)
            self.assertIn("Write nothing after the JSON", text)
            self.assertNotIn("(Section 7), put it AFTER", text)

    def test_view_batch_sheet_is_built_from_the_shotlist(self):
        (self.pdir / "shotlist.json").write_text(json.dumps(DATA), "utf-8")
        # a stale sheet from an older run must not be served
        (self.pdir / "batch_sheet.txt").write_text("STALE", "utf-8")
        resp = self.client.get(f"/studio/file/{self.pid}/batch_sheet.txt")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("S01_01_SCN_ZI.png [2304x1296]", body)
        self.assertNotIn("STALE", body)

    def test_no_shotlist_means_no_sheet_link(self):
        self.assertEqual(self.client.get(
            f"/studio/file/{self.pid}/batch_sheet.txt").status_code, 404)


if __name__ == "__main__":
    unittest.main()
