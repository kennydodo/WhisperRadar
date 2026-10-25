"""Production folder layout: the path lookup, the migration and its button."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db, prodfiles  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


def old_production(pdir: Path) -> None:
    """A production folder as the app used to leave it."""
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / ".whisperradar-production").write_text("")
    for name in ("flowbatch.json", "flow_prepare.json", "refs_project.json",
                 "motion_job.json", "publish_kit.json", "packaging_plan.json",
                 "webchat_chats.json", "titles.txt"):
        (pdir / name).write_text('{"a": 1}\n' if name.endswith(".json") else "t\n")
    (pdir / "shotlist.json").write_text("{}")             # renderer input
    (pdir / "audio.mp3").write_bytes(b"A" * 5000)
    (pdir / "audio").mkdir()
    (pdir / "audio" / "narration.mp3").write_bytes(b"A" * 5000)
    for folder in ("flow_images", "images"):
        (pdir / folder).mkdir()
        (pdir / folder / "S01_01_SCN_ZI.png").write_bytes(b"P" * 4000)
    (pdir / "images" / "S01_02_SCN_ZO.png").write_bytes(b"Q" * 10)   # only here
    (pdir / "webchat_debug").mkdir()
    (pdir / "webchat_debug" / "plan_reply_1.txt").write_text("x")
    (pdir / "thumbnails").mkdir()
    (pdir / "thumbnails" / "t1.png").write_bytes(b"T")
    (pdir / "thumbnails" / "thumbs.json").write_text(json.dumps(
        {"concepts": [{"final": "thumbnails/t1.png"}]}))
    (pdir / "out2").mkdir()
    (pdir / "out2" / "x.mp4").write_bytes(b"V")
    (pdir / "shotlist.json.pre-ai_motion.bak").write_text("{}")


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.pdir = Path(tempfile.mkdtemp()) / "prod"
        old_production(self.pdir)

    def test_old_files_are_found_where_they_are(self):
        self.assertEqual(prodfiles.path(self.pdir, "flowbatch.json"), self.pdir / "flowbatch.json")
        self.assertEqual(prodfiles.path(self.pdir, "motion_report.json"),
                         self.pdir / "motion" / "motion_report.json")   # nothing yet: new place

    def test_migration_groups_files_and_is_repeatable(self):
        self.assertTrue(prodfiles.needs_migration(self.pdir))
        done = prodfiles.migrate(self.pdir)
        self.assertTrue(done)
        for name, group in (("flowbatch.json", "flow"), ("flow_prepare.json", "flow"),
                            ("motion_job.json", "motion"), ("publish_kit.json", "packaging"),
                            ("titles.txt", "packaging"), ("webchat_chats.json", "_work")):
            self.assertTrue((self.pdir / group / name).is_file(), name)
            self.assertFalse((self.pdir / name).exists(), name)
        self.assertTrue((self.pdir / "packaging" / "thumbnails" / "t1.png").is_file())
        self.assertTrue((self.pdir / "_work" / "webchat_debug" / "plan_reply_1.txt").is_file())
        self.assertTrue((self.pdir / "shotlist.json").is_file())   # renderer input stays
        self.assertEqual(prodfiles.path(self.pdir, "flowbatch.json"),
                         self.pdir / "flow" / "flowbatch.json")
        self.assertFalse(prodfiles.needs_migration(self.pdir))
        self.assertEqual(prodfiles.migrate(self.pdir), [])

    def test_leftovers_are_kept_not_deleted_and_thumb_paths_follow(self):
        prodfiles.migrate(self.pdir)
        self.assertTrue((self.pdir / "_work" / "old" / "out2" / "x.mp4").is_file())
        self.assertTrue((self.pdir / "_work" / "old" / "shotlist.json.pre-ai_motion.bak").is_file())
        thumbs = json.loads((self.pdir / "packaging" / "thumbnails" / "thumbs.json").read_text())
        self.assertEqual(thumbs["concepts"][0]["final"], "packaging/thumbnails/t1.png")

    def test_duplicates_share_one_copy_and_other_files_are_untouched(self):
        prodfiles.migrate(self.pdir)
        if not hasattr(os, "link"):
            self.skipTest("no hard links")
        self.assertTrue(os.path.samefile(self.pdir / "images" / "S01_01_SCN_ZI.png",
                                         self.pdir / "flow_images" / "S01_01_SCN_ZI.png"))
        self.assertTrue(os.path.samefile(self.pdir / "audio" / "narration.mp3",
                                         self.pdir / "audio.mp3"))
        self.assertEqual((self.pdir / "images" / "S01_02_SCN_ZO.png").read_bytes(), b"Q" * 10)

    def test_different_content_is_not_linked(self):
        (self.pdir / "images" / "S01_01_SCN_ZI.png").write_bytes(b"E" * 4000)   # edited
        prodfiles.migrate(self.pdir)
        self.assertEqual((self.pdir / "images" / "S01_01_SCN_ZI.png").read_bytes(), b"E" * 4000)
        self.assertEqual((self.pdir / "flow_images" / "S01_01_SCN_ZI.png").read_bytes(), b"P" * 4000)

    def test_different_copies_alone_do_not_need_migration(self):
        import shutil
        pdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, pdir)
        for folder, content in (("images", b"edited"), ("flow_images", b"source")):
            (pdir / folder).mkdir()
            (pdir / folder / "shot.png").write_bytes(content)
        self.assertFalse(prodfiles.needs_migration(pdir))
        self.assertEqual(prodfiles.migrate(pdir), [])


class ButtonTests(unittest.TestCase):
    def test_button_shows_for_an_old_folder_and_migrates_it(self):
        cfg = load_config(ROOT / "config.yaml")
        base = Path(tempfile.mkdtemp())
        cfg.db_path = base / "wr.db"
        pdir = base / "prod"
        old_production(pdir)
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        pid = db.create_production(conn, "T")
        db.update_production(conn, pid, work_dir=str(pdir))
        conn.commit()
        conn.close()
        client = create_app(cfg).test_client()
        page = client.get(f"/studio/{pid}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Tidy folder", page.get_data(as_text=True))
        resp = client.post(f"/studio/{pid}/migrate-layout")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("msg=", resp.headers["Location"])
        self.assertTrue((pdir / "flow" / "flowbatch.json").is_file())
        page = client.get(f"/studio/{pid}")
        self.assertNotIn("Tidy folder", page.get_data(as_text=True))
        resp = client.post("/studio/migrate-all")
        self.assertEqual(resp.status_code, 302)


if __name__ == "__main__":
    unittest.main()
