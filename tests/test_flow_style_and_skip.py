"""FlowBatch job: the art direction is attached consistently, and PL/PR can be
left out of the first pass.

Run: python -m unittest tests.test_flow_style_and_skip
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402


def _cfg(d):
    cfg = load_config(ROOT / "config.yaml")
    cfg.db_path = Path(d) / "wr.db"
    cfg.flowbatch_project_url = None
    return cfg


def _write(pdir, images, style="STYLE-TEXT", have=()):
    (pdir / "images").mkdir(parents=True, exist_ok=True)
    data = {"images": [{"file": f, "prompt": p} for f, p in images]}
    if style is not None:
        data["style"] = style
    (pdir / "shotlist.json").write_text(json.dumps(data), encoding="utf-8")
    for f in have:
        (pdir / "images" / f).write_bytes(b"x")


class SkipMotionTests(unittest.TestCase):
    def test_skip_motion_leaves_pl_pr_out_of_the_job(self):
        with wr_tmp.tempdir() as d:
            pdir = Path(d)
            _write(pdir, [("A1_ST.png", "p1"), ("A2_PL.png", "p2"),
                          ("A3_PR.png", "p3"), ("A4_ZI.png", "p4")])
            _, names = studio.prepare_flowbatch_job(
                _cfg(d), pdir, 1, skip_motion=("PL", "PR"))
            self.assertEqual(names, ["A1_ST.png", "A4_ZI.png"])

    def test_missing_shot_files_filters(self):
        with wr_tmp.tempdir() as d:
            pdir = Path(d)
            _write(pdir, [("A1_ST.png", "p1"), ("A2_PL.png", "p2"),
                          ("A3_PR.png", "p3")], have=["A3_PR.png"])
            self.assertEqual(studio.missing_shot_files(pdir),
                             ["A1_ST.png", "A2_PL.png"])
            self.assertEqual(studio.missing_shot_files(
                pdir, motions=("PL", "PR")), ["A2_PL.png"])
            self.assertEqual(studio.missing_shot_files(
                pdir, exclude=("PL", "PR")), ["A1_ST.png"])


class StyleAttachTests(unittest.TestCase):
    def _job(self, d):
        return json.loads((Path(d) / "flowbatch.json").read_text("utf-8"))

    def test_style_attached_when_it_fits(self):
        with wr_tmp.tempdir() as d:
            _write(Path(d), [("A1_ST.png", "short")])
            studio.prepare_flowbatch_job(_cfg(d), Path(d), 1)
            self.assertEqual(self._job(d)["style"], "STYLE-TEXT")

    def test_decision_does_not_depend_on_what_is_left(self):
        """The long prompt is already rendered; the style must still be judged
        against the whole shotlist, so a resume behaves like the first run."""
        with wr_tmp.tempdir() as d:
            long_prompt = "x" * 2300
            _write(Path(d), [("A1_ST.png", "short"),
                             ("A2_ZI.png", long_prompt)],
                   style="s" * 200, have=["A2_ZI.png"])
            studio.prepare_flowbatch_job(_cfg(d), Path(d), 1)
            self.assertNotIn("style", self._job(d))      # same as run one

    def test_falls_back_to_style_md(self):
        with wr_tmp.tempdir() as d:
            _write(Path(d), [("A1_ST.png", "short")], style=None)
            (Path(d) / "style.md").write_text("FROM STYLE MD\n", "utf-8")
            studio.prepare_flowbatch_job(_cfg(d), Path(d), 1)
            self.assertEqual(self._job(d)["style"], "FROM STYLE MD")

    def test_too_long_is_a_visible_warning_on_the_production(self):
        with wr_tmp.tempdir() as d:
            cfg = _cfg(d)
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            pid = db.create_production(conn, "TEST style warning")
            conn.commit()
            conn.close()
            _write(Path(d), [("A1_ST.png", "x" * 2300)], style="s" * 300)
            studio.prepare_flowbatch_job(cfg, Path(d), pid)
            self.assertNotIn("style", self._job(d))
            conn = db.connect(cfg.db_path)
            warn = db.get_production(conn, pid)["warning"] or ""
            conn.close()
            self.assertIn("Visual style NOT attached", warn)
            # a later fitting run clears it again
            _write(Path(d), [("A1_ST.png", "short")], style="s" * 300)
            studio.prepare_flowbatch_job(cfg, Path(d), pid)
            conn = db.connect(cfg.db_path)
            warn = db.get_production(conn, pid)["warning"]
            conn.close()
            self.assertFalse(warn)


if __name__ == "__main__":
    unittest.main()
