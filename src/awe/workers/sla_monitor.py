"""
SLA monitor.

Periodically scans for tasks whose `due_at` has passed, marks them
`expired`, appends a `task_expired` event per task, and enqueues a webhook
delivery so the caller (Registry/PBMS/…) can decide what to do next
(reassign, escalate, cancel, nudge). AWE itself never auto-rejects — SLA
response policy lives in the caller.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from ..config import get_settings
from ..models import ApprovalRequest, ApprovalTask
from ..models.base import utcnow
from ..services.engine import emit_event

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

        # Group by request so each request is loaded once.
        by_request: dict[str, list[ApprovalTask]] = {}
        for t in tasks:
            t.status = "expired"
            t.completed_at = utcnow()
            by_request.setdefault(t.request_id, []).append(t)
        await session.flush()

        for request_id, expired_tasks in by_request.items():
            request = await session.get(ApprovalRequest, request_id)
            if request is None:
                continue
            for task in expired_tasks:
                await emit_event(
                    session,
                    request,
                    "task_expired",
                    {
                        "task_id": task.id,
                        "stage_order": task.stage_order,
                        "assignee": task.assignee,
                        "due_at": task.due_at.isoformat() if task.due_at else None,
                    },
                )

        await session.commit()
        logger.info(
            "SLA monitor expired %d task(s) across %d request(s)",
            len(tasks),
            len(by_request),
        )
