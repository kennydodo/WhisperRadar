"""Transcription with faster-whisper.

Uses the GPU (CUDA, float16) when available and falls back to CPU (int8)
automatically - both when picking the device and if CUDA fails at load time
(e.g. missing cuDNN DLLs).
"""

import logging
import sys
from pathlib import Path

log = logging.getLogger("whisperradar")

_MODEL_CACHE: dict = {}

# DLL groups ctranslate2 needs for CUDA inference on Windows;
# each group needs at least one loadable DLL
_CUDA_DLL_GROUPS = (
    ("cublas64_12.dll",),
    ("cudnn64_9.dll", "cudnn_ops64_9.dll"),
)


def _cuda_dlls_available() -> bool:
    if sys.platform != "win32":
        return True
    import ctypes
    import os

    # Make pip-installed CUDA libs visible: pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
    # (the nvidia.* wheels are namespace packages, so locate via site-packages)
    import sysconfig

    purelib = Path(sysconfig.get_paths()["purelib"])
    for name in ("cublas", "cudnn"):
        bin_dir = purelib / "nvidia" / name / "bin"
        if bin_dir.is_dir():
            os.add_dll_directory(str(bin_dir))

    for group in _CUDA_DLL_GROUPS:
        found = False
        for dll in group:
            try:
                ctypes.WinDLL(dll)
                found = True
                break
            except OSError:
                continue
        if not found:
            log.info("CUDA skipped: none of %s found (will use CPU)", group)
            return False
    return True


def pick_device() -> tuple[str, str]:
    """Return (device, compute_type) based on CUDA availability."""
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0 and _cuda_dlls_available():
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def _load_model(model_size: str, device: str | None = None):
    """Load or fetch the cached model. device=None auto-detects."""
    if model_size in _MODEL_CACHE:
        return _MODEL_CACHE[model_size]
    if device is None:
        device, compute_type = pick_device()
    else:
        compute_type = "int8" if device == "cpu" else "float16"
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel(model_size, device=device, compute_type=compute_type)
    except Exception as exc:
        if device == "cpu":
            raise
        log.warning("CUDA load failed (%s); falling back to CPU", exc)
        device, compute_type = "cpu", "int8"
        from faster_whisper import WhisperModel

        model = WhisperModel(model_size, device=device, compute_type=compute_type)
    log.info("Whisper model '%s' loaded on %s (%s)", model_size, device, compute_type)
    _MODEL_CACHE[model_size] = (model, device)
    return _MODEL_CACHE[model_size]


def _run(model, audio_path: Path, out_txt: Path, language: str | None) -> dict:
    segments, info = model.transcribe(
        str(audio_path),
        language=language,
        vad_filter=True,
    )

    paragraphs: list[str] = []
    buf: list[str] = []
    prev_end = None
    for seg in segments:
        if buf and prev_end is not None and seg.start - prev_end > 3.0:
            paragraphs.append(" ".join(buf))
            buf = []
        buf.append(seg.text.strip())
        prev_end = seg.end
    if buf:
        paragraphs.append(" ".join(buf))

    body = "\n\n".join(paragraphs)
    out_txt.write_text(body + "\n", encoding="utf-8")
    return {"language": info.language, "duration": info.duration}


def transcribe_audio(
    audio_path: Path,
    out_txt: Path,
    model_size: str = "small",
    language: str | None = None,
) -> dict:
    """Transcribe audio to a plain-text file. Returns metadata dict."""
    out_txt.parent.mkdir(parents=True, exist_ok=True)
    model, device = _load_model(model_size)
    try:
        result = _run(model, audio_path, out_txt, language)
    except Exception as exc:
        if device != "cuda":
            raise
        log.warning("CUDA inference failed (%s); retrying on CPU", exc)
        _MODEL_CACHE.pop(model_size, None)
        model, device = _load_model(model_size, device="cpu")
        result = _run(model, audio_path, out_txt, language)
    return {**result, "device": device}


def _srt_ts(t: float) -> str:
    if t < 0:
        t = 0.0
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms >= 1000:
        ms = 999
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def transcribe_to_srt(audio_path: Path, out_srt: Path, model_size: str = "small",
                      language: str | None = None) -> dict:
    """Transcribe audio into an .srt file with word-accurate cues."""
    out_srt.parent.mkdir(parents=True, exist_ok=True)
    model, device = _load_model(model_size)
    segments, info = model.transcribe(str(audio_path), language=language,
                                      vad_filter=True, word_timestamps=True)
    segs = list(segments)

    cues: list[tuple[float, float, str]] = []
    words: list[tuple[str, float, float]] = []

    def flush():
        if words:
            cues.append((words[0][1], words[-1][2],
                         " ".join(w[0] for w in words)))
            words.clear()

    for seg in segs:
        for w in (seg.words or []):
            text = (w.word or "").strip()
            if not text:
                continue
            if words and (w.end - words[0][1] > 3.5 or len(words) >= 9
                          or text[-1] in ".!?"):
                flush()
            words.append((text, w.start, w.end))
        flush()
    flush()

    if not cues:  # no word timestamps available - fall back to segments
        cues = [(seg.start, seg.end, seg.text.strip()) for seg in segs
                if seg.text.strip()]

    blocks = []
    for i, (start, end, text) in enumerate(cues, 1):
        blocks.append(f"{i}\n{_srt_ts(start)} --> {_srt_ts(end)}\n{text}\n")
    out_srt.write_text("\n".join(blocks), encoding="utf-8")
    return {"language": info.language, "duration": info.duration,
            "device": device, "cues": len(cues)}
