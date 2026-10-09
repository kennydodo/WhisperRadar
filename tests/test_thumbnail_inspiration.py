"""The web-chat thumbnail generator is shown the source video's original
thumbnail (plus top outliers) as visual inspiration."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_external_prompts import Base  # noqa: E402
from tests.test_thumbnails import three, reply, concept  # noqa: E402
from whisperradar import db, thumbnails as th  # noqa: E402

SRC = "srcvid0000000000000000000000"   # a 22-char-ish video id is fine


class RecFake:
    def __init__(self, replies):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.calls = []

    def ask(self, site, prompt, files=(), new_chat=True, ready=None):
        self.calls.append({"site": site, "prompt": prompt,
                           "files": list(files), "new_chat": new_chat})
        return self.replies[site].pop(0)

    def set_stop(self, fn):
        pass


class InspirationPathsTests(Base):
    def setUp(self):
        super().setUp()
        conn = db.connect(self.cfg.db_path)
        db.update_production(conn, self.pid, source_video_id=SRC)
        conn.execute(
            "INSERT INTO channels (name, channel_id, kind) VALUES (?,?,?)",
            ("SrcChan", "UCsrc", "primary"))
        conn.execute(
            "INSERT INTO videos (video_id, channel_id, title, url, view_count,"
            " status) VALUES (?,?,?,?,?,?)",
            (SRC, "UCsrc", "10 Japanese Habits to Never Have a Messy Home",
             "u", 12000, "transcribed"))
        conn.commit()
        conn.close()
        self.d = th.thumbs_dir(self.pdir) / th.INSPIRATION_DIR
        self.d.mkdir(parents=True, exist_ok=True)
        (self.d / f"{SRC}.jpg").write_bytes(b"\xff\xd8" + b"0" * 3000)

    def test_source_first(self):
        paths = th.inspiration_paths(self.cfg, self.pid)
        self.assertEqual(paths, [str(self.d / f"{SRC}.jpg")])

    def test_run_concepts_uploads_the_original_thumbnail(self):
        (self.pdir / "script.md").write_text("coins " * 30, "utf-8")
        t = RecFake({"zai": [reply(three())],
                     "deepseek": [json.dumps({"score": 9.6, "pass": True,
                                              "faults": [], "fixes": []})]})
        th.run_concepts(self.cfg, self.pid, t, writer="zai",
                        judge="deepseek", log=lambda m: None)
        writer_call = t.calls[0]
        self.assertIn(str(self.d / f"{SRC}.jpg"), writer_call["files"])
        self.assertIn("ATTACHED IMAGES", writer_call["prompt"])
        self.assertIn("ORIGINAL thumbnail", writer_call["prompt"])

    def test_no_inspiration_no_note_no_uploads(self):
        for p in (self.d / f"{SRC}.jpg",):
            p.unlink()
        (self.pdir / "script.md").write_text("coins " * 30, "utf-8")
        t = RecFake({"zai": [reply(three())],
                     "deepseek": [json.dumps({"score": 9.6, "pass": True,
                                              "faults": [], "fixes": []})]})
        th.run_concepts(self.cfg, self.pid, t, writer="zai",
                        judge="deepseek", log=lambda m: None)
        self.assertEqual(t.calls[0]["files"], [])
        self.assertNotIn("ATTACHED IMAGES", t.calls[0]["prompt"])


class ConceptsJobFetchTests(Base):
    def test_job_fetches_inspiration_first(self):
        calls = []
        with mock.patch.object(th, "fetch_inspiration",
                               side_effect=lambda *a, **k: calls.append("f")), \
             mock.patch.object(th, "run_concepts"), \
             mock.patch("whisperradar.webstages.web_transport") as wt:
            wt.return_value.__enter__.return_value = mock.Mock()
            th.concepts_job(self.cfg, self.pid, "zai", "deepseek",
                            lambda m: None)
        self.assertEqual(calls, ["f"])


if __name__ == "__main__":
    unittest.main()
