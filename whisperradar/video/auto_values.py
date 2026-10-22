"""Numbers, years and amounts the narration says, shown on screen by the renderer.

Found locally in the SRT cue text (no LLM): a value appears when the cue that says
it starts and goes when that sentence's context is over - the end of the cue, or of
the following cues while the sentence continues - never shorter than the minimum
or longer than the maximum, and never past the shot. Two values in one cue are
shown together, one per line. The result is a list of overlay items in the same
shape the shotlist's `overlays` use (start/end are seconds from the shot's start).
"""
from __future__ import annotations

import re
from typing import List

_UNITS = (r"percent|per ?cent|times|years?|months?|weeks?|days?|hours?|minutes?|seconds?|"
          r"dollars?|euros?|pounds?|cents?|kilograms?|kilos?|kg|grams?|pounds|lbs?|tons?|tonnes?|"
          r"kilometers?|kilometres?|km|miles?|meters?|metres?|feet|foot|inches|"
          r"degrees?|calories|kcal|people|million|billion|trillion|thousand|hundred|"
          r"mph|gigabytes?|megabytes?|gb|mb|hertz|hz|watts?|volts?")
_WORDS = (r"zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
          r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
          r"eighty|ninety|hundred|thousand|million|billion|trillion|half|double|triple")

_PATTERNS = [
    # money: $240,000  $1.5 million  $200 a month
    re.compile(r"[$€£]\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:thousand|million|billion|trillion|[kKmMbB])\b)?"
               r"(?:\s(?:a|per|each)\s(?:year|month|week|day|hour))?"),
    # percentages: 7%  7 percent  7.5 per cent
    re.compile(r"\d+(?:\.\d+)?\s?(?:%|percent|per ?cent)", re.I),
    # years and decades: 1940  2024  1920s  the 1990s
    re.compile(r"(?<![\d,.$])(?:1[0-9]{3}|20[0-9]{2})s?(?!\d|,\d|\.\d)"),
    # a digit number with a unit: 170 kilograms  3 times  12 million  4.5 hours
    re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?\s?(?:" + _UNITS + r")\b", re.I),
    # number words with a unit: four times  twelve thousand years  three percent
    re.compile(r"\b(?:(?:" + _WORDS + r")(?:[ -](?:and[ -])?(?:" + _WORDS + r"))*)\s(?:"
               + _UNITS + r")\b", re.I),
    # any other standalone number of 2+ digits: 17  1,200  3.14  (not part of a word)
    re.compile(r"(?<![\w.,$€£])\d[\d,]*(?:\.\d+)?(?![\w%,]|\.\d)"),
]
_SENTENCE_END = re.compile(r"[.!?…][\"')\]]*\s*$")


def prepare_cues(cues):
    """Join an aligned number split across cues without changing its timestamp."""
    out = [list(c) for c in cues]
    for i in range(len(out) - 1):
        if not re.search(r"\d\s*$", out[i][2]):
            continue
        j = i + 1
        while j < len(out):
            fragment = re.match(r"\s*,\s*(\d{3})(?!\d)", out[j][2])
            if not fragment:
                break
            out[i][2] = out[i][2].rstrip() + "," + fragment.group(1)
            out[j][2] = out[j][2][fragment.end():].lstrip()
            if out[j][2]:
                break
            j += 1
        if j < len(out):
            unit = re.match(r"\s*(%|years?\b|months?\b|weeks?\b|days?\b|percent\b)", out[j][2], re.I)
            if unit:
                out[i][2] = out[i][2].rstrip() + " " + unit.group(1)
                out[j][2] = out[j][2][unit.end():].lstrip()
    return [tuple(c) for c in out]


def find_values(text: str) -> List[str]:
    """The values in one cue's text, in the order they are said, each shown as
    spoken ("170 kilograms", "$240,000", "1940"). Overlapping matches keep the
    longest one."""
    # Alignment commonly inserts spaces inside a comma-separated amount.
    text = re.sub(r"(?<=\d)\s*,\s*(?=\d{3}\b)", ",", text)
    spans: list = []
    for rx in _PATTERNS:
        for mt in rx.finditer(text):
            s, e = mt.span()
            if text[s:e].strip().isdigit() and len(text[s:e].strip()) < 2:
                continue                                  # a lone single digit is not a value
            spans.append((s, e))
    spans.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    keep: list = []
    for s, e in spans:
        if keep and s < keep[-1][1]:
            if e > keep[-1][1] and s <= keep[-1][0]:
                keep[-1] = (s, e)
            continue
        keep.append((s, e))
    out: list = []
    for s, e in keep:
        v = re.sub(r"\s+", " ", text[s:e]).strip(" ,;:")
        v = re.sub(r"(\d) %", r"\1%", v)                  # "15 %" -> "15%"
        if v and v.lower() not in {x.lower() for x in out}:
            out.append(v)
    return out


def build(cues: list, shot_start: float, shot_end: float, *, style: str = "stat",
          position: str = "top-right", min_seconds: float = 2.0,
          max_seconds: float = 8.0) -> list:
    """cues = [(start_s, end_s, text), ...] of the shot, in order. Overlay items
    (seconds from the shot's start) for every cue that says a value."""
    cues = prepare_cues(cues)
    items: list = []
    last_cue_idx = len(cues) - 1
    for i, (cs, ce, text) in enumerate(cues):
        vals = find_values(text)
        if not vals:
            continue
        # the context lasts while the sentence goes on
        end = ce
        j = i
        while not _SENTENCE_END.search(cues[j][2]) and j < last_cue_idx:
            j += 1
            end = cues[j][1]
        start = max(cs, shot_start)
        end = min(max(end, start + min_seconds), start + max_seconds, shot_end)
        if end - start < 0.4:
            continue
        items.append({"text": "\n".join(vals), "style": style, "position": position,
                      "anim": "fade", "start": round(start - shot_start, 3),
                      "end": round(end - shot_start, 3), "auto": True})
    # one value at a time in a position: an earlier one ends when the next begins
    for a, b in zip(items, items[1:]):
        if a["end"] > b["start"]:
            a["end"] = b["start"]
    return [i for i in items if i["end"] - i["start"] >= 0.4]
