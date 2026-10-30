"""Word-by-word captions for vertical productions."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import shortcaptions as sc  # noqa: E402

SRT = ("1\n00:00:00,000 --> 00:00:02,000\nOne small step\n\n"
       "2\n00:00:02,000 --> 00:00:03,000\nGo\n")


class WordTests(unittest.TestCase):
    def test_every_word_gets_its_own_time_inside_its_cue(self):
        w = sc.words_from_srt(SRT)
        self.assertEqual([x[2] for x in w], ["One", "small", "step", "Go"])
        self.assertAlmostEqual(w[0][0], 0.0)
        self.assertAlmostEqual(w[2][1], 2.0, places=2)
        self.assertAlmostEqual(w[3][0], 2.0)

    def test_longer_words_stay_longer(self):
        w = sc.words_from_srt(SRT)
        self.assertGreater(w[1][1] - w[1][0], w[0][1] - w[0][0])

    def test_words_never_overlap(self):
        w = sc.words_from_srt(SRT)
        for a, b in zip(w, w[1:]):
            self.assertLessEqual(a[1], b[0] + 1e-6)

    def test_empty_or_broken_srt_gives_nothing(self):
        self.assertEqual(sc.words_from_srt(""), [])
        self.assertEqual(sc.words_from_srt("nonsense"), [])


class FileTests(unittest.TestCase):
    def test_writes_srt_and_ass(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "a.srt"
            src.write_text(SRT, encoding="utf-8")
            out = sc.write(src, Path(d) / "o", 1080, 1920)
            self.assertIn("PlayResY: 1920", Path(out["ass"]).read_text(encoding="utf-8"))
            self.assertIn("STEP", Path(out["ass"]).read_text(encoding="utf-8"))
            self.assertEqual(Path(out["srt"]).read_text(encoding="utf-8").count("-->"), 4)

    def test_no_srt_no_files(self):
        self.assertEqual(sc.write(None, "x"), {})

    def test_braces_cannot_break_the_ass_line(self):
        a = sc.to_ass([(0, 1, "{bad}")])
        self.assertNotIn("{bad}", a)


if __name__ == "__main__":
    unittest.main()
