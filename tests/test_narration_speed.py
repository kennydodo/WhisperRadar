"""Narration speed: ffmpeg atempo after the TTS step, per channel with a global default."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from whisperradar import db, narration_speed as ns, settings


def _duration(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True).stdout
    return float(out.strip())


class PureTests(unittest.TestCase):
    def test_is_change(self):
        self.assertFalse(ns.is_change(1.0))
        self.assertFalse(ns.is_change(1.001))
        self.assertTrue(ns.is_change(1.1))
        self.assertTrue(ns.is_change(0.9))
        self.assertFalse(ns.is_change("x"))

    def test_clamped_to_what_atempo_takes(self):
        self.assertEqual(2.0, ns.clamp(5))
        self.assertEqual(0.5, ns.clamp(0.1))
        self.assertIn("atempo=1.1000", ns.command(Path("a.mp3"), Path("b.mp3"), 1.1))

    def test_setting_and_channel_field_exist(self):
        keys = {e["key"]: e for e in settings.SPEC}
        self.assertEqual(1.0, keys["narration_speed"]["default"])
        self.assertIn("narration_speed", db._OWN_CHANNEL_FIELDS)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class FfmpegTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audio = Path(self.tmp.name) / "audio.mp3"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=8", str(self.audio)], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_faster_is_shorter_and_the_original_is_kept(self):
        before = _duration(self.audio)
        ns.apply(self.audio, 1.25, fresh=True)
        self.assertAlmostEqual(before / 1.25, _duration(self.audio), delta=0.25)
        self.assertAlmostEqual(before, _duration(self.audio.with_name("audio_original.mp3")), delta=0.1)

    def test_repeating_does_not_compound(self):
        ns.apply(self.audio, 1.25, fresh=True)
        once = _duration(self.audio)
        ns.apply(self.audio, 1.25)
        self.assertAlmostEqual(once, _duration(self.audio), delta=0.1)

    def test_fresh_audio_replaces_a_stale_original(self):
        ns.apply(self.audio, 1.25, fresh=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=330:duration=4", str(self.audio)], check=True)
        ns.apply(self.audio, 1.0 + 0.5, fresh=True)
        self.assertAlmostEqual(4 / 1.5, _duration(self.audio), delta=0.25)


if __name__ == "__main__":
    unittest.main()
