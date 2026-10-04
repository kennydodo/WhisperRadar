from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_db
from models import Generation, Project

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    channel_id: int
    name: str = Field(min_length=1, max_length=120)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)


class ProjectOut(BaseModel):
    id: int
    channel_id: int
    name: str

    model_config = {"from_attributes": True}


def _get_project_or_404(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("", response_model=list[ProjectOut])
def list_projects(channel_id: int | None = None, db: Session = Depends(get_db)):
    query = select(Project).order_by(Project.id)
    if channel_id is not None:
        query = query.where(Project.channel_id == channel_id)
    return list(db.scalars(query).all())


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectCreate, db: Session = Depends(get_db)):
    exists = db.scalar(
        select(Project).where(Project.channel_id == body.channel_id, Project.name == body.name)
    )
    if exists:
        raise HTTPException(
            status_code=409, detail=f"Project '{body.name}' already exists in this channel"
        )
    project = Project(channel_id=body.channel_id, name=body.name)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(project_id: int, body: ProjectUpdate, db: Session = Depends(get_db)):
    project = _get_project_or_404(db, project_id)
    if body.name is not None and body.name != project.name:
        exists = db.scalar(
            select(Project).where(
                Project.channel_id == project.channel_id, Project.name == body.name
            )
        )
        if exists:
            raise HTTPException(
                status_code=409, detail=f"Project '{body.name}' already exists in this channel"
            )
        project.name = body.name
    db.commit()
    db.refresh(project)
    return project


@router.delete("/{project_id}")
def delete_project(project_id: int, db: Session = Depends(get_db)):
    project = _get_project_or_404(db, project_id)
    siblings = db.scalars(
        select(Project)
        .where(Project.channel_id == project.channel_id, Project.id != project.id)
        .order_by(Project.id)
    ).all()
    # Keep the generations: move them to the channel's first other project.
    if siblings:
        for gen in db.scalars(
            select(Generation).where(Generation.project_id == project.id)
        ).all():
            gen.project_id = siblings[0].id
    db.delete(project)
    db.commit()
    return {"ok": True}
