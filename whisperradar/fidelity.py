"""Does each image prompt show what its own cues say? Free local checks.

The planner writes, for every new image, a short `"shows"` list BEFORE the
prompt: the 2-4 concrete, visible things the narration at that shot's cues
names. That gives the app something it can check without a chat round:

    grounded   at least one `shows` item uses the narration's own words at
               those cues (the other items name other literal things those cues name);
    drifted    none does, but the items match the previous or next shot's
               cues - the classic off-by-one cue-range mistake;
    in prompt  every `shows` item is actually written into the prompt;
    reuse      an image shown again over other cues still shows something
               those cues name;
    filler     the prompt adds a gesture the cues never mention (waving,
               pointing, a thumbs-up) instead of the action they narrate.

Everything is word matching on the narration, so it is deliberately lenient:
a fault means "plainly not about these cues", never "could be better" - that
judgement stays with the reviewer. Off unless `studio.prompt_fidelity` is set
in config.yaml. No model, browser or paid service is called.
"""
from __future__ import annotations

import re

MIN_ITEMS, MAX_ITEMS = 2, 4
ITEM_MAX_CHARS = 60
FAULT_LIMIT = 24

_STOP = frozenset("""a an the and or but of to in on at by for with from into over under up down
out off as is are was were be been being it its this that these those there here he she they them
his her their our your my we you i who whom which what when where why how not no nor so than then
too very just only also both each every all any some more most much many few one two three first
second next last new old own same other another such can could will would shall should may might
must do does did done have has had having about above after again against before below between
during through while if because until near beside behind front back left right top bottom side
wide close shot view scene image picture showing shows shown visible""".split())


# Filler gestures that stand in for the narrated action. A fault only when the
# prompt AFFIRMS a character making the gesture (not "ocean waves", "an arrow
# pointing to", or "no waving") and the narration at the shot's cues does not
# itself contain the stem.
_NEGATION = re.compile(
    r"\b(?:no|not|never|without|avoid|avoiding|don'?t|do not|doesn'?t|nobody|"
    r"nor|neither|ban(?:ned)?|instead of)\b[^.;:\n]{0,40}$", re.I)
_INANIMATE = re.compile(
    r"\b(?:arrows?|lines?|needles?|signs?|signposts?|markers?|chevrons?|labels?|"
    r"indicators?|paths?|roads?|compass|beams?|streaks?|trends?|curves?|bars?|"
    r"graphs?|charts?|ocean|sea|water|heat|radio|sound|light|flags?)\s+(?:\w+\s+)?$",
    re.I)
_FILLERS = (
    (re.compile(r"\bwav(?:es|ed|ing)\s+(?:at|to|hello|goodbye|his|her|their|"
                r"a hand|an arm|one hand|both|the camera|the viewer)\b"
                r"|\b(?:he|she|they|man|woman|boy|girl|person|character|friend|"
                r"customer|presenter|joe)\s+wav(?:es|ed)\b", re.I),
     "wav", "waving"),
    (re.compile(r"\bpoint(?:s|ed|ing)\s+(?:at|to|toward|towards|up|down|out|"
                r"ahead|forward)\b", re.I), "point", "pointing"),
    (re.compile(r"\bthumbs?[- ]?up\b", re.I), "thumb", "a thumbs-up"),
    (re.compile(r"\bgestur(?:es|ed|ing)\b", re.I), "gestur", "gesturing"),
)


def _affirmed(pat, text) -> bool:
    for m in pat.finditer(text):
        before = text[:m.start()]
        if _NEGATION.search(before[-60:]) or _INANIMATE.search(before[-30:]):
            continue
        return True
    return False


def filler_gestures(prompt, narration) -> list[str]:
    """Names of filler gestures a character makes in the prompt that the
    narration never mentions."""
    said = str(narration or "").lower()
    text = str(prompt or "")
    return [name for pat, stem, name in _FILLERS
            if stem not in said and _affirmed(pat, text)]


def _stem(word: str) -> str:
    for end in ("ies", "ing", "ed", "es", "s"):
        if word.endswith(end) and len(word) - len(end) >= 3:
            return word[:-len(end)] + ("y" if end == "ies" else "")
    return word


def content_words(text) -> set[str]:
    """The words that carry meaning, lightly stemmed. Numbers are left out: a
    figure is spoken as words and written as digits, so it cannot be matched."""
    return {_stem(w) for w in re.findall(r"[a-z][a-z'\-]*", str(text or "").lower()
                                         .replace("’", "'"))
            if w not in _STOP and len(w) > 2}


def clean_shows(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in (value or []) if str(v or "").strip()] \
        if isinstance(value, list) else []


def _named(item: str, words: set[str]) -> bool:
    """True when the narration (or prompt) names this item: at least half of
    its meaningful words are there. An item with no such words always passes."""
    need = content_words(item)
    return not need or 2 * len(need & words) >= len(need)


def _range(shot) -> tuple[int, int] | None:
    from . import studio
    return studio.cue_range(shot.get("cues"))


