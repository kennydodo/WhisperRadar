"""Port of FfmpegRunner.cs: run an ffmpeg/ffprobe process, stream lines, support cancel."""
from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

from . import tool_locator


class RenderCancelled(Exception):
    pass


@dataclass
class ProcessResult:
    exit_code: int
    standard_output: str
    standard_error: str

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    @property
    def error_tail(self) -> str:
        trimmed = self.standard_error.rstrip()
        if not trimmed:
            return ""
        lines = trimmed.split("\n")
        return trimmed if len(lines) <= 40 else "\n".join(lines[-40:])


class FfmpegRunner:
    def __init__(self, ffmpeg_path: str = "ffmpeg"):
        self.ffmpeg_path = tool_locator.resolve("ffmpeg", ffmpeg_path)

    def run(
        self,
        arguments: Sequence[str],
        on_stdout_line: Optional[Callable[[str], None]] = None,
        on_stderr_line: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> ProcessResult:
        try:
            proc = subprocess.Popen(
                [self.ffmpeg_path, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as e:
            raise RuntimeError(
                f'Failed to start ffmpeg ("{self.ffmpeg_path}"): {e}. '
                "Install ffmpeg or set the ffmpeg path in the render settings."
            ) from e

        out: List[str] = []
        err: List[str] = []

        def pump(stream, sink: List[str], cb):
            for line in stream:
                line = line.rstrip("\r\n")
                sink.append(line)
                if cb is not None:
                    try:
                        cb(line)
                    except Exception:
                        pass

        threads = [
            threading.Thread(target=pump, args=(proc.stdout, out, on_stdout_line), daemon=True),
            threading.Thread(target=pump, args=(proc.stderr, err, on_stderr_line), daemon=True),
        ]
        for t in threads:
            t.start()
        cancelled = False
        while True:
            try:
                proc.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    proc.wait()
                    break
        for t in threads:
            t.join(timeout=5)
        for st in (proc.stdout, proc.stderr):
            try:
                st.close()
            except OSError:
                pass
        if cancelled:
            raise RenderCancelled("Render cancelled.")
        return ProcessResult(
            proc.returncode,
            "".join(l + "\n" for l in out),
            "".join(l + "\n" for l in err),
        )
