"""Watched-channel "Get style & bible" also returns the WRITING style, built
from the transcripts of several videos: the stage-1 sections and rules plus a
Story Layout section."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402,F401

from whisperradar import channel_look, db  # noqa: E402
from whisperradar.config import load_config  # noqa: E402

VISUAL = ("=== STYLE GUIDE ===\nSoft 3D.\n=== BIBLE ===\nA tall robot.")
WRITING = "## Voice & Tone\ncalm\n## Story Layout (how this channel lays out its videos)\nhook, build, payoff"


class FakeTransport:
    def __init__(self, fail_writing=False):
        self.calls = []
        self.fail_writing = fail_writing

    def ask(self, site, prompt, files=(), **kw):
        self.calls.append((prompt, list(files)))
        if "WRITING STYLE" in prompt and "TRANSCRIPT TO ANALYZE" in prompt:
            if self.fail_writing:
                raise RuntimeError("chat down")
            return WRITING
        return VISUAL


class LookWritingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = self.t / "wr.db"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        conn.execute("INSERT INTO channels (name, channel_id, kind, genre, "
                     "active, added_at) VALUES ('Rival','UCrival','source',"
                     "'Lifestyle',1,'2026-01-01')")
        self.paths = []
        for i, title in enumerate(["Quiet mornings in Kyoto",
                                   "Why tea rituals calm you",
                                   "Building a slow evening routine"]):
            tr = self.t / f"t{i}.txt"
            tr.write_text(f"Video {i} words. " * 50, encoding="utf-8")
            self.paths.append(tr)
            conn.execute(
                "INSERT INTO videos (channel_id, video_id, title, url, "
                "published_at, view_count, duration, transcript_path) VALUES "
                "('UCrival', ?, ?, ?, '2026-01-01', ?, 600, ?)",
                (f"v{i}", title, f"https://y/{i}", 100 - i, str(tr)))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _grab(self, video, at, out):
        out.write_bytes(b"jpg")
        return True

    def _dl(self, url, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / "v.mp4"
        p.write_bytes(b"x")
        return p, 600.0

    def _run(self, transport):
        return channel_look.run(
            self.cfg, "UCrival", "chat", transport, lambda m: None,
            n_frames=6, download=self._dl, grab=self._grab,
            thumbs=None, send_count=3)

    def test_prompt_has_the_stage_one_sections_and_story_layout(self):
        conn = db.connect(self.cfg.db_path)
        videos = channel_look.pick_transcribed(conn, "UCrival")
        conn.close()
        self.assertEqual(len(videos), 3)
        p = channel_look.writing_prompt("Rival", "Lifestyle", videos,
                                        hint="calm narrator")
        for section in ("## Voice & Tone", "## Pacing & Rhythm",
                        "## Sentence Style", "## Hook Pattern",
                        "## Structure", "## CTA Style",
                        "## Vocabulary & Register", "## Things to Avoid",
                        channel_look.STORY_HEADER):
            self.assertIn(section, p)
        self.assertIn("### Video 1", p)
        self.assertIn("### Video 3", p)
        self.assertIn("3 separate videos", p)
        self.assertIn("calm narrator", p)
        self.assertIn("STORY LAYOUT", p)
        self.assertIn("TIME BEATS", p)          # default time-beat variant
        self.assertTrue(p.rstrip().endswith("Output ONLY the style guide markdown."))

    def test_run_saves_the_writing_style_beside_the_look(self):
        t = FakeTransport()
        self._run(t)
        look = channel_look.load(self.cfg, "UCrival")
        self.assertEqual(look["style"].strip(), "Soft 3D.")
        self.assertEqual(look["writing_style"].splitlines()[0], "## Voice & Tone")
        self.assertEqual(len(look["writing_videos"]), 3)
        writing_calls = [c for c in t.calls if not c[1]]
        self.assertEqual(len(writing_calls), 1)       # text only, no frames

    def test_writing_failure_keeps_the_look(self):
        self._run(FakeTransport(fail_writing=True))
        look = channel_look.load(self.cfg, "UCrival")
        self.assertEqual(look["style"].strip(), "Soft 3D.")
        self.assertEqual(look["writing_style"], "")

    def test_no_transcripts_means_no_writing_style_but_a_look(self):
        conn = db.connect(self.cfg.db_path)
        conn.execute("UPDATE videos SET transcript_path = NULL")
        conn.commit()
        conn.close()
        t = FakeTransport()
        self._run(t)
        look = channel_look.load(self.cfg, "UCrival")
        self.assertEqual(look["style"].strip(), "Soft 3D.")
        self.assertEqual(look["writing_style"], "")
        with self.assertRaises(RuntimeError):
            channel_look.regenerate_writing(self.cfg, "UCrival", "chat", t,
                                            lambda m: None)

    def test_regenerate_writing_keeps_the_visual_look(self):
        self._run(FakeTransport())
        t = FakeTransport()
        channel_look.regenerate_writing(self.cfg, "UCrival", "chat", t,
                                        lambda m: None, hint="slow")
        look = channel_look.load(self.cfg, "UCrival")
        self.assertEqual(look["style"].strip(), "Soft 3D.")
        self.assertTrue(look["files"])
        self.assertIn("slow", t.calls[0][0])
        self.assertIn("Story Layout", look["writing_style"])

    def test_writing_only_state_loads_and_a_later_look_keeps_it(self):
        channel_look.regenerate_writing(self.cfg, "UCrival", "chat",
                                        FakeTransport(), lambda m: None)
        look = channel_look.load(self.cfg, "UCrival")
        self.assertTrue(look["writing_style"])
        self.assertEqual(look["style"].strip(), "")
        self._run(FakeTransport(fail_writing=True))   # visual refresh only
        look = channel_look.load(self.cfg, "UCrival")
        self.assertEqual(look["style"].strip(), "Soft 3D.")
        self.assertTrue(look["writing_style"])
        self.assertEqual(len(look["writing_videos"]), 3)

    def test_long_transcripts_keep_opening_and_ending(self):
        v = {"transcript_path": str(self.paths[0])}
        self.paths[0].write_text("START " + "mid " * 6000 + "END", encoding="utf-8")
        text = channel_look._transcript_text(v, 1000)
        self.assertTrue(text.startswith("START"))
        self.assertTrue(text.endswith("END"))
        self.assertIn("[...]", text)
        self.assertLessEqual(len(text), 1010)


if __name__ == "__main__":
    unittest.main()
