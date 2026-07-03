"""User model — belongs to exactly one organization."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAt, UUIDPrimaryKey

if TYPE_CHECKING:
    from app.models.organization import Organization


class User(UUIDPrimaryKey, CreatedAt, Base):
    __tablename__ = "users"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    email: Mapped[str] = mapped_column(unique=True)
    hashed_password: Mapped[str]

    organization: Mapped[Organization] = relationship(back_populates="users")
