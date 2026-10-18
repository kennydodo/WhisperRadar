"""Stable text line endings for browser forms and generated content."""
import re


def normalize_newlines(text):
    # A CRCRLF is a double-translated Windows newline, not a blank paragraph.
    # Preserve intentional consecutive LF/CRLF paragraph breaks.
    return re.sub(r"\r+\n", "\n", text).replace("\r", "\n")


def write_text(path, text):
    path.write_text(normalize_newlines(text), encoding="utf-8", newline="\n")


def read_text(path):
    # Read bytes so universal-newline decoding cannot expand CRCRLF first.
    return normalize_newlines(path.read_bytes().decode("utf-8-sig"))
