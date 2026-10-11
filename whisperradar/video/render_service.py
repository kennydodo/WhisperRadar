"""Port of PreviewRenderService.cs: render segments in parallel, concat, mux."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .ffmpeg_runner import FfmpegRunner, RenderCancelled
from .render_plan import RenderPlan

MANIFEST_FILE_NAME = "render-manifest.json"


@dataclass
class RenderResult:
    success: bool
    output_path: str
    errors: List[str] = field(default_factory=list)


class PreviewRenderService:
    def __init__(self, runner: FfmpegRunner):
        self._runner = runner

    def render(self, plan: RenderPlan, max_parallelism: int = 2,
               progress: Optional[Callable[[float], None]] = None,
               cancel_event: Optional[threading.Event] = None,
               reuse_unchanged_segments: bool = False) -> RenderResult:
        errors: List[str] = []
        gate = threading.Lock()
        state = {"completed": 0}
        manifest_path = os.path.join(os.path.dirname(plan.concat_list_path), MANIFEST_FILE_NAME)
        manifest = _load_manifest(manifest_path)
        fingerprints: Dict[str, str] = {}
        skip = set()

        def report(v: float) -> None:
            if progress is not None:
                progress(v)

        for seg in plan.segments:
            key = os.path.basename(seg.output_path)
            fp = _fingerprint(seg.arguments)
            fingerprints[key] = fp
            if (reuse_unchanged_segments and manifest.get(key.lower()) is not None
                    and manifest[key.lower()][1] == fp and os.path.exists(seg.output_path)):
                skip.add(seg.output_path)
                state["completed"] += seg.frame_count

        report(1.0 if plan.total_frames == 0 else state["completed"] / plan.total_frames)

        def run_segment(seg) -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise RenderCancelled("Render cancelled.")

            def on_line(line: str) -> None:
                if not line.startswith("frame="):
                    return
                try:
                    frame = int(line[len("frame="):].strip())
                except ValueError:
                    return
                with gate:
                    done = min(frame, seg.frame_count)
                    fraction = (state["completed"] + done) / plan.total_frames
                report(fraction)

            result = self._runner.run(seg.arguments, on_stdout_line=on_line, cancel_event=cancel_event)
            key = os.path.basename(seg.output_path)
            with gate:
                state["completed"] += seg.frame_count
                if result.success:
                    manifest[key.lower()] = (key, fingerprints[key])
                else:
                    errors.append(f"Segment {seg.index} failed (exit {result.exit_code}): {result.error_tail}")
                    manifest.pop(key.lower(), None)
                frac = 1.0 if plan.total_frames == 0 else state["completed"] / plan.total_frames
            report(frac)

        todo = [s for s in plan.segments if s.output_path not in skip]
        with ThreadPoolExecutor(max_workers=max(1, max_parallelism)) as pool:
            futures = [pool.submit(run_segment, s) for s in todo]
            first_exc = None
            for f in futures:
                try:
                    f.result()
                except BaseException as e:  # noqa: BLE001
                    if first_exc is None:
                        first_exc = e
                        if cancel_event is None or not isinstance(e, RenderCancelled):
                            pass
            if first_exc is not None:
                raise first_exc
        if cancel_event is not None and cancel_event.is_set():
            raise RenderCancelled("Render cancelled.")

        _save_manifest(manifest_path, manifest, fingerprints)

        if errors:
            return RenderResult(False, plan.preview_path, errors)

        with open(plan.concat_list_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(plan.concat_list_content)

        concat = self._runner.run(plan.concat_arguments, cancel_event=cancel_event)
        if not concat.success:
            return RenderResult(False, plan.preview_path,
                                [f"Concatenation failed (exit {concat.exit_code}): {concat.error_tail}"])
        mux = self._runner.run(plan.mux_arguments, cancel_event=cancel_event)
        if not mux.success:
            return RenderResult(False, plan.preview_path,
                                [f"Audio mux failed (exit {mux.exit_code}): {mux.error_tail}"])
        report(1.0)
        return RenderResult(True, plan.preview_path, [])


def _load_manifest(path: str) -> Dict[str, tuple]:
    """lower-cased key -> (original key, fingerprint)."""
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        segs = data.get("segments") if isinstance(data, dict) else None
        if not segs:
            return {}
        return {k.lower(): (k, v) for k, v in segs.items() if isinstance(v, str)}
    except (OSError, ValueError):
        return {}


def _save_manifest(path: str, manifest: Dict[str, tuple], fingerprints: Dict[str, str]) -> None:
    try:
        pruned = {k: v for k, v in fingerprints.items()
                  if k.lower() in manifest and manifest[k.lower()][1] == v}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"segments": pruned}, fh, indent=2)
    except OSError:
        pass


def _input_stamp(file: str) -> str:
    try:
        st = os.stat(file)
        return f"{os.path.abspath(file)}|{st.st_size}|{st.st_mtime_ns // 100}"
    except OSError:
        return f"{file}|missing"


def _fingerprint(arguments) -> str:
    inputs = [arguments[i + 1] for i in range(len(arguments) - 1) if arguments[i] == "-i"]
    payload = "\n".join(arguments) + "\n" + "\n".join(_input_stamp(i) for i in inputs)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest().upper()
