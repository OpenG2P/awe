"""Append-only audit trail of admin / ops actions in AWE.

Distinct from `approval_event` (which captures the workflow state machine
for each request). This table records *who changed configuration / acted on
shared state* — policy CRUD, request cancellation, delivery retries, etc.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_uuid


class AuditAction(Base):
    """One row per admin / ops action.

    `action` follows a `<resource>.<verb>` convention:
      * `policy.create`, `policy.update`, `policy.add_version`,
        `policy.activate`, `policy.deactivate`
      * `request.cancel`
      * `delivery.retry`
    `before` and `after` snapshots are JSON dumps of the resource pre/post
    change (null on creates / one-shot ops).
    """

    __tablename__ = "audit_action"
    __table_args__ = (
        Index("idx_audit_occurred_at", "occurred_at"),
        Index("idx_audit_actor", "actor"),
        Index("idx_audit_resource", "resource_type", "resource_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # JWT `sub` of the user / service that performed the action.
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    # Display name (typically email from the JWT) — convenience for the UI.
    actor_email: Mapped[Optional[str]] = mapped_column(String(255))

    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # JSON snapshots of the resource state. Both nullable: creates have no
    # `before`, one-shot actions (e.g. retry) may have a tiny diff that lives
    # entirely in metadata.
    before: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    after: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)

    # Free-form context: request id, IP, comment, error reason, etc.
    metadata_: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSON)

    # Human-readable summary for UI display ("Activated registry.cr.v1 v2").
    summary: Mapped[Optional[str]] = mapped_column(Text)
