"""Motion clips stage: which clips an opening needs, and when the stage skips."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import autorun, db, motion, studio  # noqa: E402
from tests.test_motion_shotlist import _cues, _data  # noqa: E402


class StageOrderTests(unittest.TestCase):
    def test_motion_sits_between_images_and_merge(self):
        s = db.STAGES
        self.assertEqual(s.index("motion"), s.index("images") + 1)
        self.assertEqual(s.index("merge"), s.index("motion") + 1)
        self.assertIn("motion", autorun.RUN_STAGES)
        self.assertIn("motion", autorun._RUNNERS)


class WindowClipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdir = Path(self.tmp.name)
        (self.pdir / "images").mkdir()
        self.cues = _cues()          # 20 cues x 4 s
        self.data = _data(self.cues, motion_for=lambda c: c <= 8)

    def tearDown(self):
        self.tmp.cleanup()

    def _clips(self, seconds=30):
        return motion.window_clips(self.data, self.cues, seconds,
                                   studio._srt_seconds, self.pdir)

    def test_only_window_shots_become_clips(self):
        clips = self._clips(30)
        self.assertEqual(len(clips), 8)
        self.assertTrue(all(c["prompt"] for c in clips))
        self.assertTrue(all(c["out"].endswith(".mp4") for c in clips))

    def test_image_is_found_by_stem_and_missing_is_none(self):
        (self.pdir / "images" / "S01_SCN_ZI.jpg").write_bytes(b"x")
        clips = self._clips(30)
        self.assertTrue(clips[0]["image"].endswith("S01_SCN_ZI.jpg"))
        self.assertIsNone(clips[1]["image"])

    def test_existing_clip_is_not_regenerated(self):
        clips = self._clips(30)
        out = Path(clips[0]["out"])
        out.parent.mkdir(parents=True)
        out.write_bytes(b"x" * 20000)
        left = motion.missing_clips(clips)
        self.assertEqual(len(left), len(clips) - 1)

    def test_tiny_clip_file_counts_as_missing(self):
        clips = self._clips(30)
        out = Path(clips[0]["out"])
        out.parent.mkdir(parents=True)
        out.write_bytes(b"x")
        self.assertEqual(len(motion.missing_clips(clips)), len(clips))

    def test_shots_without_ai_motion_are_left_out(self):
        self.data = _data(self.cues, motion_for=lambda c: c in (1, 2))
        self.assertEqual(len(self._clips(30)), 2)


if __name__ == "__main__":
    unittest.main()
