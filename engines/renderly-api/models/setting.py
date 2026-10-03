from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class AppSetting(Base):
    """Simple key-value store for user preferences (upscale level, etc.)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(String(500), default="")
