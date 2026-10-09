"""A local keyword "demand" signal. The YouTube Data API has no search
volume, so this combines two free, honest signals:

* YouTube search autocomplete - the phrases people actually type that start
  with your phrase (more, and earlier, suggestions = more demand);
* how many of the niche's own winning titles already name the phrase.

The result is a 0-100 score with the evidence, never a search volume.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

SUGGEST_URL = ("https://suggestqueries.google.com/complete/search?"
               "client=firefox&ds=yt&q=")


def _default_opener(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode("utf-8", "replace")


def suggestions(phrase: str, opener=None) -> list[str]:
    """Autocomplete phrases for `phrase` (empty list when unreachable)."""
    phrase = (phrase or "").strip()
    if not phrase:
        return []
    try:
        raw = (opener or _default_opener)(SUGGEST_URL
                                         + urllib.parse.quote(phrase))
        data = json.loads(raw)
        return [str(s) for s in data[1] if str(s).strip()][:15]
    except Exception:  # noqa: BLE001 - offline / changed format = no signal
        return []


def title_counts(items: list[dict], phrase: str,
                 min_multiplier: float = 3.0) -> tuple[int, int]:
    """(outlier titles naming the phrase, all titles naming it)."""
    p = (phrase or "").strip().lower()
    if not p:
        return 0, 0
    allc = outc = 0
    for i in items:
        if p in (i.get("title") or "").lower():
            allc += 1
            if i.get("multiplier", 0) >= min_multiplier:
                outc += 1
    return outc, allc


def score(phrase: str, sugg: list[str], items: list[dict]) -> dict:
    """0-100 demand score with its evidence and a plain label."""
    p = (phrase or "").strip().lower()
    exact = [s for s in sugg if s.lower().startswith(p)] if p else []
    # 15 suggestions is the cap: how many came back, and is the phrase itself
    # the first thing YouTube offers (people type exactly this)
    sugg_pts = min(len(exact), 10) * 5                       # 0-50
    first_pts = 15 if sugg and sugg[0].lower() == p else 0   # 0-15
    outc, allc = title_counts(items, phrase)
    niche_pts = min(outc, 7) * 5                             # 0-35
    total = sugg_pts + first_pts + niche_pts
    label = ("strong" if total >= 65 else "decent" if total >= 35
             else "weak" if total > 0 else "no signal")
    return {"phrase": phrase, "score": total, "label": label,
            "suggestions": sugg, "outlier_titles": outc, "all_titles": allc,
            "note": "A local estimate from autocomplete and our own winning "
                    "titles - not a search volume."}
