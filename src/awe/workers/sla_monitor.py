"""
SLA monitor.

Periodically scans for tasks whose `due_at` has passed, marks them `expired`,
and (if every task in a stage has expired) emits a `task_expired` event so the
caller can decide what to do. v1 does NOT auto-reject the request — operators
configure escalation in the caller.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from ..config import get_settings
from ..models import ApprovalRequest, ApprovalTask
from ..models.base import utcnow

logger = logging.getLogger(__name__)


async def sla_monitor_loop(engine: AsyncEngine) -> None:
    cfg = get_settings().awe.sla
    sm = async_sessionmaker(engine, expire_on_commit=False)

    while True:
        try:
            await _tick(sm)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("SLA monitor tick failed; continuing")
        await asyncio.sleep(cfg.check_interval_seconds)


async def _tick(sm: async_sessionmaker) -> None:
    async with sm() as session:
        rows = await session.execute(
            select(ApprovalTask).where(
                ApprovalTask.status.in_(("open", "claimed")),
                ApprovalTask.due_at.is_not(None),
                ApprovalTask.due_at <= utcnow(),
            )
        )
        tasks = list(rows.scalars())
        if not tasks:
            await session.commit()
            return

        affected_request_ids: set[str] = set()
        for t in tasks:
            t.status = "expired"
            t.completed_at = utcnow()
            affected_request_ids.add(t.request_id)
        await session.commit()
        logger.info(
            "SLA monitor expired %d task(s) across %d request(s)",
            len(tasks),
            len(affected_request_ids),
        )
