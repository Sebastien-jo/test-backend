"""ProcessingStep model — one pipeline step for a document."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamps, UUIDPrimaryKey, pg_enum
from app.services.status import StepName, StepStatus

if TYPE_CHECKING:
    from app.models.document import Document
    from app.models.step_attempt import StepAttempt


class ProcessingStep(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "processing_steps"
    __table_args__ = (
        UniqueConstraint("document_id", "name", name="uq_processing_steps_document_name"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    name: Mapped[StepName] = mapped_column(pg_enum(StepName, "step_name"))
    status: Mapped[StepStatus] = mapped_column(
        pg_enum(StepStatus, "step_status"),
        default=StepStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    result: Mapped[Any | None] = mapped_column(JSONB)

    document: Mapped[Document] = relationship(back_populates="steps")
    attempt_history: Mapped[list[StepAttempt]] = relationship(
        back_populates="step",
        order_by="StepAttempt.attempt",
        cascade="all, delete-orphan",
    )
