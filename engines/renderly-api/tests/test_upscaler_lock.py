"""Concurrent /generate requests must not run the GPU engine at the same time."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import base  # noqa: F401
from PIL import Image
from services import upscaler


class EngineSerialised(unittest.TestCase):
    def test_only_one_engine_process_at_a_time(self):
        state = {"now": 0, "max": 0}
        guard = threading.Lock()

        def fake_run(exe, src, dst, gpu):
            with guard:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
            time.sleep(0.05)
            Image.new("RGB", (256, 256), (10, 20, 30)).save(dst, "PNG")
            with guard:
                state["now"] -= 1
            return mock.Mock(stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.png"
            Image.new("RGB", (64, 64), (10, 20, 30)).save(src, "PNG")
            with mock.patch.object(upscaler, "exe_path", return_value=Path(__file__)), \
                    mock.patch.object(upscaler, "_run", side_effect=fake_run), \
                    mock.patch.object(upscaler, "_cached_gpu", return_value=0), \
                    mock.patch.object(upscaler, "_content_ok", return_value=True), \
                    mock.patch.object(upscaler, "_load_cache", return_value={"device": 0}), \
                    mock.patch.object(upscaler, "_save_cache"):
                threads = [
                    threading.Thread(target=upscaler.upscale, args=(src, Path(tmp) / f"o{i}.png", "HD"))
                    for i in range(4)
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
        self.assertEqual(state["max"], 1)


if __name__ == "__main__":
    unittest.main()
