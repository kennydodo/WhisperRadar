"""The publish kit page and its place on the Finished page."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_external_prompts import Base  # noqa: E402
from tests.test_packaging import good_kit, hhmmss, shot  # noqa: E402
from whisperradar import db, packaging as pk  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class KitPageTests(Base):
    def setUp(self):
        super().setUp()
        conn = db.connect(self.cfg.db_path)
        db.update_production(conn, self.pid, status="ready")
        conn.commit()
        conn.close()
        srt = "\n".join(f"{i+1}\n{hhmmss(i*10)} --> {hhmmss(i*10+9)}\nline {i+1}\n"
                        for i in range(30))
        (self.pdir / "subtitles.srt").write_text(srt, "utf-8")
        (self.pdir / "shotlist.json").write_text(json.dumps({"shots": [
            shot(1, 10, "S01"), shot(11, 20, "S02"), shot(21, 30, "S03")],
            "images": []}), "utf-8")
        self.client = create_app(self.cfg).test_client()

    def test_page_without_a_kit_offers_to_generate(self):
        (self.pdir / "script.md").write_text("A script. " * 30, "utf-8")
        html = self.client.get(f"/studio/{self.pid}/kit").get_data(as_text=True)
        self.assertIn("Generate kit", html)
        self.assertNotIn("Checklist", html)

    def test_no_script_no_generate(self):
        html = self.client.get(f"/studio/{self.pid}/kit").get_data(as_text=True)
        self.assertIn("no script yet", html)
        r = self.client.post(f"/studio/{self.pid}/kit/generate", data={})
        self.assertIn("no+script", r.headers["Location"])

    def test_unknown_production(self):
        r = self.client.get("/studio/9999/kit")
        self.assertEqual(r.status_code, 302)

    def test_save_edits_then_page_shows_checks_and_the_full_description(self):
        data = {"title": "How Fast Is a Cheetah Really? The Cheetah Speed Truth",
                "keyword": "cheetah speed",
                "description": "The cheetah speed secret. " * 12,
                "chapter_0": "Start", "chapter_1": "Middle", "chapter_2": "End",
                "hashtags": "cheetah wildlife #animals",
                "tags": "a, b, c, d, e, f",
                "pinned_comment": "Which animal next?"}
        r = self.client.post(f"/studio/{self.pid}/kit/save", data=data)
        self.assertEqual(r.status_code, 302)
        kit = pk.load_kit(self.pdir)
        self.assertEqual(kit["hashtags"], ["#cheetah", "#wildlife", "#animals"])
        self.assertEqual(kit["chapter_titles"], ["Start", "Middle", "End"])
        self.assertEqual(kit["status"], "ready")
        html = self.client.get(f"/studio/{self.pid}/kit").get_data(as_text=True)
        self.assertIn("Checklist", html)
        self.assertIn("0:00 Start", html)
        self.assertIn("1:40 Middle", html)
        self.assertIn("#cheetah #wildlife #animals", html)

    def test_a_kit_with_a_hard_fault_stays_a_draft(self):
        self.client.post(f"/studio/{self.pid}/kit/save", data={
            "title": "T", "keyword": "", "description": "short",
            "hashtags": "one", "tags": "a"})
        self.assertEqual(pk.load_kit(self.pdir)["status"], "draft")

    def test_finished_page_shows_kit_state_and_asks_before_publishing_without(self):
        html = self.client.get("/finished?all=1").get_data(as_text=True)
        self.assertIn("no kit", html)
        self.assertIn("no finished publish kit", html)
        kit = good_kit(3)
        kit["status"] = "ready"
        kit["score"] = 9.1
        pk.save_kit(self.pdir, kit)
        html = self.client.get("/finished?all=1").get_data(as_text=True)
        self.assertIn("kit ready 9.1", html)
        self.assertNotIn("no finished publish kit", html)


if __name__ == "__main__":
    unittest.main()
