"""The studio job log must outlive the server process.

A stopped batch's real reason used to exist only in the in-memory deque: the
Flow Driver keeps its batch log in RAM (and its service was stopped), and a
WhisperRadar restart (dev reloader or manual) wiped sjob.log - production
12's image batch stopped with 57 of 64 images missing and left no trace of
why anywhere on disk. _TeeLog mirrors every line into data\\logs\\studio.log
so the reason can always be read back.

Run: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar.webapp import _TeeLog  # noqa: E402


class TeeLogTests(unittest.TestCase):
    def test_lines_land_on_disk_and_in_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "logs" / "studio.log"
            log = _TeeLog(p)
            log.append("image batch: 7 of 64 produced")
            log.append("paused: 57 of 64 image(s) were not produced")
            self.assertEqual(list(log), ["image batch: 7 of 64 produced",
                                         "paused: 57 of 64 image(s) were not produced"])
            text = p.read_text(encoding="utf-8")
            self.assertIn("7 of 64 produced", text)
            self.assertIn("57 of 64 image(s) were not produced", text)
            self.assertRegex(text, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ")

    def test_the_parent_logs_folder_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "does" / "not" / "exist" / "studio.log"
            _TeeLog(p).append("survives the restart")
            self.assertTrue(p.exists())

    def test_without_a_path_it_still_buffers(self):
        log = _TeeLog(None)
        log.append("kept only in memory")
        self.assertEqual(list(log), ["kept only in memory"])

    def test_maxlen_still_caps_the_memory_copy(self):
        log = _TeeLog(None, maxlen=2)
        for i in range(5):
            log.append(f"line {i}")
        self.assertEqual(list(log), ["line 3", "line 4"])


if __name__ == "__main__":
    unittest.main()
