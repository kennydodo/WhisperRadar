import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db, source_analysis as sa  # noqa: E402
from whisperradar.config import load_config  # noqa: E402


def _reply():
    return {"summary": "s", "takeaways": ["open a question early", " ", "x"],
            "dimensions": [{"key": k, "score": s, "quote": "q " * 5,
                            "analysis": "a"}
                           for k, s in zip(sa.KEYS, (8, 9.26, 7, 5.5, 12, "x", 4))]
            + [{"key": "bogus", "score": 3}]}


class ParseTests(unittest.TestCase):
    def test_keeps_known_keys_clamps_and_drops_bad_scores(self):
        d = sa.parse(_reply())
        self.assertEqual([x["key"] for x in d["dimensions"]],
                         [k for k in sa.KEYS if k != "promise"])
        self.assertEqual({x["key"]: x["score"] for x in d["dimensions"]}["interrupts"], 10.0)
        self.assertEqual(d["takeaways"], ["open a question early", "x"])
        self.assertIsNotNone(d["overall"])

    def test_garbage_gives_empty(self):
        d = sa.parse("nope")
        self.assertEqual(d["dimensions"], [])
        self.assertIsNone(d["overall"])

    def test_writer_note_names_weak_spots_and_never_quotes(self):
        note = sa.writer_note(sa.parse(_reply()))
        self.assertIn("open a question early", note)
        self.assertIn("weak on", note)
        self.assertNotIn("q q", note)
        self.assertEqual(sa.writer_note(None), "")

    def test_prompt_lists_every_dimension_and_cuts_long_text(self):
        p = sa.prompt("T", "w " * 100000)
        for k in sa.KEYS:
            self.assertIn(k, p)
        self.assertIn("transcript cut", p)


class PageTests(unittest.TestCase):
    def test_transcript_page_shows_saved_analysis(self):
        from whisperradar.webapp import create_app
        with tempfile.TemporaryDirectory() as d:
            t = Path(d)
            cfg = load_config(ROOT / "config.yaml")
            cfg.db_path = t / "wr.db"
            cfg.studio_dir = t / "studio"
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            db.add_channel(conn, "Src", "UC1")
            db.upsert_video(conn, "UC1", {"video_id": "v1", "title": "T", "url": "u"})
            tr = t / "v1.txt"
            tr.write_text("hello", encoding="utf-8")
            db.set_video(conn, "v1", status="transcribed", transcript_path=str(tr))
            conn.close()
            p = sa.path_for(cfg, "v1")
            p.parent.mkdir(parents=True)
            import json
            data = sa.parse(_reply())
            data.update({"made_at": "now", "writer": "zai"})
            p.write_text(json.dumps(data), encoding="utf-8")
            html = create_app(cfg).test_client().get("/transcript/v1").get_data(as_text=True)
            self.assertIn("Why it works", html)
            self.assertIn("Curiosity loops", html)
            self.assertIn("Analyse again", html)


if __name__ == "__main__":
    unittest.main()
