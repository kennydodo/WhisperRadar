"""PNG / JPEG header size reader (ImgToVideo.Core.Imaging.ImageDimensionsReader)."""
import struct

_PNG = b"\x89PNG\r\n\x1a\n"


def try_read(path):
    """-> (width, height) or None."""
    try:
        with open(path, "rb") as fs:
            first = fs.read(2)
            if len(first) < 2:
                return None
            if first == b"\xff\xd8":
                return _read_jpeg(fs)
            rest = fs.read(6)
            if len(first + rest) < 8:
                return None
            if first + rest == _PNG:
                return _read_png(fs)
            return None
    except OSError:
        return None


def _read_png(fs):
    header = fs.read(16)
    if len(header) < 16 or header[4:8] != b"IHDR":
        return None
    w, h = struct.unpack(">ii", header[8:16])
    return (w, h) if w > 0 and h > 0 else None


def _read_jpeg(fs):
    while True:
        b = fs.read(1)
        while b and b != b"\xff":
            b = fs.read(1)
        if not b:
            return None
        marker = fs.read(1)
        while marker == b"\xff":
            marker = fs.read(1)
        if not marker:
            return None
        mk = marker[0]
        if mk == 0x01 or 0xD0 <= mk <= 0xD9:
            continue
        lb = fs.read(2)
        if len(lb) < 2:
            return None
        length = struct.unpack(">H", lb)[0]
        if length < 2:
            return None
        if 0xC0 <= mk <= 0xCF and mk not in (0xC4, 0xC8, 0xCC):
            sof = fs.read(5)
            if len(sof) < 5:
                return None
            h, w = struct.unpack(">HH", sof[1:5])
            return (w, h) if w > 0 and h > 0 else None
        fs.seek(length - 2, 1)
