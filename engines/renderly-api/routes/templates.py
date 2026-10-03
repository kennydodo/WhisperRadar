from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_db
from models import Channel, Template

router = APIRouter(prefix="/api", tags=["templates"])


class TemplateCreate(BaseModel):
    text: str = Field(min_length=1)
    name: str | None = Field(default=None, max_length=120)


class TemplateOut(BaseModel):
    id: int
    channel_id: int
    name: str
    text: str

    model_config = {"from_attributes": True}


@router.get("/channels/{channel_id}/templates", response_model=list[TemplateOut])
def list_templates(channel_id: int, db: Session = Depends(get_db)):
    if db.get(Channel, channel_id) is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    templates = db.scalars(
        select(Template).where(Template.channel_id == channel_id).order_by(Template.name)
    ).all()
    return list(templates)


@router.post("/channels/{channel_id}/templates", response_model=TemplateOut, status_code=201)
def create_template(channel_id: int, body: TemplateCreate, db: Session = Depends(get_db)):
    if db.get(Channel, channel_id) is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    name = (body.name or "").strip()
    if not name:
        name = " ".join(body.text.split())[:40].rstrip(" -,;:.") or "Template"
    template = Template(channel_id=channel_id, name=name[:120], text=body.text)
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


@router.delete("/templates/{template_id}")
def delete_template(template_id: int, db: Session = Depends(get_db)):
    template = db.get(Template, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found")
    db.delete(template)
    db.commit()
    return {"ok": True}
