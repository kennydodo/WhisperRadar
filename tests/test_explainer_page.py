"""The /explainer page: import, validate, storyboard, preview routes."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402,F401

from whisperradar import db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

DOC = {
    "schema_version": 1,
    "theme": "finance_dark",
    "canvas": {"width": 640, "height": 360},
    "scenes": [{
        "id": "s1", "shot_id": "S01_01", "cue_range": [1, 1],
        "objects": [{"id": "t1", "type": "text", "text": "Hi",
                     "box": [0.1, 0.1, 0.8, 0.2], "style": "heading"}],
        "events": [{"target": "t1", "action": "fade_in",
                    "at": {"cue": 1, "edge": "start"}, "duration_ms": 200}],
    }],
}

SRT = "1\n00:00:00,000 --> 00:00:02,000\none\n"


class ExplainerPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.pid = db.create_production(conn, "P", "general", None, None)
        conn.close()
        self.pdir = studio.prod_dir(self.cfg, self.pid)
        self.pdir.mkdir(parents=True, exist_ok=True)
        app = create_app(self.cfg)
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def _seed(self, doc=DOC):
        (self.pdir / "subtitles.srt").write_text(SRT, encoding="utf-8")
        (self.pdir / "explainer.json").write_text(
            json.dumps(doc), encoding="utf-8")

    def test_page_shows_empty_state(self):
        r = self.client.get(f"/explainer?pid={self.pid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"No explainer.json yet", r.data)

    def test_import_rejects_invalid_before_writing(self):
        good = json.dumps(DOC)
        (self.pdir / "explainer.json").write_text(good, encoding="utf-8")
        r = self.client.post(f"/explainer/{self.pid}/import", data={
            "text": json.dumps({"schema_version": 9})}, follow_redirects=True)
        self.assertIn(b"schema_version must be one of 1, 2", r.data)
        self.assertEqual((self.pdir / "explainer.json").read_text(encoding="utf-8"),
                         good)   # previous file untouched

    def test_import_writes_valid_file(self):
        r = self.client.post(f"/explainer/{self.pid}/import", data={
            "text": json.dumps(DOC)}, follow_redirects=True)
        self.assertIn(b"Imported explainer.json", r.data)
        self.assertIn("s1", (self.pdir / "explainer.json").read_text(encoding="utf-8"))
        self.assertFalse((self.pdir / ".explainer-probe.json").exists())

    def test_validate_reports_cue_errors(self):
        bad = json.dumps(DOC)
        doc = json.loads(bad)
        doc["scenes"][0]["cue_range"] = [1, 7]
        (self.pdir / "subtitles.srt").write_text(SRT, encoding="utf-8")
        (self.pdir / "explainer.json").write_text(json.dumps(doc), encoding="utf-8")
        r = self.client.post(f"/explainer/{self.pid}/validate",
                             follow_redirects=True)
        self.assertIn(b"exceeds the 1 cues", r.data)

    def test_validate_ok(self):
        self._seed()
        r = self.client.post(f"/explainer/{self.pid}/validate",
                             follow_redirects=True)
        self.assertIn(b"Valid.", r.data)

    def test_unknown_production_404(self):
        self.assertEqual(self.client.get("/explainer?pid=99999").status_code, 404)


if __name__ == "__main__":
    unittest.main()
