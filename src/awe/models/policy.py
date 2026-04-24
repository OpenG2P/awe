"""Policy blueprint tables — policy, stage, approver rule."""

from __future__ import annotations

from typing import Any, List, Optional

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, new_uuid


class ApprovalPolicy(Base, TimestampMixin):
    """Versioned policy blueprint for an artifact type.

    Editing a policy creates a new draft version; activating a new version
    archives the previously active one. In-flight requests stay pinned to the
    version they started with (via `approval_request.policy_id`).
    """

    __tablename__ = "approval_policy"
    __table_args__ = (
        UniqueConstraint("policy_key", "version", name="uq_policy_key_version"),
        Index("idx_policy_key_status", "policy_key", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    policy_key: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(1024))
    # draft | active | archived
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    # Artifact type this policy governs (e.g. "registry.change_request").
    artifact_type: Mapped[str] = mapped_column(String(128), nullable=False)
    # Who created / last edited.
    created_by: Mapped[Optional[str]] = mapped_column(String(128))

    stages: Mapped[List["ApprovalStage"]] = relationship(
        back_populates="policy",
        cascade="all, delete-orphan",
        order_by="ApprovalStage.stage_order",
        lazy="selectin",
    )


class ApprovalStage(Base, TimestampMixin):
    """One ordered stage inside a policy.

    Modes:
      * `all` — every approver must approve; any reject → stage rejected.
      * `any-N` — first N approvals complete the stage.
      * `quorum:N` — alias for any-N (kept for readability).
      * `percentage:P` — need ceil(P/100 * approvers) approvals.
    """

    __tablename__ = "approval_stage"
    __table_args__ = (
        UniqueConstraint("policy_id", "stage_order", name="uq_stage_policy_order"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    policy_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("approval_policy.id", ondelete="CASCADE"), nullable=False
    )
    stage_order: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="all")
    # N for any-N / quorum:N, P for percentage:P; null for `all`.
    mode_value: Mapped[Optional[int]] = mapped_column(Integer)
    sla_hours: Mapped[Optional[int]] = mapped_column(Integer)
    # Optional JSONLogic expression evaluated against the request context.
    # When it returns truthy, the whole stage is skipped.
    skip_if: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    # `skip` or `block` — what to do if approver resolution yields zero users.
    on_empty: Mapped[str] = mapped_column(String(8), nullable=False, default="block")

    policy: Mapped["ApprovalPolicy"] = relationship(back_populates="stages")
    rules: Mapped[List["ApproverRule"]] = relationship(
        back_populates="stage",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class ApproverRule(Base, TimestampMixin):
    """One approver-resolution rule for a stage.

    rule_type ∈ user | role | group | expression | http
    rule_value holds the type-specific payload:
      * user         → {"user_id": "u-123"}
      * role         → {"role": "PROGRAM_MANAGER"[, "client": "registry-staff-portal"]}
                       `client` optional — omit for a realm role, set to a
                       clientId to resolve a client role (e.g. roles defined
                       on a Caller's OIDC client).
      * group        → {"group": "/states/d1/officers"}
      * expression   → {"logic": <JSONLogic>}
      * http         → {"url": "https://caller/resolve"}
    """

    __tablename__ = "approver_rule"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    stage_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("approval_stage.id", ondelete="CASCADE"), nullable=False
    )
    rule_type: Mapped[str] = mapped_column(String(16), nullable=False)
    rule_value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    stage: Mapped["ApprovalStage"] = relationship(back_populates="rules")
