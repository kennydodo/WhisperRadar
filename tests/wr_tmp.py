"""Shared temp-dir cleanup for tests: a route may start a job on a thread
that still holds the temporary wr.db open for a moment. On Windows that
makes TemporaryDirectory.cleanup() raise PermissionError, which unittest
reports as a spurious ERROR. Retry briefly, then drop it either way."""
import contextlib
import gc
import tempfile
import time


def cleanup(tmp):
    gc.collect()
    for _ in range(20):
        try:
            tmp.cleanup()
            return
        except (PermissionError, OSError):
            time.sleep(0.05)
    try:
        tmp.cleanup()
    except (PermissionError, OSError):
        pass


@contextlib.contextmanager
def tempdir():
    """Drop-in for `with tempfile.TemporaryDirectory() as d:` where `d` is
    the path string, cleaned with the retry above."""
    tmp = tempfile.TemporaryDirectory()
    try:
        yield tmp.name
    finally:
        cleanup(tmp)
