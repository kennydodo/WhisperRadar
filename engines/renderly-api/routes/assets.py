import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from config import MAX_UPLOAD_SIZE, STORAGE_DIR
from db import get_db
from models import Asset, Channel
from services import thumbs, upscaler

router = APIRouter(prefix="/api", tags=["assets"])

ALLOWED_MIME_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


class AssetOut(BaseModel):
    id: int
    channel_id: int
    original_name: str
    url_path: str
    mime_type: str
    created_at: datetime

    model_config = {"from_attributes": True}


class AssetUpscaleRequest(BaseModel):
    tier: Literal["HD", "2K", "4K"] | None = None
    scale: int | None = None  # legacy 2x/4x request, mapped onto the nearest tier


def channel_dir(channel_id: int) -> Path:
    path = STORAGE_DIR / str(channel_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def asset_path(asset: Asset) -> Path:
    return STORAGE_DIR / str(asset.channel_id) / asset.stored_name


def _get_channel_or_404(db: Session, channel_id: int) -> Channel:
    channel = db.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    return channel


@router.get("/channels/{channel_id}/assets", response_model=list[AssetOut])
def list_assets(channel_id: int, db: Session = Depends(get_db)):
    _get_channel_or_404(db, channel_id)
    assets = db.scalars(
        select(Asset)
        .where(Asset.channel_id == channel_id)
        .order_by(Asset.created_at.desc())
    ).all()
    return list(assets)


@router.post("/channels/{channel_id}/assets", response_model=AssetOut, status_code=201)
async def upload_asset(
    channel_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    _get_channel_or_404(db, channel_id)

    mime_type = file.content_type or ""
    if mime_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{mime_type}'. Allowed: {sorted(ALLOWED_MIME_TYPES)}",
        )

    data = await file.read()
    if len(data) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    ext = ALLOWED_MIME_TYPES[mime_type]
    stored_name = f"{uuid.uuid4().hex}{ext}"
    destination = channel_dir(channel_id) / stored_name
    destination.write_bytes(data)

    asset = Asset(
        channel_id=channel_id,
        original_name=file.filename or stored_name,
        stored_name=stored_name,
        url_path=f"/storage/{channel_id}/{stored_name}",
        mime_type=mime_type,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


@router.get("/thumb/asset/{asset_id}")
def asset_thumb(asset_id: int, db: Session = Depends(get_db)):
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    path = asset_path(asset)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Asset file missing")
    try:
        thumb = thumbs.ensure_thumb(path, f"asset{asset_id}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Thumbnail failed: {exc}")
    return FileResponse(thumb, media_type="image/png")


@router.post("/assets/{asset_id}/upscale", response_model=AssetOut, status_code=201)
def upscale_asset(asset_id: int, body: AssetUpscaleRequest, db: Session = Depends(get_db)):
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    src = asset_path(asset)
    out_name = f"{uuid.uuid4().hex}.png"
    out_path = channel_dir(asset.channel_id) / out_name
    tier = upscaler.resolve_tier(body.tier, body.scale)
    try:
        upscaler.upscale(src, out_path, tier)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    stem = Path(asset.original_name).stem or f"asset-{asset.id}"
    upscaled = Asset(
        channel_id=asset.channel_id,
        original_name=f"{stem} ({tier}).png",
        stored_name=out_name,
        url_path=f"/storage/{asset.channel_id}/{out_name}",
        mime_type="image/png",
    )
    db.add(upscaled)
    db.commit()
    db.refresh(upscaled)
    return upscaled


@router.delete("/assets/{asset_id}")
def delete_asset(asset_id: int, db: Session = Depends(get_db)):
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    db.delete(asset)
    db.commit()
    Path(asset_path(asset)).unlink(missing_ok=True)
    return {"ok": True}
