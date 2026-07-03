"""Declarative base and shared column conventions."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {
        datetime: DateTime(timezone=True),
        uuid.UUID: Uuid(as_uuid=True),
    }


def pg_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """Map a Python StrEnum to a native Postgres enum, storing the enum *values*.

    Without `values_callable`, SQLAlchemy stores the member *names*; we want the
    lowercase string values (e.g. "ocr", not "OCR").
    """
    return Enum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


class UUIDPrimaryKey:
    """Mixin: UUID primary key generated application-side."""

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class CreatedAt:
    """Mixin: server-side creation timestamp."""

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Timestamps(CreatedAt):
    """Mixin: creation + auto-updated modification timestamps."""

    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        onupdate=func.now(),
    )
