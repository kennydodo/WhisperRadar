"""Niche-specific planning: what the winning titles of THIS niche look like.

The packaging planner used to get a flat list of outlier titles from whatever
genre the channel carried (often "general"), dominated by one prolific
channel. A profile fixes three things:

- the reference pool: the SOURCE video's own channel first, then the genre of
  the source video, with a cap per channel so one channel cannot set the style;
- measured style of the pool (how they open, voice, recurring words, and how
  often they split, count or run long - reported, not enforced);
- a curated playbook per niche (the core values that niche sells, what to
  avoid). Finance first; other niches fall back to the measured style only.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter

PER_CHANNEL = 4        # titles one channel may contribute to the pool
POOL = 12              # reference titles in the pool
MIN_MULT = 2.0         # a niche winner beats its channel's norm by this much
MIN_SAMPLE = 6         # below this the profile is "thin": playbook only
MIN_CHANNELS = 3       # measured style needs this many channels, or one
                       # channel's signature ("POV: You...") becomes the niche

PLAYBOOKS = {
    "finance": {
        "name": "Personal finance",
        "values": [
            "relief from money stress",
            "fear of falling behind or being the last to know",
            "quiet status: being rich without looking rich",
            "freedom: work becoming optional",
            "the insider edge wealthy people have",
            "a mistake that is quietly costing money",
            "an old-school habit that still works",
            "challenging a belief about money, spending or ownership",
        ],
        "voice": "plain, calm, second person (you / your); money is "
                 "personal, so the title speaks to the viewer's own "
                 "situation, never to a stranger's.",
        "avoid": "guaranteed returns, get-rich-quick wording, exact "
                 "figures promised as results, named stocks or funds, and "
                 "anything that reads as personal financial advice.",
        "match": ("finance", "invest", "money", "wealth"),
    },
}

# used when the niche has no playbook of its own (or yours): values that sell
# in almost any niche, so the titler still works from angles, not from nothing
GENERIC = {
    "name": "General audience",
    "generic": True,
    "values": [
        "curiosity about how something really works",
        "a mistake that is quietly costing the viewer",
        "an insider edge most people never get",
        "relief: a problem that turns out simpler than feared",
        "fear of missing out or being the last to know",
        "challenging a belief the viewer has never questioned",
        "a small change with an outsized payoff",
    ],
    "voice": "plain, natural, spoken English; speak to the viewer's own "
             "situation (you / your).",
    "avoid": "clickbait promises the video cannot keep, shouting, and "
             "claims presented as guaranteed.",
    "match": (),
}

PLAYBOOK_FILE = "niche_playbooks.json"   # next to the database; yours

_SPLIT = re.compile(r"[:|]|\s[-–—]\s|[(\[]")
_COUNT = re.compile(r"\b\d+\s+(?:\w+\s+){0,2}?"
                    r"(ways?|reasons?|costs?|things?|signs?|steps?|tips?|"
                    r"mistakes?|secrets?|rules?|habits?|lessons?|truths?)\b",
                    re.I)
_STOP = set("the a an of to and or with for in on at by is are was were it "
            "this that you your what why how who from as but if than "
            "nobody everyone".split())


def user_playbooks(path) -> dict:
    """Playbooks you wrote: {"key": {"name", "match": [...], "values": [...],
    "voice", "avoid"}} in niche_playbooks.json. Bad entries are skipped."""
    import json
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError, TypeError):
        return {}
    out = {}
    for k, v in (data.items() if isinstance(data, dict) else []):
        if not isinstance(v, dict):
            continue
        vals = [str(x).strip() for x in v.get("values") or [] if str(x).strip()]
        match = [str(x).lower() for x in v.get("match") or [str(k)]
                 if str(x).strip()]
        if vals and match:
            out[k] = {"name": str(v.get("name") or k), "values": vals,
                      "voice": str(v.get("voice") or GENERIC["voice"]),
                      "avoid": str(v.get("avoid") or GENERIC["avoid"]),
                      "match": tuple(match), "user": True}
    return out


def extra_values(text: str) -> list[str]:
    """The "Extra core values" setting: one value per line or ';'-separated."""
    return [v.strip() for v in re.split(r"[;\n]", text or "") if v.strip()]


def playbook_for(genre: str, user: dict | None = None) -> dict | None:
    g = (genre or "").lower()
    for pb in list((user or {}).values()) + list(PLAYBOOKS.values()):
        if any(m in g for m in pb["match"]):
            return pb
    return None


def pool(items: list[dict], genre: str, source_channel_id: str = "",
         exclude_title: str = "") -> list[dict]:
    """The reference outliers: the source channel's own winners first, then
    the genre's, at most PER_CHANNEL from any one channel."""
    ex = re.sub(r"\W+", " ", (exclude_title or "").lower()).strip()
    ok = [i for i in items
          if i["multiplier"] >= MIN_MULT and not i.get("is_short")
          and re.sub(r"\W+", " ", (i["title"] or "").lower()).strip() != ex]
    ok.sort(key=lambda i: i["multiplier"], reverse=True)
    taken: dict[str, int] = {}
    out: list[dict] = []

    def take(rows):
        for i in rows:
            if len(out) >= POOL:
                return
            if taken.get(i["channel_id"], 0) >= PER_CHANNEL or i in out:
                continue
            taken[i["channel_id"]] = taken.get(i["channel_id"], 0) + 1
            out.append(i)

    if source_channel_id:
        take([i for i in ok if i["channel_id"] == source_channel_id])
    if genre:
        take([i for i in ok if (i["genre"] or "") == genre])
    if len(out) < MIN_SAMPLE:
        take(ok)
    return out


