"""Curated "search like a niche" chips, local-first.

YouTube's search-result topic chips come from Innertube, which is fragile to
scrape. These are plain curated phrases per genre; clicking one filters the
research table (title contains) at no quota cost.
"""
from __future__ import annotations

GENERIC = ["how to", "mistakes", "secret", "why", "vs", "beginner",
           "best", "rules", "habits", "story"]

BY_GENRE = {
    "finance": ["investing", "passive income", "debt", "budget", "index fund",
                "dividend", "retire", "side hustle", "compound interest"],
    "invest": ["investing", "stocks", "etf", "dividend", "portfolio",
               "retire", "recession", "inflation", "wealth"],
    "cooking": ["easy recipe", "dinner", "one pan", "meal prep", "budget meals",
                "air fryer", "bread", "chicken", "pasta"],
    "fitness": ["workout", "fat loss", "home workout", "stretch", "mobility",
                "protein", "beginner", "abs", "walking"],
    "home": ["declutter", "minimalism", "cleaning", "organize", "habits",
             "routine", "japanese", "small space", "laundry"],
    "japan": ["japanese", "tokyo", "habits", "minimalism", "culture",
              "mistakes", "rules", "food", "travel"],
    "tech": ["review", "vs", "tips", "settings", "hidden features", "ai",
             "setup", "upgrade", "worth it"],
    "animal": ["cat", "dog", "puppy", "kitten", "wild", "rescue", "funny",
               "behavior", "story"],
    "kids": ["story", "bedtime", "learn", "song", "animals", "colors",
             "adventure", "friends", "bear"],
    "travel": ["itinerary", "budget", "hidden gems", "mistakes", "packing",
               "solo", "cheap", "guide", "vlog"],
}


def for_genre(genre: str = "", limit: int = 12) -> list[str]:
    """The chips for a genre (substring match, case-insensitive), then the
    generic ones, without repeats."""
    g = (genre or "").strip().lower()
    out: list[str] = []
    if g:
        for key, chips in BY_GENRE.items():
            if key in g or g in key:
                out += chips
    out += GENERIC
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq[:max(1, limit)]
