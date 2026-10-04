"""A shotlist that stops short of the last subtitle cue must never be accepted
as the finished shots stage or reach the paid images stage (a real plan covered
cues 1-460 of 521 and nothing flagged the missing 61).

Run: python -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import autorun, db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


def _srt(n):
    out = []
    for i in range(1, n + 1):
        out.append(f"{i}\n00:00:{i - 1:02d},000 --> 00:00:{i - 1:02d},900\n"
                   f"line {i}\n")
    return "\n".join(out)


def _shotlist(upto):
    shots = [{"asset": f"S{i:02d}_ZI.png", "cues": f"{i}-{i}",
              "motion": "ZI"} for i in range(1, upto + 1)]
    images = [{"file": s["asset"], "prompt": f"p{i}"}
              for i, s in enumerate(shots, 1)]
    return {"style": "s", "images": images, "shots": shots}


class CoverageTests(unittest.TestCase):
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
        (self.pdir / "subtitles.srt").write_text(_srt(30), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _save(self, upto):
        (self.pdir / "shotlist.json").write_text(
            json.dumps(_shotlist(upto)), encoding="utf-8")

    def test_report_names_the_last_covered_cue(self):
        self._save(22)
        rep = studio.shotlist_coverage_report(self.pdir)
        self.assertEqual((rep["cues"], rep["covered_to"]), (30, 22))
        self.assertTrue(rep["faults"])
        self.assertIn("1-22 of 30", autorun.coverage_gap(self.pdir))

    def test_complete_plan_has_no_gap(self):
        self._save(30)
        self.assertIsNone(autorun.coverage_gap(self.pdir))

    def test_no_srt_means_nothing_to_compare(self):
        (self.pdir / "subtitles.srt").unlink()
        self._save(5)
        self.assertIsNone(autorun.coverage_gap(self.pdir))

    def test_plan_pauses_instead_of_skipping_or_rendering(self):
        self._save(22)
        for stage in ("shots", "images"):
            self.assertEqual(
                autorun.stage_action(self.cfg, self.pid, stage)["action"],
                "pause", stage)

    def test_manual_save_is_not_accepted_with_a_gap(self):
        self._save(30)
        client = create_app(self.cfg).test_client()
        resp = client.post(f"/studio/{self.pid}/shotlist/save",
                           data={"shotlist": json.dumps(_shotlist(22))})
        self.assertIn("NOT", resp.headers["Location"])
        conn = db.connect(self.cfg.db_path)
        try:
            steps = [r["stage"] for r in conn.execute(
                "select stage from production_steps where production_id=?",
                (self.pid,))]
        finally:
            conn.close()
        self.assertNotIn("shots", steps)
        ok = client.post(f"/studio/{self.pid}/shotlist/save",
                         data={"shotlist": json.dumps(_shotlist(30))})
        self.assertNotIn("NOT", ok.headers["Location"])

    def test_render_route_refuses_a_gap(self):
        self._save(22)
        client = create_app(self.cfg).test_client()
        resp = client.post(f"/studio/{self.pid}/images/render", data={})
        self.assertIn("Not+rendering", resp.headers["Location"].replace("%20", "+"))


if __name__ == "__main__":
    unittest.main()
