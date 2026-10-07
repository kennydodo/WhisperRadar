"""'Clear all' on the saved-versions lists removes every listed version."""
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


class ClearVersionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        chan = db.create_own_channel(conn, "Ch")
        self.pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, self.pid, own_channel_id=chan, stage="script")
        conn.commit()
        conn.close()
        self.pdir = studio.prod_dir(self.cfg, self.pid)
        self.pdir.mkdir(parents=True, exist_ok=True)
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def _put(self, *parts):
        f = self.pdir.joinpath("versions", *parts)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x", encoding="utf-8")
        return f

    def _clear(self, **data):
        return self.client.post(f"/studio/{self.pid}/versions/clear", data=data)

    def test_clears_all_script_versions_only(self):
        files = [self._put("script", n) for n in ("a.md", "b.md", "attempt-1.md")]
        direction = self._put("direction", "script", "d.md")
        self._clear(kind="script")
        for f in files:
            self.assertFalse(f.exists(), f.name)
        self.assertTrue(direction.exists())
        self.assertTrue(self.pdir.joinpath("versions", "script").is_dir())

    def test_clears_only_that_stages_direction(self):
        mine = self._put("direction", "script", "d1.md")
        other = self._put("direction", "shots", "d2.md")
        self._clear(kind="direction", stage="script")
        self.assertFalse(mine.exists())
        self.assertTrue(other.exists())

    def test_rejects_bad_kind_and_stage_and_empty(self):
        keep = self._put("script", "keep.md")
        for data in ({"kind": "bogus"}, {"kind": "direction", "stage": "nope"}):
            self.assertEqual(self._clear(**data).status_code, 302)
        self.assertTrue(keep.exists())
        keep.unlink()
        r = self._clear(kind="script")
        self.assertIn("No%20saved%20versions", r.headers["Location"])

    def test_buttons_rendered_when_versions_exist(self):
        self._put("script", "a.md")
        html = self.client.get(f"/studio/{self.pid}").get_data(as_text=True)
        self.assertIn(f"/studio/{self.pid}/versions/clear", html)
        self.assertIn("Clear all", html)

    def _set_script(self, warning):
        conn = db.connect(self.cfg.db_path)
        db.update_production(conn, self.pid, warning=warning)
        db.add_step(conn, self.pid, "script", "auto", detail="d")
        conn.commit()
        conn.close()
        script = self.pdir / "script.md"
        script.write_text("rejected draft\n", encoding="utf-8")
        return script

    def test_clearing_also_drops_a_rejected_script_and_its_review(self):
        script = self._set_script("script gate failed after 5 attempt(s)")
        review = self._put("script", "review.json")
        self._put("script", "attempt-1.md")
        self._clear(kind="script")
        self.assertFalse(script.exists())
        self.assertFalse(review.exists())
        conn = db.connect(self.cfg.db_path)
        self.assertNotIn("script", db.latest_steps(conn, self.pid))
        self.assertFalse(db.get_production(conn, self.pid)["warning"])
        conn.close()

    def test_clearing_keeps_an_accepted_script(self):
        script = self._set_script("")
        self._put("script", "attempt-1.md")
        self._clear(kind="script")
        self.assertTrue(script.exists())


if __name__ == "__main__":
    unittest.main()
