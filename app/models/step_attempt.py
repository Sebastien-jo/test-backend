"""StepAttempt model — append-only history of each pipeline step execution."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAt, UUIDPrimaryKey

if TYPE_CHECKING:
    from app.models.processing_step import ProcessingStep


class StepAttempt(UUIDPrimaryKey, CreatedAt, Base):
    __tablename__ = "step_attempts"

    step_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("processing_steps.id"), index=True)
    attempt: Mapped[int]
    error: Mapped[str | None] = mapped_column(Text)  # null = this attempt succeeded
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]

    step: Mapped[ProcessingStep] = relationship(back_populates="attempt_history")
