"""
Admin / ops endpoints.

Surfaces the operational APIs the admin UI needs: inspect webhook
deliveries, retry failed/exhausted ones, and browse the audit log.

List/read endpoints accept `AWE_VIEWER` or `AWE_ADMIN`; mutations require
`AWE_ADMIN`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import ApprovalEvent, AuditAction, WebhookDelivery
from ..models.base import utcnow
from ..schemas.admin import AuditActionOut, DeliveryOut
from ..services import audit as audit_svc
from ..services.auth import (
    ROLE_ADMIN,
    ROLE_VIEWER,
    CallerIdentity,
    require_role,
    require_role_any,
)
from ._helpers import error

router = APIRouter(prefix="/v1/awe/admin", tags=["admin"])


def _to_out(delivery: WebhookDelivery, event: ApprovalEvent) -> DeliveryOut:
    return DeliveryOut(
        id=delivery.id,
        event_id=delivery.event_id,
        request_id=event.request_id,
        event_type=event.event_type,
        url=delivery.url,
        status=delivery.status,
        attempt=delivery.attempt,
        next_attempt_at=delivery.next_attempt_at,
        last_attempt_at=delivery.last_attempt_at,
        last_status_code=delivery.last_status_code,
        last_error=delivery.last_error,
        created_at=delivery.created_at,
        updated_at=delivery.updated_at,
    )


@router.get(
    "/deliveries",
    response_model=list[DeliveryOut],
    summary="List webhook deliveries — filter by status / request",
)
async def list_deliveries(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    request_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    identity: CallerIdentity = Depends(require_role_any(ROLE_VIEWER, ROLE_ADMIN)),
    session: AsyncSession = Depends(get_db),
):
    stmt = (
        select(WebhookDelivery, ApprovalEvent)
        .join(ApprovalEvent, ApprovalEvent.id == WebhookDelivery.event_id)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(limit)
    )
    if status_filter:
        stmt = stmt.where(WebhookDelivery.status == status_filter)
    if request_id:
        stmt = stmt.where(ApprovalEvent.request_id == request_id)
    rows = await session.execute(stmt)
    return [_to_out(d, e) for d, e in rows.all()]


@router.post(
    "/deliveries/{delivery_id}/retry",
    response_model=DeliveryOut,
    summary="Manually re-queue a delivery (resets attempts, fires on next dispatcher tick)",
)
async def retry_delivery(
    delivery_id: str,
    identity: CallerIdentity = Depends(require_role("AWE_ADMIN")),
    session: AsyncSession = Depends(get_db),
):
    delivery = await session.get(WebhookDelivery, delivery_id)
    if delivery is None:
        return error(404, "AWE-007", f"Delivery {delivery_id} not found")
    if delivery.status == "delivered":
        return error(409, "AWE-007", "Delivery already succeeded — nothing to retry")

    before_snapshot = {
        "status": delivery.status,
        "attempt": delivery.attempt,
        "last_status_code": delivery.last_status_code,
        "last_error": delivery.last_error,
    }
    delivery.status = "pending"
    delivery.attempt = 0
    delivery.next_attempt_at = utcnow()
    delivery.last_error = None
    delivery.last_status_code = None
    await session.flush()

    event = await session.get(ApprovalEvent, delivery.event_id)

    await audit_svc.record(
        session,
        identity=identity,
        action="delivery.retry",
        resource_type="delivery",
        resource_id=delivery.id,
        summary=f"Retried delivery {delivery.id[:8]}… ({event.event_type if event else 'unknown'})",
        before=before_snapshot,
        after={"status": "pending", "attempt": 0},
        metadata={"request_id": event.request_id if event else None},
    )

    return _to_out(delivery, event)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
@router.get(
    "/audit",
    response_model=list[AuditActionOut],
    summary="Browse the audit trail of admin / ops actions — filter by actor, action, resource, or date range",
)
async def list_audit(
    actor: Optional[str] = Query(default=None),
    action: Optional[str] = Query(default=None),
    resource_type: Optional[str] = Query(default=None),
    resource_id: Optional[str] = Query(default=None),
    since: Optional[datetime] = Query(
        default=None,
        description="Only return rows with occurred_at >= this (ISO 8601).",
    ),
    until: Optional[datetime] = Query(
        default=None,
        description="Only return rows with occurred_at <  this (ISO 8601).",
    ),
    limit: int = Query(default=100, ge=1, le=1000),
    identity: CallerIdentity = Depends(require_role_any(ROLE_VIEWER, ROLE_ADMIN)),
    session: AsyncSession = Depends(get_db),
):
    stmt = (
        select(AuditAction).order_by(AuditAction.occurred_at.desc()).limit(limit)
    )
    if actor:
        stmt = stmt.where(AuditAction.actor == actor)
    if action:
        stmt = stmt.where(AuditAction.action == action)
    if resource_type:
        stmt = stmt.where(AuditAction.resource_type == resource_type)
    if resource_id:
        stmt = stmt.where(AuditAction.resource_id == resource_id)
    if since:
        stmt = stmt.where(AuditAction.occurred_at >= since)
    if until:
        stmt = stmt.where(AuditAction.occurred_at < until)
    rows = await session.execute(stmt)
    return [
        AuditActionOut.model_validate(
            {
                "id": r.id,
                "occurred_at": r.occurred_at,
                "actor": r.actor,
                "actor_email": r.actor_email,
                "action": r.action,
                "resource_type": r.resource_type,
                "resource_id": r.resource_id,
                "summary": r.summary,
                "before": r.before,
                "after": r.after,
                "metadata_": r.metadata_,
            }
        )
        for r in rows.scalars()
    ]
