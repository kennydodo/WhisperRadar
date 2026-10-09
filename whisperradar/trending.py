"""The last "what is being watched now" search, kept on disk so the page can
show it without spending API quota again."""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

FILE = "trending.json"


def path(cfg) -> Path:
    return Path(cfg.db_path).parent / FILE


def save(cfg, query: str, days: int, videos: list[dict]) -> None:
    p = path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "query": query, "days": days, "videos": videos,
        "made_at": _dt.datetime.now(_dt.timezone.utc).isoformat()},
        indent=1, ensure_ascii=False), encoding="utf-8")


def load(cfg) -> dict:
    try:
        data = json.loads(path(cfg).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("videos"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"query": "", "days": 7, "videos": [], "made_at": None}
