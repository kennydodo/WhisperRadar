"""Capture the REAL output of a code snippet for a code_lesson output panel, in any language.

    python -m whisperradar.video.explainer.code_output "python snippet.py"
    python -m whisperradar.video.explainer.code_output "dotnet run --project Demo"
    python -m whisperradar.video.explainer.code_output "node app.js" --max-lines 4

The command is whatever runs the video's code (python, dotnet, node, java, go run ., sqlite3 ...). The output goes
to the panel as is, so nothing on screen is invented; it fails (exit code 1) if the command fails or prints nothing,
and refuses output that would not fit the panel (8 lines, 80 characters) instead of silently cutting it.
"""
import shlex
import subprocess
import sys

MAX_LINES = 8
MAX_WIDTH = 80


class OutputError(Exception):
    pass


def capture(command, cwd=None, timeout=60, max_lines=MAX_LINES, max_width=MAX_WIDTH):
    """-> list of output lines (trailing blanks dropped). command: a string or an argument list."""
    args = shlex.split(command) if isinstance(command, str) else list(command)
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise OutputError(f"{args[0]!r} was not found on PATH") from None
    except subprocess.TimeoutExpired:
        raise OutputError(f"the command ran longer than {timeout} s") from None
    if r.returncode:
        raise OutputError(f"the command failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[:300]}")
    lines = [ln.rstrip() for ln in r.stdout.replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        raise OutputError("the command printed nothing")
    if len(lines) > max_lines:
        raise OutputError(f"{len(lines)} output lines; the panel shows {max_lines}. Print less in the snippet.")
    wide = [ln for ln in lines if len(ln) > max_width]
    if wide:
        raise OutputError(f"an output line is {len(wide[0])} characters; the panel shows {max_width}.")
    return lines


def main(argv):
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    n = int(argv[argv.index("--max-lines") + 1]) if "--max-lines" in argv else MAX_LINES
    try:
        for ln in capture(argv[0], max_lines=n):
            print(ln)
    except OutputError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
