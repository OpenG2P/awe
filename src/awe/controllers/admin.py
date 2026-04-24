"""
Admin / ops endpoints.

Surface a focused set of operational APIs for the admin UI: inspect
webhook deliveries and trigger manual retries on failed/exhausted ones.
All gated on `awe-admin` role.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import ApprovalEvent, WebhookDelivery
from ..models.base import utcnow
from ..schemas.admin import DeliveryOut
from ..services.auth import CallerIdentity, require_role
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
    identity: CallerIdentity = Depends(require_role("awe-admin")),
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
    identity: CallerIdentity = Depends(require_role("awe-admin")),
    session: AsyncSession = Depends(get_db),
):
    delivery = await session.get(WebhookDelivery, delivery_id)
    if delivery is None:
        return error(404, "AWE-007", f"Delivery {delivery_id} not found")
    if delivery.status == "delivered":
        return error(409, "AWE-007", "Delivery already succeeded — nothing to retry")

    delivery.status = "pending"
    delivery.attempt = 0
    delivery.next_attempt_at = utcnow()
    delivery.last_error = None
    delivery.last_status_code = None
    await session.flush()

    event = await session.get(ApprovalEvent, delivery.event_id)
    return _to_out(delivery, event)