_FORMULAS = [
    ("a number-led list", re.compile(r"^\s*(\d+|one|two|three|four|five|six|"
                                    r"seven|eight|nine|ten)\b", re.I)),
    ("a question", re.compile(r"\?\s*$")),
    ("'how to' / 'how I'", re.compile(r"^\s*how (to|i|we)\b", re.I)),
    ("'why ...' explanation", re.compile(r"^\s*why\b", re.I)),
    ("'I did / I tried' first person", re.compile(r"^\s*(i|we)\b", re.I)),
    ("'The ... you ...' blunt truth", re.compile(r"^\s*the\b.*\byou", re.I)),
    ("'Stop / Never / Don't' command", re.compile(
        r"^\s*(stop|never|don't|do not|quit)\b", re.I)),
]


def formula_mix(titles: list[str]) -> list[tuple[str, int]]:
    """How the winners are built, as (formula, percent), commonest first;
    only formulas that at least two winners share."""
    t = [x for x in titles if x]
    if not t:
        return []
    counts = Counter()
    for x in t:
        for name, rx in _FORMULAS:
            if rx.search(x):
                counts[name] += 1
                break
        else:
            counts["a plain statement"] += 1
    return [(n, round(100 * c / len(t))) for n, c in counts.most_common()
            if c > 1]


def measure(titles: list[str]) -> dict:
    """Descriptive style of a set of winning titles."""
    t = [x for x in titles if x]
    if not t:
        return {"n": 0}
    n = len(t)
    openers = Counter(" ".join(x.split()[:2]).lower().rstrip(":,") for x in t)
    words = Counter(w for x in t for w in set(re.findall(r"[a-z']{4,}", x.lower()))
                    if w not in _STOP)
    return {
        "n": n,
        "median_len": int(statistics.median(len(x) for x in t)),
        "pct_split": round(100 * sum(bool(_SPLIT.search(x)) for x in t) / n),
        "pct_count": round(100 * sum(bool(_COUNT.search(x)) for x in t) / n),
        "pct_you": round(100 * sum(bool(re.search(r"\byou(r|'re|'ve)?\b", x, re.I))
                                   for x in t) / n),
        "formulas": formula_mix(t),
        "openers": [o for o, c in openers.most_common(4) if c > 1],
        "words": [w for w, c in words.most_common(8) if c > 1],
    }


def _playbook(genre: str, user: dict | None, extra: list[str] | None) -> dict:
    pb = dict(playbook_for(genre, user) or GENERIC)
    vals = list(pb["values"])
    for v in extra or []:
        if v.lower() not in {x.lower() for x in vals}:
            vals.append(v)
    pb["values"] = vals
    return pb


def build(items: list[dict], genre: str, source_channel_id: str = "",
          source_channel_name: str = "", exclude_title: str = "",
          user: dict | None = None, extra: list[str] | None = None) -> dict:
    rows = pool(items, genre, source_channel_id, exclude_title)
    titles = [r["title"] for r in rows]
    mine = [r["title"] for r in rows if r["channel_id"] == source_channel_id]
    return {
        "genre": genre,
        "playbook": _playbook(genre, user, extra),
        "source_channel": source_channel_name,
        "refs": [(r["title"], r["multiplier"]) for r in rows],
        "channels": len({r["channel_id"] for r in rows}),
        "stats": measure(titles),
        "source_stats": measure(mine),
        "thin": len(rows) < MIN_SAMPLE,
    }


def prompt_block(profile: dict | None) -> str:
    """The niche guidance for the writer and the judge ("" when there is
    nothing niche-specific to say)."""
    if not profile:
        return ""
    lines = []
    pb = profile.get("playbook")
    st = profile.get("stats") or {}
    if pb:
        lines.append(f"NICHE: {pb['name']}.")
        lines.append("Core values this niche sells (use them as ANGLES: each "
                     "title sells the whole video through a different one; "
                     "use those that fit the story): "
                     + "; ".join(pb["values"]) + ".")
        lines.append("Voice: " + pb["voice"])
        lines.append("Avoid: " + pb["avoid"])
    if (st.get("n", 0) >= MIN_SAMPLE and not profile.get("thin")
            and profile.get("channels", 0) >= MIN_CHANNELS):
        bits = []
        if st.get("openers"):
            bits.append("they often open with: " + ", ".join(
                f"\"{o}\"" for o in st["openers"]))
        if st.get("pct_you", 0) >= 40:
            bits.append(f"{st['pct_you']}% speak to \"you\"")
        if st.get("words"):
            bits.append("recurring words: " + ", ".join(st["words"]))
        if st.get("formulas"):
            bits.append("how they are built: " + ", ".join(
                f"{n} {p}%" for n, p in st["formulas"][:4]))
        if bits:
            lines.append("Measured from this niche's winners ("
                         f"{st['n']} titles, {profile['channels']} channel(s)): "
                         + "; ".join(bits) + ". Match the voice and the "
                         "ideas; the shape and length rules still apply.")
    return "\n".join(lines)
