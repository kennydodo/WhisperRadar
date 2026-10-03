from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    assets = relationship("Asset", back_populates="channel", cascade="all, delete-orphan")
    generations = relationship("Generation", back_populates="channel", cascade="all, delete-orphan")
    templates = relationship("Template", back_populates="channel", cascade="all, delete-orphan")
