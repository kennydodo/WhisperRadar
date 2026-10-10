"""Studio "Start over": any already-reached stage must be a valid reset
point, not just the 3 that used to be hardcoded (style/script/images) - and
resetting from a stage must actually clear THAT stage's own files.

Before this fix, from_stage was whitelisted to ("style", "script", "images")
only, so a production could not be rewound to just audio, srt, shots, refs or
merge even though the backend's reset mechanics (RESET_FILES + db.STAGES
slicing) already supported any of them. Separately, the "audio" exclusion
filter unconditionally dropped "audio" from the reset list unless with_audio
was set - which would have silently kept the audio file even when the user
picked "start over from audio" itself.

Run: python -m unittest discover -s tests
"""
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

ALL_STAGE_FILES = {
    "writing_style.md": "s", "script.md": "s", "audio.mp3": "a",
    "subtitles.srt": "s", "shotlist.json": "{}", "prompts.txt": "p",
    "final.mp4": "m",
}


class StartOverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        chan = db.create_own_channel(conn, "Ch")
        self.pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, self.pid, own_channel_id=chan,
                             stage="merge")
        for s in ("style", "script", "audio", "srt", "shots", "refs",
                 "images"):
            db.add_step(conn, self.pid, s, "auto", detail="d")
        conn.commit()
        conn.close()
        self.pdir = studio.prod_dir(self.cfg, self.pid)
        self.pdir.mkdir(parents=True, exist_ok=True)
        for name, content in ALL_STAGE_FILES.items():
            (self.pdir / name).write_text(content, encoding="utf-8")
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def _post(self, from_stage, with_audio=False):
        data = {"from": from_stage}
        if with_audio:
            data["with_audio"] = "1"
        return self.client.post(f"/studio/{self.pid}/start-over", data=data)

    def test_review_is_rejected(self):
        resp = self._post("review")
        self.assertIn("error=", resp.headers["Location"])
        self.assertTrue((self.pdir / "writing_style.md").exists())

    def test_unknown_stage_is_rejected(self):
        resp = self._post("not-a-stage")
        self.assertIn("error=", resp.headers["Location"])

    def test_every_reachable_stage_is_now_accepted(self):
        # style/script/images were already accepted before this fix; the new
        # part is audio/srt/shots/refs/merge being valid too
        for stage in ("style", "script", "audio", "srt", "shots", "refs",
                     "images", "merge"):
            with self.subTest(stage=stage):
                tmp = tempfile.TemporaryDirectory()
                cfg = load_config(ROOT / "config.yaml")
                cfg.db_path = Path(tmp.name) / "wr.db"
                cfg.studio_dir = Path(tmp.name) / "studio"
                conn = db.connect(cfg.db_path)
                db.init_db(conn)
                chan = db.create_own_channel(conn, "Ch")
                pid = db.create_production(conn, "P", "general", None, None)
                db.update_production(conn, pid, own_channel_id=chan,
                                     stage="merge")
                conn.commit()
                conn.close()
                resp = create_app(cfg).test_client().post(
                    f"/studio/{pid}/start-over", data={"from": stage})
                self.assertIn("msg=", resp.headers["Location"], stage)
                wr_tmp.cleanup(tmp)

    def test_start_over_from_the_plan_clears_plan_scores_and_title(self):
        import json as _j
        conn = db.connect(self.cfg.db_path)
        db.update_production(conn, self.pid, title="Applied title",
                             warning="script gate failed: x")
        conn.commit()
        conn.close()
        (self.pdir / "packaging_plan.json").write_text(_j.dumps(
            {"title": "Applied title", "applied": True, "score": 8,
             "source_title": "Original title"}), encoding="utf-8")
        (self.pdir / "publish_kit.json").write_text("{}", encoding="utf-8")
        (self.pdir / "research_notes.md").write_text("brief", encoding="utf-8")
        (self.pdir / "research_notes.json").write_text(
            '{"kind":"brief"}', encoding="utf-8")
        resp = self._post("plan")
        self.assertIn("msg=", resp.headers["Location"])
        for name in ("packaging_plan.json", "publish_kit.json",
                     "research_notes.md", "script.md", "shotlist.json"):
            self.assertFalse((self.pdir / name).exists(), name)
        self.assertTrue((self.pdir / "audio.mp3").exists())
        conn = db.connect(self.cfg.db_path)
        prod = db.get_production(conn, self.pid)
        conn.close()
        self.assertEqual(prod["title"], "Original title")
        self.assertEqual(prod["stage"], "style")
        self.assertFalse(prod["warning"])

    def test_start_over_from_the_plan_restores_the_source_video_title(self):
        conn = db.connect(self.cfg.db_path)
        conn.execute("INSERT INTO channels (channel_id, name, genre, active) "
                     "VALUES ('c1','Src','finance',1)")
        conn.execute("INSERT INTO videos (video_id, channel_id, title, url, "
                     "view_count) VALUES ('v1','c1','The Original Title','u',5)")
        db.update_production(conn, self.pid, source_video_id="v1",
                             title="An older plan's title")
        conn.commit()
        conn.close()
        # no plan file at all (an older run's record is gone)
        self._post("plan")
        conn = db.connect(self.cfg.db_path)
        title = db.get_production(conn, self.pid)["title"]
        conn.close()
        self.assertEqual(title, "The Original Title")

    def test_script_reset_forgets_the_saved_chats(self):
        (self.pdir / "webchat_chats.json").write_text("{}", encoding="utf-8")
        self._post("script")
        self.assertFalse((self.pdir / "webchat_chats.json").exists())
        (self.pdir / "webchat_chats.json").write_text("{}", encoding="utf-8")
        self._post("shots")
        self.assertTrue((self.pdir / "webchat_chats.json").exists())

    def test_script_reset_keeps_manual_notes(self):
        (self.pdir / "research_notes.md").write_text("mine", encoding="utf-8")
        (self.pdir / "research_notes.json").write_text(
            '{"manual": true}', encoding="utf-8")
        self._post("script")
        self.assertTrue((self.pdir / "research_notes.md").exists())

    def test_reset_from_refs_keeps_audio_srt_shots(self):
        self._post("refs")
        self.assertTrue((self.pdir / "audio.mp3").exists())
        self.assertTrue((self.pdir / "subtitles.srt").exists())
        self.assertTrue((self.pdir / "shotlist.json").exists())
        self.assertFalse((self.pdir / "final.mp4").exists())

    def test_reset_from_audio_deletes_audio_even_without_with_audio_flag(self):
        # this is the exclusion-filter bug: "audio" must not be treated as
        # "kept by default" when it is itself the reset target
        self._post("audio")
        self.assertFalse((self.pdir / "audio.mp3").exists())
        self.assertFalse((self.pdir / "subtitles.srt").exists())
        self.assertTrue((self.pdir / "writing_style.md").exists())
        self.assertTrue((self.pdir / "script.md").exists())

    def test_reset_from_script_keeps_audio_by_default(self):
        self._post("script")
        self.assertTrue((self.pdir / "audio.mp3").exists())
        self.assertFalse((self.pdir / "script.md").exists())

    def test_reset_from_script_with_audio_flag_also_clears_audio(self):
        self._post("script", with_audio=True)
        self.assertFalse((self.pdir / "audio.mp3").exists())

    def test_reset_from_style_clears_everything(self):
        self._post("style", with_audio=True)
        for name in ALL_STAGE_FILES:
            self.assertFalse((self.pdir / name).exists(), name)


    def test_reset_from_shots_also_clears_the_saved_best_plan(self):
        sd = self.pdir / "versions" / "shotlist"
        sd.mkdir(parents=True)
        for name in ("best_ever.json", "review.json", "brief_used.md"):
            (sd / name).write_text("x", encoding="utf-8")
        named = self.pdir / "versions" / "script"
        named.mkdir(parents=True)
        (named / "keep.md").write_text("k", encoding="utf-8")
        self._post("shots")
        for name in ("best_ever.json", "review.json", "brief_used.md"):
            self.assertFalse((sd / name).exists(), name)
        self.assertFalse((self.pdir / "shotlist.json").exists())
        self.assertTrue((named / "keep.md").exists())   # named versions stay

    def test_reset_from_script_clears_generated_versions_keeps_named(self):
        vd = self.pdir / "versions" / "script"
        vd.mkdir(parents=True)
        for name in ("attempt-1.md", "attempt-2.md", "auto-20261001-120000.md",
                     "review.json", "mine.md"):
            (vd / name).write_text("x", encoding="utf-8")
        self._post("script")
        for name in ("attempt-1.md", "attempt-2.md", "auto-20261001-120000.md",
                     "review.json"):
            self.assertFalse((vd / name).exists(), name)
        self.assertTrue((vd / "mine.md").exists())      # named version stays

    def test_reset_from_later_stage_keeps_script_attempts(self):
        vd = self.pdir / "versions" / "script"
        vd.mkdir(parents=True)
        (vd / "attempt-1.md").write_text("x", encoding="utf-8")
        self._post("shots")
        self.assertTrue((vd / "attempt-1.md").exists())

    def test_reset_from_later_stage_keeps_the_saved_best_plan(self):
        sd = self.pdir / "versions" / "shotlist"
        sd.mkdir(parents=True)
        (sd / "best_ever.json").write_text("x", encoding="utf-8")
        self._post("images")
        self.assertTrue((sd / "best_ever.json").exists())

    def test_modal_preselects_one_stage_at_every_production_stage(self):
        """The reset dialog script reads the checked radio; at "review" none
        was checked, which crashed the script and broke the button."""
        import re
        for stage in db.STAGES:
            conn = db.connect(self.cfg.db_path)
            db.update_production(conn, self.pid, stage=stage)
            conn.commit()
            conn.close()
            html = self.client.get(f"/studio/{self.pid}").get_data(as_text=True)
            radios = re.findall(
                r'<input type="radio" name="resetfrom"[^>]*>', html)
            self.assertTrue(radios, stage)
            self.assertEqual(
                sum(1 for r in radios if "checked" in r), 1, stage)


if __name__ == "__main__":
    unittest.main()
