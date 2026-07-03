"""WebhookEvent model — append-only audit trail of inbound partner webhooks."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKey


class WebhookEvent(UUIDPrimaryKey, Base):
    __tablename__ = "webhook_events"

    # Indexed but *not* unique
    job_id: Mapped[str] = mapped_column(index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    signature_valid: Mapped[bool] = mapped_column(Boolean)
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
