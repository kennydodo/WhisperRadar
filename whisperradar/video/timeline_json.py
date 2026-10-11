"""timeline.json load/save (ImgToVideo.Core.Serialization.TimelineJson)."""
import json
from pathlib import Path

from .models import Timeline


def save(timeline: Timeline, path) -> None:
    Path(path).write_text(json.dumps(timeline.to_dict(), indent=2), encoding="utf-8")


def load_from_json(text: str) -> Timeline:
    return Timeline.from_dict(json.loads(text))


def load(path) -> Timeline:
    return load_from_json(Path(path).read_text(encoding="utf-8-sig"))
