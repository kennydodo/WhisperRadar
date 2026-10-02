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

from whisperradar import db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

ALL_STAGE_FILES = {
    "writing_style.md": "s", "script.md": "s", "audio.mp3": "a",
    "subtitles.srt": "s", "shotlist.json": "{}", "final.mp4": "m",
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
        self.tmp.cleanup()

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
                tmp.cleanup()

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

    def test_reset_from_later_stage_keeps_the_saved_best_plan(self):
        sd = self.pdir / "versions" / "shotlist"
        sd.mkdir(parents=True)
        (sd / "best_ever.json").write_text("x", encoding="utf-8")
        self._post("images")
        self.assertTrue((sd / "best_ever.json").exists())

if __name__ == "__main__":
    unittest.main()
