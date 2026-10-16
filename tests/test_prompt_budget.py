"""Which image prompts are too long for Flow, named one by one.

Flow takes the style and one image prompt together (2420 characters). A prompt
over the budget used to make the style silently drop from every image, with a
warning that gave only the longest length. Now the shotlist check and the
production warning list the exact prompts.

Run: python -m unittest tests.test_prompt_budget
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402
from tests.test_flow_style_and_skip import _cfg, _write  # noqa: E402

from whisperradar import db, studio  # noqa: E402


def _data(prompts, style="s" * 1800):
    imgs, shots = [], []
    for i, p in enumerate(prompts):
        name = f"S01_{i + 1:02d}_SCN_PR.png"
        imgs.append({"file": name, "prompt": p})
        shots.append({"cues": str(i + 1), "asset": name, "scene": "S01",
                      "motion": "PR"})
    out = {"shots": shots, "images": imgs}
    if style:
        out["style"] = style
    return out


class BudgetTests(unittest.TestCase):
    def test_the_budget_is_the_flow_limit_minus_style_and_a_space(self):
        budget, over = studio.prompts_over_budget(_data(["a"], style="s" * 1800))
        self.assertEqual(budget, 2420 - 1800 - 1)
        self.assertEqual(over, [])

    def test_no_style_leaves_the_whole_limit(self):
        budget, _ = studio.prompts_over_budget(_data(["a"], style=""))
        self.assertEqual(budget, 2420)

    def test_over_budget_prompts_are_listed_worst_first(self):
        data = _data(["x" * 700, "short", "y" * 900, "z" * 621])
        budget, over = studio.prompts_over_budget(data)
        self.assertEqual(budget, 619)
        self.assertEqual([o[0] for o in over],
                         ["S01_03_SCN_PR.png", "S01_01_SCN_PR.png",
                          "S01_04_SCN_PR.png"])
        self.assertEqual(over[0], ("S01_03_SCN_PR.png", 900, 281))

    def test_text_shows_the_worst_few_and_counts_the_rest(self):
        over = [(f"S{i}.png", 700 + i, 80 + i) for i in range(8)]
        text = studio.over_budget_text(619, over, shown=3)
        self.assertIn("S0.png 700 chars (80 over)", text)
        self.assertTrue(text.endswith("and 5 more"))


class FaultTests(unittest.TestCase):
    def test_a_too_long_prompt_is_a_named_shotlist_fault(self):
        data = _data(["short", "x" * 800])
        faults = studio.shotlist_structural_faults(data, 2)
        hit = [f for f in faults if "too long for Flow" in f]
        self.assertEqual(len(hit), 1, faults)
        self.assertIn("S01_02_SCN_PR.png 800 chars (181 over)", hit[0])
        self.assertIn("leaves 619 per prompt", hit[0])
        self.assertNotIn("S01_01_SCN_PR.png", hit[0])

    def test_prompts_that_fit_are_not_a_fault(self):
        faults = studio.shotlist_structural_faults(
            _data(["x" * 619, "short"]), 2)
        self.assertFalse(any("too long for Flow" in f for f in faults), faults)


class WarningTests(unittest.TestCase):
    def test_the_production_warning_names_the_prompts(self):
        with wr_tmp.tempdir() as d:
            cfg = _cfg(d)
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            pid = db.create_production(conn, "TEST budget warning")
            conn.commit()
            conn.close()
            _write(Path(d), [("A1_ST.png", "short"),
                             ("A2_ZI.png", "x" * 800)], style="s" * 1800)
            studio.prepare_flowbatch_job(cfg, Path(d), pid)
            conn = db.connect(cfg.db_path)
            warn = db.get_production(conn, pid)["warning"] or ""
            conn.close()
            self.assertIn("Visual style NOT attached", warn)
            self.assertIn("A2_ZI.png 800 chars (181 over)", warn)
            self.assertNotIn("A1_ST.png", warn)


if __name__ == "__main__":
    unittest.main()
