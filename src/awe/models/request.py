"""Runtime tables — request, task, decision, event."""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, new_uuid


class ApprovalRequest(Base, TimestampMixin):
    """Live instance of an approval flow.

    Pinned to a specific policy version via `policy_id` so a mid-flight policy
    update doesn't reshape stages underneath an in-flight request.
    """

    __tablename__ = "approval_request"
    __table_args__ = (
        Index("idx_request_artifact", "artifact_type", "artifact_id"),
        Index("idx_request_status", "status"),
        Index("idx_request_artifact_type", "artifact_type"),
        Index("idx_request_artifact_id", "artifact_id"),
        Index("idx_request_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    policy_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("approval_policy.id"), nullable=False
    )
    policy_key: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)

    artifact_type: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_service: Mapped[str] = mapped_column(String(128), nullable=False)
    requester: Mapped[Optional[str]] = mapped_column(String(128))

    # Snapshot of the context used for approver resolution. Never changes after
    # the request is created — stage 2+ rules resolve against this.
    context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    # pending | in_review | approved | rejected | cancelled | expired
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    current_stage_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    callback_url: Mapped[Optional[str]] = mapped_column(Text)
    # Reference to a callback_secret row — HMAC secret for webhook signing.
    callback_secret_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("callback_secret.id")
    )

    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    tasks: Mapped[List["ApprovalTask"]] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )
    decisions: Mapped[List["ApprovalDecision"]] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )
    events: Mapped[List["ApprovalEvent"]] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )


class ApprovalTask(Base, TimestampMixin):
    """One approver's to-do for one request+stage."""

    __tablename__ = "approval_task"
    __table_args__ = (
        Index("idx_task_assignee_status", "assignee", "status"),
        Index("idx_task_request_stage", "request_id", "stage_order"),
        Index("idx_task_request_id", "request_id"),
        Index("idx_task_assignee_status_created", "assignee", "status", "created_at"),
        Index("idx_task_created_at", "created_at"),
        Index(
            "idx_task_search_text_gin",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    request_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("approval_request.id", ondelete="CASCADE"),
        nullable=False,
    )
    stage_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("approval_stage.id"), nullable=False
    )
    stage_order: Mapped[int] = mapped_column(Integer, nullable=False)
    assignee: Mapped[str] = mapped_column(String(128), nullable=False)
    # Human-readable assignee name (Keycloak `name` / first+last); optional.
    assignee_name: Mapped[Optional[str]] = mapped_column(String(256))
    # `approver` (counts toward stage completion) or `observer` (comment-only).
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="approver")
    # If this task was created by delegation, the user id the task originally
    # would have gone to. Purely informational — shown in audit/UI.
    delegated_from: Mapped[Optional[str]] = mapped_column(String(128))
    # If this task was created by an admin reassignment, the user id the task
    # previously belonged to (the one whose task was closed as `reassigned`).
    reassigned_from: Mapped[Optional[str]] = mapped_column(String(128))

    # open | claimed | completed | skipped | expired | reassigned
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    decision_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("approval_decision.id")
    )
    # Denormalized from the owning request's context for inbox search.
    search_text: Mapped[Optional[str]] = mapped_column(Text)

    request: Mapped["ApprovalRequest"] = relationship(back_populates="tasks")


class ApprovalDecision(Base, TimestampMixin):
    """Append-only record of an approver's action on a task."""

    __tablename__ = "approval_decision"
    __table_args__ = (
        Index("idx_decision_request", "request_id"),
        Index("idx_decision_task_id", "task_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    request_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("approval_request.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("approval_task.id"), index=True)
    stage_order: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    # approve | reject | abstain
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    comment: Mapped[Optional[str]] = mapped_column(Text)
    attachments_ref: Mapped[Optional[str]] = mapped_column(Text)

    request: Mapped["ApprovalRequest"] = relationship(back_populates="decisions")


class ApprovalEvent(Base):
    """Append-only audit log. Every row is also a candidate webhook delivery."""

    __tablename__ = "approval_event"
    __table_args__ = (
        Index("idx_event_request_created", "request_id", "created_at"),
        Index("idx_event_request_id", "request_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    request_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("approval_request.id", ondelete="CASCADE"),
        nullable=False,
    )
    # request_created | stage_started | stage_completed | request_approved |
    # request_rejected | request_cancelled | stage_skipped | task_expired
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    # Materialised from base.utcnow — we don't use the TimestampMixin here
    # because events are append-only and only need a single timestamp.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    request: Mapped["ApprovalRequest"] = relationship(back_populates="events")
