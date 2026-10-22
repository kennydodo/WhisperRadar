"""The "Every cue" brief: one image per narration cue.

Applied to the standard brief template at render time (briefs.render_brief), so
brief_template.md is never edited and every other channel keeps the brief it
has. The template's Sections 1-4 (grouping, reuse, "one cue = one image is
failure") and checklist item 2 are replaced; everything else - style, refs,
motion, output format - is the normal brief."""
from __future__ import annotations

import re

SECTIONS_1_TO_4 = """\
## SECTION 1 — NARRATION IS THE SOURCE OF TRUTH

The SRT controls narration timing and visual content. On this channel EVERY NARRATION CUE GETS ITS OWN SHOT AND ITS OWN NEW IMAGE: cue 1 is one shot, cue 2 is the next, and so on up to the last cue. There is no grouping of cues, no holding an image across cues and no reusing an image — the picture changes at every cue. The channel's maximum hold is irrelevant here (a cue is as long as it is).

## SECTION 2 — ONE CUE = ONE IMAGE

- **Every `shots[].cues` is a single cue number** (`"7"`, never `"7-9"` or `[7,8,9]`). The number of shots equals the number of cues. Ignore the multi-cue ranges in the example in Section 7 - they show the shape of a shot, not how many cues it spans here.
- **Each image shows what THAT cue says.** Read the cue's own words and picture them: its subject, action, number, object or place. Never a generic picture that would fit the next cue too.
- **Neighbouring images must differ.** When two cues say nearly the same thing, vary the angle, distance, framing, composition or focal object so the cut still reads as a change - but never repeat an image and never merge the cues.
- **A cue that is only a few words** (a list item, a one-word beat) still gets its own image: show that item or moment on its own.
- **Numbers, statistics and prices** get a clear visual of the figure's meaning; **questions** get the thing asked about; **transitions between ideas** get the scene the next idea starts in.

## SECTION 3 — HIERARCHY

1. Divide the narration into MAIN SEMANTIC BEATS (hook, problem, explanation, evidence, consequence, solution, conclusion...). Number S01, S02, ... These are only labels for the `scene` field; a beat is made of whole cues.
2. Inside each beat, every cue is one visual sub-beat: S01_01, S01_02, ... resetting per beat. The sub-beat number goes up by one for every cue of the beat.

## SECTION 4 — EVERY IMAGE IS NEW

Every cue has a new image in `images[]` with its own unique filename; no `asset` is used by two shots. Never reuse an image across cues, even for the same idea restated.

## SECTION 5 — DESIGN VISUALS THAT EXPLAIN"""

CHECK_ITEM_2 = ("2. One cue per shot: every `cues` value is a single number, the shots follow the cues "
                "1..N one by one, there are exactly as many shots as cues, and no two shots share an "
                "image. {{CHECK_HOLD}}")


def apply(text: str) -> str:
    """The standard brief template turned into the every-cue brief."""
    a = text.find("## SECTION 1 ")
    b = text.find("## SECTION 5 ")
    if a < 0 or b < a:
        raise RuntimeError("the planning brief template has no '## SECTION 1' ... '## SECTION 5' "
                           "headings, so the 'every cue' brief cannot be applied")
    text = text[:a] + SECTIONS_1_TO_4 + text[b + len("## SECTION 5 — DESIGN VISUALS THAT EXPLAIN"):]
    new, n = re.subn(r"^2\. Grouping by visual idea.*$", lambda _m: CHECK_ITEM_2, text, count=1, flags=re.M)
    if n != 1:
        raise RuntimeError("the planning brief template's checklist item 2 was not found, so the "
                           "'every cue' brief cannot be applied")
    return new
