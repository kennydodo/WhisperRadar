"""What worked on THIS channel: from the published videos that have results,
which title shapes and thumbnail layouts beat the channel's own median. The
summary goes into the packaging-plan, kit and thumbnail writers' prompts.
Small samples are said to be small; nothing is shown before 3 results."""
from __future__ import annotations

import re
import statistics

MIN_RESULTS = 3
LAYOUT_NAMES = {"character_host": "character + host", "character": "character alone",
                "host": "host alone"}


def title_features(title: str) -> dict[str, bool]:
    t = title or ""
    return {"has a number": bool(re.search(r"\d", t)),
            "is a question": t.strip().endswith("?") or bool(
                re.match(r"(?i)\s*(why|how|what|is|are|can|does|do)\b", t)),
            "short title (up to 50 characters)": len(t) <= 50,
            "long title (over 70 characters)": len(t) > 70}


def collect(cfg, conn, own_channel_id) -> list[dict]:
    """One row per published video of the channel with its best-known
    result (the latest horizon) and the packaging snapshot."""
    from . import results
    rows = []
    for p in conn.execute(
            "SELECT * FROM productions WHERE status = 'published'"
            " AND own_channel_id IS ?", (own_channel_id,)):
        have = results.recorded(conn, p["id"])
        if not have:
            continue
        horizon = [h for h, _ in results.HORIZONS if h in have][-1]
        snap = results.load_snapshot(cfg, p["id"])
        rows.append({"pid": p["id"], "title": p["title"], "horizon": horizon,
                     "views": have[horizon]["views"],
                     "thumb_layout": snap.get("thumb_layout", ""),
                     "thumb_text": snap.get("thumb_text", "")})
    return rows


def _relative(rows: list[dict]) -> list[dict]:
    """Views against the median of the SAME horizon, so a 24h number is
    never compared with a 28d one."""
    out = []
    for h in {r["horizon"] for r in rows}:
        group = [r for r in rows if r["horizon"] == h]
        if len(group) < MIN_RESULTS:
            continue
        med = statistics.median(r["views"] for r in group) or 1
        out += [dict(r, rel=r["views"] / med) for r in group]
    return out


def summary(rows: list[dict]) -> dict:
    rel = _relative(rows)
    if len(rel) < MIN_RESULTS:
        return {"n": len(rel), "best": [], "worst": [], "features": [],
                "layouts": []}
    ranked = sorted(rel, key=lambda r: r["rel"], reverse=True)
    feats = []
    for name in title_features(""):
        yes = [r["rel"] for r in rel if title_features(r["title"])[name]]
        no = [r["rel"] for r in rel if not title_features(r["title"])[name]]
        if yes and no:
            feats.append({"name": name, "n": len(yes),
                          "avg": statistics.mean(yes),
                          "others": statistics.mean(no)})
    layouts = []
    for lay in LAYOUT_NAMES:
        vals = [r["rel"] for r in rel if r["thumb_layout"] == lay]
        if vals:
            layouts.append({"layout": lay, "n": len(vals),
                            "avg": statistics.mean(vals)})
    return {"n": len(rel), "best": ranked[:3], "worst": ranked[-3:][::-1],
            "features": feats, "layouts": layouts}


def context_text(cfg, conn, own_channel_id) -> str:
    """The block for the writers' prompts, or "" with too little data."""
    s = summary(collect(cfg, conn, own_channel_id))
    if not s["best"]:
        return ""
    lines = [f"WHAT HAS WORKED ON THIS CHANNEL ({s['n']} published videos "
             "with results; 1.0x = this channel's median at the same age"
             + ("; SMALL SAMPLE, treat as a hint" if s["n"] < 8 else "") + "):"]
    for r in s["best"]:
        lines.append(f"- best: \"{r['title']}\" {r['rel']:.1f}x"
                     + (f" (thumbnail: {LAYOUT_NAMES.get(r['thumb_layout'], '?')}"
                        f", \"{r['thumb_text']}\")" if r["thumb_text"] else ""))
    for r in s["worst"]:
        if r["rel"] < 1:
            lines.append(f"- weakest: \"{r['title']}\" {r['rel']:.1f}x")
    for f in s["features"]:
        if abs(f["avg"] - f["others"]) >= 0.25:
            lines.append(f"- titles with {f['name']}: {f['avg']:.1f}x vs "
                         f"{f['others']:.1f}x without (n={f['n']})")
    for l in s["layouts"]:
        lines.append(f"- thumbnails with {LAYOUT_NAMES[l['layout']]}: "
                     f"{l['avg']:.1f}x (n={l['n']})")
    return "\n".join(lines) + "\n"