def faults(data: dict, cues: list[dict], limit: int = FAULT_LIMIT) -> list[str]:
    """Faults for the writer; each names its asset, so the fix request shows
    that shot and its narration."""
    text = {int(c["index"]): str(c.get("text") or "") for c in cues}
    shots = [s for s in (data.get("shots") or []) if isinstance(s, dict)]
    images = {str(i.get("file")): i for i in (data.get("images") or [])
              if isinstance(i, dict) and i.get("file")}

    def said(shot) -> set[str]:
        rng = _range(shot)
        return content_words(" ".join(
            text.get(c, "") for c in range(rng[0], rng[1] + 1))) if rng else set()

    def label(shot) -> str:
        rng = _range(shot)
        return f"cues {rng[0]}-{rng[1]}" if rng and rng[0] != rng[1] else \
            (f"cue {rng[0]}" if rng else "its cues")

    out: list[str] = []
    first_use: dict[str, int] = {}
    for n, shot in enumerate(shots):
        asset = str(shot.get("asset") or "")
        image = images.get(asset)
        if not image:
            continue                      # an orphan asset is a structural fault
        shows = clean_shows(image.get("shows"))
        own = said(shot)
        if asset in first_use:
            # the picture is shown again over other cues
            if shows and own and not any(_named(i, own) and content_words(i) for i in shows):
                out.append(
                    f"{asset} is reused at {label(shot)}, but nothing it shows "
                    f"({'; '.join(shows)}) is named there. Give those cues their own image.")
            continue
        first_use[asset] = n
        rng_ = _range(shot)
        spoken = " ".join(text.get(c, "") for c in range(rng_[0], rng_[1] + 1)) if rng_ else ""
        extra = filler_gestures(image.get("prompt"), spoken)
        if extra:
            out.append(
                f"{asset}: its prompt has {' and '.join(extra)}, which the narration at "
                f"{label(shot)} never mentions. Show the action those cues narrate "
                f"instead, performed by the subject.")
        if not MIN_ITEMS <= len(shows) <= MAX_ITEMS:
            out.append(
                f"{asset}: add \"shows\" to this image - {MIN_ITEMS} to {MAX_ITEMS} short "
                f"concrete, visible things the narration at {label(shot)} names.")
            continue
        long = [i for i in shows if len(i) > ITEM_MAX_CHARS]
        if long:
            out.append(f"{asset}: each \"shows\" item is a few words, not a sentence "
                       f"(over {ITEM_MAX_CHARS} characters: \"{long[0][:40]}...\").")
            continue
        prompt_words = content_words(image.get("prompt"))
        absent = [i for i in shows if not _named(i, prompt_words)]
        if absent:
            names = "; ".join(f'"{i}"' for i in absent)
            out.append(
                f"{asset}: its prompt does not mention {names}, which its own "
                f"\"shows\" list says the image must show. Write it into the prompt.")
        if own and not any(_named(i, own) and content_words(i) for i in shows):
            where = ""
            for other, word in ((n + 1, "next"), (n - 1, "previous")):
                if 0 <= other < len(shots) and any(
                        _named(i, said(shots[other])) and content_words(i) for i in shows):
                    where = (f" They match {label(shots[other])} (the {word} shot) - "
                             f"this prompt seems to describe the wrong cues.")
                    break
            out.append(
                f"{asset}: nothing in its \"shows\" list ({'; '.join(shows)}) is named in the "
                f"narration at {label(shot)}.{where} Show what those cues actually say.")
    if len(out) > limit:
        out = out[:limit] + [f"...and {len(out) - limit} more images with the same kind of fault."]
    return out


def brief_block() -> str:
    """Appended to the planning brief: the extra field and what it is for."""
    return (
        "\n\n---\n\nEACH IMAGE SHOWS WHAT ITS OWN CUES SAY (checked by the app):\n"
        "Every new image entry (in beats: every beat) carries one more field, written BEFORE "
        "its prompt: \"shows\": a list of " f"{MIN_ITEMS} to {MAX_ITEMS} short items (a few words "
        "each) naming the concrete, visible things the narration at THAT shot's cues talks about - "
        "the subject, the object, the action, the place. At least one item must use the "
        "narration's own words from those cues; the other items must also be literal things those cues talk about, "
        "never a symbol or a metaphor. Then write the prompt so that every "
        "item in \"shows\" is plainly in it. Example: narration \"a branch snaps and the deer "
        "freeze at the edge of the clearing\" -> \"shows\": [\"deer frozen mid-step\", "
        "\"snapped branch\", \"edge of the clearing\"]. An image whose \"shows\" comes from other "
        "cues than its own, or whose prompt leaves an item out, is sent back. Reuse an image only "
        "over cues that still name something in its \"shows\"; otherwise those cues get a new image. "
        "\"shows\" and the prompt hold ONLY what those cues name or what the action needs: no "
        "waving, pointing or other filler gesture, no figurative language drawn as scenery, and no unrelated prop or decoration."
    )
