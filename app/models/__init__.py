"""SQLAlchemy models.

Every model is imported here so that `Base.metadata` is fully populated on
`import app.models` — Alembic's autogenerate relies on this.
"""

from app.models.base import Base
from app.models.document import Document
from app.models.organization import Organization
from app.models.processing_step import ProcessingStep
from app.models.user import User
from app.models.webhook_event import WebhookEvent

__all__ = [
    "Base",
    "Document",
    "Organization",
    "ProcessingStep",
    "User",
    "WebhookEvent",
]
