"""Organization model — the tenant boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.orm import Mapped, relationship

from app.models.base import Base, CreatedAt, UUIDPrimaryKey

if TYPE_CHECKING:
    from app.models.document import Document
    from app.models.user import User


class Organization(UUIDPrimaryKey, CreatedAt, Base):
    __tablename__ = "organizations"

    name: Mapped[str]

    users: Mapped[list[User]] = relationship(back_populates="organization")
    documents: Mapped[list[Document]] = relationship(back_populates="organization")
