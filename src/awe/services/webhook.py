"""
Webhook delivery — HMAC signing + per-attempt POST.

The dispatcher worker polls `webhook_delivery` for due rows, calls
`deliver_one()` on each, and updates the row in place. This module owns the
contract; scheduling lives in `awe.workers.webhook_dispatcher`.

Signature scheme (matches the design handoff):
  X-Approval-Event-Id   : delivery.event_id
  X-Approval-Timestamp  : unix seconds at attempt time
  X-Approval-Signature  : "sha256=" + HMAC_SHA256(secret, timestamp + "." + body)

The timestamp is included in the signed payload so a captured body can't be
replayed at a later time without invalidating the MAC. Callers should reject
deliveries with timestamps more than ~5 minutes off wall clock.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import timedelta
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import ApprovalEvent, ApprovalRequest, CallbackSecret, WebhookDelivery
from ..models.base import utcnow
from ..schemas.callback import WebhookEvent

logger = logging.getLogger(__name__)


def sign_body(secret: str, timestamp: int, body_bytes: bytes) -> str:
    mac = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + body_bytes,
        hashlib.sha256,
    )
    return f"sha256={mac.hexdigest()}"


async def _load_secret(
    session: AsyncSession, secret_id: Optional[str]
) -> Optional[str]:
    """Look up the raw secret for a delivery.

    NOTE: in production the raw secret would live in a vault — `secret_hash`
    on `callback_secret` is a placeholder for that integration. For v1 we
    return the hash field directly so the docker-compose dev stack works
    end-to-end. Replace with a vault lookup before production.
    """
    if not secret_id:
        return None
    row = await session.execute(
        select(CallbackSecret).where(CallbackSecret.id == secret_id)
    )
    sec = row.scalar_one_or_none()
    return sec.secret_hash if sec else None


async def deliver_one(
    session: AsyncSession,
    delivery: WebhookDelivery,
) -> bool:
    """POST one webhook attempt. Returns True on success.

    Mutates `delivery` in place; the caller is expected to commit.
    """
    cfg = get_settings().awe.webhook

    event_row = await session.get(ApprovalEvent, delivery.event_id)
    if event_row is None:
        delivery.status = "exhausted"
        delivery.last_error = "event row missing"
        return False

    request_row = await session.get(ApprovalRequest, event_row.request_id)
    if request_row is None:
        delivery.status = "exhausted"
        delivery.last_error = "request row missing"
        return False

    body = WebhookEvent(
        event_id=event_row.id,
        event_type=event_row.event_type,
        request_id=request_row.id,
        artifact_type=request_row.artifact_type,
        artifact_id=request_row.artifact_id,
        status=request_row.status,
        stage_order=request_row.current_stage_order,
        actor=event_row.payload.get("actor") if isinstance(event_row.payload, dict) else None,
        occurred_at=event_row.created_at,
    ).model_dump(mode="json")

    body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
    now = utcnow()
    ts = int(now.timestamp())
    secret = await _load_secret(session, request_row.callback_secret_id)

    headers = {
        "Content-Type": "application/json",
        "X-Approval-Event-Id": event_row.id,
        "X-Approval-Timestamp": str(ts),
    }
    if secret:
        headers["X-Approval-Signature"] = sign_body(secret, ts, body_bytes)

    delivery.attempt += 1
    delivery.last_attempt_at = now

    try:
        async with httpx.AsyncClient(timeout=cfg.timeout_seconds) as client:
            resp = await client.post(delivery.url, content=body_bytes, headers=headers)
        delivery.last_status_code = resp.status_code
        if 200 <= resp.status_code < 300:
            delivery.status = "delivered"
            delivery.last_error = None
            return True
        delivery.last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
    except httpx.HTTPError as e:
        delivery.last_status_code = None
        delivery.last_error = str(e)[:500]

    # Failed attempt — schedule retry or mark exhausted.
    if delivery.attempt >= cfg.max_attempts:
        delivery.status = "exhausted"
        logger.warning(
            "Webhook delivery %s exhausted after %d attempts to %s",
            delivery.id,
            delivery.attempt,
            delivery.url,
        )
        return False

    backoff_idx = min(delivery.attempt - 1, len(cfg.backoff_seconds) - 1)
    backoff = cfg.backoff_seconds[backoff_idx]
    delivery.status = "pending"
    delivery.next_attempt_at = now + timedelta(seconds=backoff)
    return False
