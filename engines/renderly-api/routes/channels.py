import io
import re
import shutil
import zipfile

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from config import STORAGE_DIR
from db import get_db
from models import Channel, Generation

router = APIRouter(prefix="/api/channels", tags=["channels"])


class ChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)


class ChannelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)


class ChannelOut(BaseModel):
    id: int
    name: str
    description: str

    model_config = {"from_attributes": True}


def _get_channel_or_404(db: Session, channel_id: int) -> Channel:
    channel = db.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    return channel


@router.get("", response_model=list[ChannelOut])
def list_channels(db: Session = Depends(get_db)):
    channels = db.scalars(select(Channel).order_by(Channel.name)).all()
    return list(channels)


@router.post("", response_model=ChannelOut, status_code=201)
def create_channel(body: ChannelCreate, db: Session = Depends(get_db)):
    exists = db.scalar(select(Channel).where(Channel.name == body.name))
    if exists:
        raise HTTPException(status_code=409, detail=f"Channel '{body.name}' already exists")
    channel = Channel(name=body.name, description=body.description)
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return channel


@router.get("/{channel_id}", response_model=ChannelOut)
def get_channel(channel_id: int, db: Session = Depends(get_db)):
    return _get_channel_or_404(db, channel_id)


@router.patch("/{channel_id}", response_model=ChannelOut)
def update_channel(channel_id: int, body: ChannelUpdate, db: Session = Depends(get_db)):
    channel = _get_channel_or_404(db, channel_id)
    if body.name is not None and body.name != channel.name:
        exists = db.scalar(select(Channel).where(Channel.name == body.name))
        if exists:
            raise HTTPException(status_code=409, detail=f"Channel '{body.name}' already exists")
        channel.name = body.name
    if body.description is not None:
        channel.description = body.description
    db.commit()
    db.refresh(channel)
    return channel


@router.get("/{channel_id}/export")
def export_channel(channel_id: int, db: Session = Depends(get_db)):
    channel = _get_channel_or_404(db, channel_id)
    generations = list(
        db.scalars(
            select(Generation)
            .where(Generation.channel_id == channel_id)
            .where(Generation.status == "done")
            .order_by(Generation.created_at)
        ).all()
    )
    if not generations:
        raise HTTPException(status_code=404, detail="Channel has no generated images to export")

    buffer = io.BytesIO()
    used_names: set[str] = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for generation in generations:
            path = STORAGE_DIR / (generation.image_url or "").removeprefix("/storage/")
            if not path.exists():
                continue
            base = re.sub(r'[\\/:*?"<>|]+', "_", (generation.name or "").strip())
            if not base:
                base = f"generation-{generation.id}"
            filename = f"{base}.png"
            counter = 2
            while filename.lower() in used_names:
                filename = f"{base}-{counter}.png"
                counter += 1
            used_names.add(filename.lower())
            zf.write(path, arcname=filename)
    zf_name = re.sub(r'[\\/:*?"<>|]+', "_", channel.name) or f"channel-{channel_id}"

    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zf_name}-images.zip"'},
    )


@router.delete("/{channel_id}")
def delete_channel(channel_id: int, db: Session = Depends(get_db)):
    channel = _get_channel_or_404(db, channel_id)
    for asset in list(channel.assets):
        db.delete(asset)
    for generation in list(channel.generations):
        db.delete(generation)
    db.delete(channel)
    db.commit()
    shutil.rmtree(STORAGE_DIR / str(channel_id), ignore_errors=True)
    return {"ok": True}
