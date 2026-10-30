"""Storyboard editor: edit plan.json from the Studio (explainer_storyboard + /explainer/<pid>/plan)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402,F401
from tests.test_explainer_scenes import SRT, plan  # noqa: E402

from whisperradar import db, studio  # noqa: E402
from whisperradar import explainer_storyboard as sbe  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class StoryboardEditorTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        tmp = Path(tempfile.mkdtemp())
        self.cfg.db_path = tmp / "wr.db"
        self.cfg.studio_dir = tmp / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.pid = db.create_production(conn, "T", "general", None, str(tmp / "p"))
        conn.close()
        self.pdir = studio.prod_dir(self.cfg, self.pid)
        self.pdir.mkdir(parents=True, exist_ok=True)
        (self.pdir / "subtitles.srt").write_text(SRT, encoding="utf-8")
        self.plan_path = self.pdir / "plan.json"
        self.plan_path.write_text(json.dumps(plan()), encoding="utf-8")
        self.client = create_app(self.cfg).test_client()

    def form(self, **over):
        rows = sbe.rows(plan())
        d = {"scene_count": str(len(rows))}
        for r in rows:
            p = f"s{r['n']}_"
            d.update({p + "id": r["id"], p + "layout": r["layout"], p + "title": r["title"],
                      p + "footer": r["footer"], p + "start": r["start"], p + "extra": r["extra"],
                      p + "order": str(r["n"] + 1),
                      p + "item_label": [i["label"] for i in r["items"]],
                      p + "item_icon": [i["icon"] for i in r["items"]],
                      p + "item_at": [i["at"] for i in r["items"]],
                      p + "item_extra": [i["extra"] for i in r["items"]]})
        d.update(over)
        return d

    def test_page_lists_the_scenes(self):
        html = self.client.get(f"/explainer/{self.pid}/plan").get_data(as_text=True)
        self.assertIn('value="Fit"', html)
        self.assertIn('value="memory-stick"', html)

    def test_rows_round_trip_without_losing_keys(self):
        base = plan()
        form = self.form()

        class F(dict):
            def getlist(self, k):
                v = self.get(k, [])
                return v if isinstance(v, list) else [v]
        new, errors = sbe.plan_from_form(F(form), base)
        self.assertEqual(errors, [])
        self.assertEqual(new, base)           # sides, theme, points: all kept

    def test_a_reworded_label_is_saved_with_a_backup(self):
        form = self.form(**{"s1_item_label": ["Model file", "Memory"]})
        r = self.client.post(f"/explainer/{self.pid}/plan", data=form)
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:400])
        saved = json.loads(self.plan_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["scenes"][1]["items"][0]["label"], "Model file")
        self.assertTrue((self.pdir / "plan_before_edit.json").is_file())

    def test_a_faulty_edit_is_not_written(self):
        before = self.plan_path.read_text(encoding="utf-8")
        form = self.form(**{"s1_item_icon": ["no-such-icon-xyz", "memory-stick"]})
        r = self.client.post(f"/explainer/{self.pid}/plan", data=form)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Not saved", r.get_data(as_text=True))
        self.assertEqual(self.plan_path.read_text(encoding="utf-8"), before)

    def test_bad_json_in_other_settings_is_reported(self):
        form = self.form(**{"s0_extra": "{not json"})
        r = self.client.post(f"/explainer/{self.pid}/plan", data=form)
        self.assertIn("not valid JSON", r.get_data(as_text=True))

    def test_delete_and_reorder(self):
        base = plan()
        form = self.form(**{"s0_order": "5"})            # scene a moves after b
        class F(dict):
            def getlist(self, k):
                v = self.get(k, [])
                return v if isinstance(v, list) else [v]
        new, _ = sbe.plan_from_form(F(form), base)
        self.assertEqual([s["id"] for s in new["scenes"]], ["b", "a"])
        form = self.form(**{"s0_delete": "on", "s1_delete": "on"})
        _, errors = sbe.plan_from_form(F(form), base)
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
