"""ProcessingStep model — one pipeline step for a document."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamps, UUIDPrimaryKey, pg_enum
from app.services.status import StepName, StepStatus

if TYPE_CHECKING:
    from app.models.document import Document


class ProcessingStep(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "processing_steps"
    __table_args__ = (
        # A document has at most one row per step name.
        UniqueConstraint("document_id", "name", name="uq_processing_steps_document_name"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    name: Mapped[StepName] = mapped_column(pg_enum(StepName, "step_name"))
    status: Mapped[StepStatus] = mapped_column(
        pg_enum(StepStatus, "step_status"),
        default=StepStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]

    document: Mapped[Document] = relationship(back_populates="steps")
