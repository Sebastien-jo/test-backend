"""Document model — a file being processed by the pipeline."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamps, UUIDPrimaryKey, pg_enum
from app.services.status import DocumentStatus

if TYPE_CHECKING:
    from app.models.organization import Organization
    from app.models.processing_step import ProcessingStep
    from app.models.user import User


class Document(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "documents"
    __table_args__ = (
        # Backs the tenant-scoped listing (newest first).
        Index(
            "ix_documents_organization_id_created_at",
            "organization_id",
            text("created_at DESC"),
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    filename: Mapped[str]
    storage_path: Mapped[str]
    status: Mapped[DocumentStatus] = mapped_column(
        pg_enum(DocumentStatus, "document_status"),
        default=DocumentStatus.PENDING,
    )
    # Opaque id returned by the partner's external_call; correlates the inbound
    # webhook back to this document. Unique: one partner job per document.
    partner_job_id: Mapped[str | None] = mapped_column(unique=True, index=True)

    organization: Mapped[Organization] = relationship(back_populates="documents")
    uploader: Mapped[User] = relationship()
    steps: Mapped[list[ProcessingStep]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )
