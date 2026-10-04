from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config import MAX_UPSCALE_LEVEL
from db import get_db
from models import AppSetting

router = APIRouter(prefix="/api", tags=["settings"])

DEFAULTS = {"upscale_level": "3", "auto_download": "1", "download_dir": ""}

# The old upscale_level was a Real-ESRGAN multiplier (2x/4x); it is now a
# resolution tier (1 = 1K, 2 = 2K, 3 = 4K). The old 4x value becomes 4K.
_LEGACY_LEVELS = {4: 3}


def get_setting(db: Session, key: str) -> str:
    row = db.get(AppSetting, key)
    return row.value if row else DEFAULTS.get(key, "")


def normalize_level(value) -> int:
    """Clamp a stored/requested upscale_level to 0-3, migrating the old 4x."""
    try:
        level = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_UPSCALE_LEVEL, _LEGACY_LEVELS.get(level, level)))


def auto_process_settings(db: Session) -> tuple[int, bool, str]:
    """(upscale_level, auto_download, download_dir) for finished generations."""
    return (
        normalize_level(get_setting(db, "upscale_level")),
        get_setting(db, "auto_download") != "0",
        get_setting(db, "download_dir"),
    )


class SettingsUpdate(BaseModel):
    upscale_level: int | None = None
    auto_download: bool | None = None
    download_dir: str | None = None


@router.get("/settings")
def read_settings(db: Session = Depends(get_db)):
    return {
        "upscale_level": normalize_level(get_setting(db, "upscale_level")),
        "auto_download": get_setting(db, "auto_download") != "0",
        "download_dir": get_setting(db, "download_dir"),
    }


@router.put("/settings")
def update_settings(body: SettingsUpdate, db: Session = Depends(get_db)):
    updates = {}
    if body.upscale_level is not None:
        updates["upscale_level"] = str(normalize_level(body.upscale_level))
    if body.auto_download is not None:
        updates["auto_download"] = "1" if body.auto_download else "0"
    if body.download_dir is not None:
        updates["download_dir"] = body.download_dir.strip()
    for key, value in updates.items():
        row = db.get(AppSetting, key)
        if row is None:
            db.add(AppSetting(key=key, value=value))
        else:
            row.value = value
    db.commit()
    return read_settings(db)
