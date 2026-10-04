from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base


class Generation(Base):
    __tablename__ = "generations"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), default="")
    prompt: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(120))

    # Reference configuration (actually used for this generation)
    ref_asset_ids: Mapped[str] = mapped_column(Text, default="[]")
    ref_generation_ids: Mapped[str] = mapped_column(Text, default="[]")
    aspect_ratio: Mapped[str] = mapped_column(String(20), default="16:9")
    ref_strength: Mapped[str] = mapped_column(String(20), default="balanced")
    image_size: Mapped[str] = mapped_column(String(20), default="1K")

    status: Mapped[str] = mapped_column(String(20), default="pending")  # done | error
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    hidden: Mapped[bool] = mapped_column(default=False)
    # Removed from the channel's "Recent generations" strip (still in library)
    recent_removed: Mapped[bool] = mapped_column(default=False)
    # Media organization (Flow-style tabs): image | character | video
    category: Mapped[str] = mapped_column(String(20), default="image")
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True, nullable=True
    )
    batch_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    channel = relationship("Channel", back_populates="generations")
