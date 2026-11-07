"""WhisperRadar: watch YouTube channels, download audio, transcribe with Whisper."""

__version__ = "0.1.0"


_DLL_DIR_HANDLES: list = []


def _register_nvidia_dll_dirs() -> None:
    """Windows: make the pip-installed CUDA libraries (nvidia-cublas-cu12 /
    nvidia-cudnn-cu12) loadable BEFORE ctranslate2 is imported. ctranslate2
    loads its DLLs on import and only searches its own folder, so without this
    "Could not find module ...ctranslate2.dll (or one of its dependencies)"
    is raised (and Whisper silently drops to the CPU)."""
    import sys

    if sys.platform != "win32":
        return
    try:
        import os
        import sysconfig

        site = sysconfig.get_paths()["purelib"]
        for name in ("cublas", "cudnn"):
            path = os.path.join(site, "nvidia", name, "bin")
            if os.path.isdir(path):
                _DLL_DIR_HANDLES.append(os.add_dll_directory(path))
    except Exception:  # noqa: BLE001 - never block startup over this
        pass


_register_nvidia_dll_dirs()
