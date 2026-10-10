"""Helpers reproducing .NET number formatting used in ImgToVideo messages (culture-invariant)."""
from decimal import Decimal, ROUND_HALF_UP


def fixed(x: float, digits: int) -> str:
    """double.ToString("F<digits>"): exact value, midpoints away from zero."""
    q = Decimal(1).scaleb(-digits)
    d = Decimal(float(x)).quantize(q, rounding=ROUND_HALF_UP)
    return format(d, "f")


def general(x) -> str:
    """double.ToString() / string interpolation of a double (shortest round-trip)."""
    if isinstance(x, int):
        return str(x)
    s = repr(float(x))
    if s.endswith(".0"):
        s = s[:-2]
    if "e" in s or "E" in s:
        d = Decimal(s)
        s = format(d, "f")
        if "." in s:
            s = s.rstrip("0").rstrip(".")
    return s


def optional(x: float, max_digits: int) -> str:
    """double.ToString("0.##") / "0.#" : up to max_digits decimals, trailing zeros dropped."""
    q = Decimal(1).scaleb(-max_digits)
    d = Decimal(repr(float(x))).quantize(q, rounding=ROUND_HALF_UP)
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s


def signed(n: int) -> str:
    """{n:+0;-0}"""
    return f"+{n}" if n >= 0 else f"-{abs(n)}"
