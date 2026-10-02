"""Channel style/bible are the source of truth; a production can override.

The planner must use the LIVE channel value by default (so editing the channel
propagates to every production), and a production's own file only when it has
explicitly overridden (style_override / bible_override)."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import autorun, db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402

CHANNEL_BIBLE = "channel bible: MAYA the host."
CHANNEL_STYLE = "channel style: soft anime."


class StyleBibleOverrideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.chan = db.create_own_channel(conn, "Ch", bible=CHANNEL_BIBLE,
                                          style=CHANNEL_STYLE)
        self.pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, self.pid, own_channel_id=self.chan)
        conn.commit()
        conn.close()
        self.pdir = studio.prod_dir(self.cfg, self.pid)
        self.pdir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _resolve(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        try:
            prod = db.get_production(conn, self.pid)
            return prod, autorun.style_bible(self.cfg, conn, prod, self.pdir)
        finally:
            conn.close()

    def test_channel_value_wins_by_default(self):
        # no override flags, no production files -> live channel value
        _, (style, ssrc, bible, bsrc) = self._resolve()
        self.assertEqual((style, ssrc), (CHANNEL_STYLE, "channel"))
        self.assertEqual((bible, bsrc), (CHANNEL_BIBLE, "channel"))

    def test_channel_edit_propagates_without_a_file(self):
        # the bible gate must pass on the channel value ALONE (no bible.md)
        _, (_, _, bible, bsrc) = self._resolve()
        self.assertTrue(bible.strip())
        self.assertEqual(bsrc, "channel")
        self.assertFalse((self.pdir / "bible.md").exists())

    def test_production_override_wins(self):
        (self.pdir / "style.md").write_text("production style: watercolor.\n",
                                            encoding="utf-8")
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.update_production(conn, self.pid, style_override=1)
        conn.commit()
        conn.close()
        _, (style, ssrc, _, _) = self._resolve()
        self.assertEqual((style, ssrc), ("production style: watercolor.",
                                         "production"))

    def test_override_off_falls_back_to_channel(self):
        (self.pdir / "style.md").write_text("stale old style.\n",
                                            encoding="utf-8")
        # flag 0 (default) -> the file is ignored, the live channel wins
        _, (style, ssrc, _, _) = self._resolve()
        self.assertEqual((style, ssrc), (CHANNEL_STYLE, "channel"))


class StyleSaveSetsOverrideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.pid = db.create_production(conn, "P", "general", None, None)
        conn.commit()
        conn.close()
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_style_save_writes_writing_guide_only(self):
        # stage 1's manual save is the WRITING guide: it must land in
        # writing_style.md and must not touch the art style or the override flag
        pdir = studio.prod_dir(self.cfg, self.pid)
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "style.md").write_text("art: watercolor\n", encoding="utf-8")
        r = self.client.post(f"/studio/{self.pid}/style/save",
                             data={"style": "my writing style"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual((pdir / "writing_style.md").read_text(encoding="utf-8").strip(),
                         "my writing style")
        self.assertEqual((pdir / "style.md").read_text(encoding="utf-8").strip(),
                         "art: watercolor")
        conn = db.connect(self.cfg.db_path)
        prod = db.get_production(conn, self.pid)
        conn.close()
        self.assertEqual(prod["style_override"] or 0, 0)

    def test_use_channel_clears_override(self):
        conn = db.connect(self.cfg.db_path)
        db.update_production(conn, self.pid, style_override=1)
        conn.commit()
        conn.close()
        self.client.post(f"/studio/{self.pid}/style-bible/use-channel",
                         data={"which": "style"}, follow_redirects=False)
        conn = db.connect(self.cfg.db_path)
        prod = db.get_production(conn, self.pid)
        conn.close()
        self.assertEqual(prod["style_override"], 0)


from whisperradar.webapp import create_app  # noqa: E402

if __name__ == "__main__":
    unittest.main()
