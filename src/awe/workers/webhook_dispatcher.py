"""
Webhook dispatcher worker.

Polls `webhook_delivery` for pending rows whose `next_attempt_at` has passed,
calls `services.webhook.deliver_one()` on each, and commits in the same
transaction. Single-process for v1 — Postgres SKIP LOCKED is used for safe
concurrent claim across replicas.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from ..config import get_settings
from ..models import WebhookDelivery
from ..models.base import utcnow
from ..services import webhook as webhook_svc

logger = logging.getLogger(__name__)


async def webhook_dispatcher_loop(engine: AsyncEngine) -> None:
    """Run forever — polls + dispatches in batches."""
    cfg = get_settings().awe.webhook
    sm = async_sessionmaker(engine, expire_on_commit=False)

    while True:
        try:
            await _tick(sm, cfg.batch_size)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Webhook dispatcher tick failed; continuing")
        await asyncio.sleep(cfg.poll_interval_seconds)


async def _tick(sm: async_sessionmaker, batch_size: int) -> None:
    async with sm() as session:
        # SKIP LOCKED is Postgres-specific; on SQLite (used in tests) the dialect
        # silently ignores `with_for_update` so the loop falls back to optimistic.
        try:
            stmt = (
                select(WebhookDelivery)
                .where(
                    WebhookDelivery.status == "pending",
                    WebhookDelivery.next_attempt_at <= utcnow(),
                )
                .order_by(WebhookDelivery.next_attempt_at.asc())
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            rows = await session.execute(stmt)
        except Exception:  # noqa: BLE001
            stmt = (
                select(WebhookDelivery)
                .where(
                    WebhookDelivery.status == "pending",
                    WebhookDelivery.next_attempt_at <= utcnow(),
                )
                .order_by(WebhookDelivery.next_attempt_at.asc())
                .limit(batch_size)
            )
            rows = await session.execute(stmt)

        deliveries = list(rows.scalars())
        if not deliveries:
            await session.commit()
            return

        for d in deliveries:
            try:
                await webhook_svc.deliver_one(session, d)
            except Exception:  # noqa: BLE001
                logger.exception("Delivery %s raised", d.id)
                d.last_error = "internal dispatcher error"

        await session.commit()
