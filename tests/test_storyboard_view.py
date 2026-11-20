import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402
from whisperradar import db, studio, storyboard_view as sv  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

CUES = {1: "Joe walks into the clinic.", 2: "He pays at the desk.", 3: "Later he cooks."}


def data():
    return {
        "style": "S" * 100,
        "shots": [
            {"cues": "1-2", "asset": "S01_01.png", "scene": "S01", "motion": "ZI",
             "action": "Joe walks into a clinic.", "note": "actual visit"},
            {"cues": "3", "asset": "S01_02.png", "scene": "S01", "motion": "ZO"},
        ],
        "images": [
            {"file": "S01_01.png", "refs": ["CH_JOE"],
             "prompt": "Joe walks into a waiting room. CH_DOG stays outside."},
            {"file": "S01_02.png", "refs": [],
             "prompt": "Joe sits at a kitchen table. " + "x" * 2400},
        ],
    }


class RowTests(unittest.TestCase):
    def build(self, have=()):
        return sv.build_rows(data(), CUES, have)

    def test_narration_joined_from_cue_range(self):
        r = self.build()["rows"][0]
        self.assertEqual(r["narration"], "Joe walks into the clinic. He pays at the desk.")
        self.assertEqual(r["cues"], "1-2")

    def test_missing_action_is_empty_not_invented(self):
        r = self.build()["rows"][1]
        self.assertEqual(r["action"], "")
        self.assertIn("no action written", r["flags"])

    def test_status_only_from_note_words_else_unknown(self):
        rows = self.build()["rows"]
        self.assertEqual(rows[0]["status"], ["actual"])
        self.assertEqual(rows[1]["status"], [])

    def test_place_in_narration_but_not_prompt_is_a_warning(self):
        flags = self.build()["rows"][0]["flags"]
        self.assertTrue(any("clinic" in f and "not in prompt" in f for f in flags))

    def test_named_character_without_ref_is_listed(self):
        self.assertEqual(self.build()["rows"][0]["who_unattached"], ["CH_DOG"])

    def test_sitting_and_limit_flags(self):
        r = self.build()["rows"][1]
        self.assertTrue(any("sitting" in f for f in r["flags"]))
        self.assertTrue(any("limit 2420" in f for f in r["flags"]))
        self.assertEqual(self.build()["summary"]["over_limit"], 1)

    def test_objects_that_sit_are_not_flagged(self):
        d = data()
        d["images"][0]["prompt"] = "A large SUV sits in the lot. Joe walks to the clinic."
        r = sv.build_rows(d, CUES)["rows"][0]
        self.assertFalse(any("sitting" in f for f in r["flags"]))

    def test_image_marked_only_when_the_file_exists(self):
        rows = self.build({"S01_01.png"})["rows"]
        self.assertEqual([r["image"] for r in rows], ["S01_01.png", ""])

    def test_html_escapes_and_shows_unknown(self):
        d = data()
        d["shots"][0]["action"] = "<script>alert(1)</script>"
        page = sv.render_html(sv.build_rows(d, CUES), "T <b>")
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)", page)
        self.assertIn("unknown", page)
        self.assertIn("no action written", page)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.pid = db.create_production(conn, "My Title", "general", None, None)
        conn.commit()
        conn.close()
        self.pdir = studio.prod_dir(self.cfg, self.pid)
        self.pdir.mkdir(parents=True, exist_ok=True)
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def test_no_shotlist_redirects_with_message(self):
        r = self.client.get(f"/studio/{self.pid}/storyboard", follow_redirects=True)
        self.assertIn(b"Create the shotlist first", r.data)

    def test_page_export_and_nothing_written(self):
        (self.pdir / "shotlist.json").write_text(json.dumps(data()), encoding="utf-8")
        (self.pdir / "subtitles.srt").write_text(
            "1\n00:00:00,000 --> 00:00:02,000\nJoe walks into the clinic.\n\n", encoding="utf-8")
        (self.pdir / "images").mkdir()
        (self.pdir / "images" / "S01_01.png").write_bytes(b"x")
        db_conn = db.connect(self.cfg.db_path)
        db.update_production(db_conn, self.pid, stage="shots")
        db_conn.commit()
        db_conn.close()
        stage = self.client.get(f"/studio/{self.pid}?stage=shots")
        self.assertIn(f"/studio/{self.pid}/storyboard".encode(), stage.data)
        before = sorted(p.name for p in self.pdir.rglob("*"))
        r = self.client.get(f"/studio/{self.pid}/storyboard")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"My Title", r.data)
        self.assertIn(f"/studio/file/{self.pid}/images/S01_01.png".encode(), r.data)
        e = self.client.get(f"/studio/{self.pid}/storyboard?export=1")
        self.assertIn("attachment", e.headers["Content-Disposition"])
        self.assertNotIn(b"/studio/file/", e.data)
        self.assertEqual(before, sorted(p.name for p in self.pdir.rglob("*")))


if __name__ == "__main__":
    unittest.main()
