"""
SLA monitor.

Periodically scans for approver tasks whose `due_at` has passed, marks them
`expired`, then applies the stage's `on_breach` action:

  * None / "notify"  — emit `task_expired` events per task, let the Caller
                       decide. (Original behaviour.)
  * "escalate"       — additionally resolve the stage's escalation_rules and
                       add those users as fresh approver tasks on the stage.
  * "auto_approve"   — synthesize approve-decisions for all remaining open
                       tasks on the stage (actor=`sla-monitor`) and let the
                       engine advance.
  * "auto_reject"    — symmetric: synthesize reject-decisions.

Actions fire once per stage per tick, even if multiple tasks on the stage
just expired together.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import selectinload

from ..config import get_settings
from ..models import ApprovalPolicy, ApprovalRequest, ApprovalStage, ApprovalTask
from ..models.base import utcnow
from ..services import engine as engine_svc
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
                ApprovalTask.kind == "approver",
                ApprovalTask.due_at.is_not(None),
                ApprovalTask.due_at <= utcnow(),
            )
        )
        tasks = list(rows.scalars())
        if not tasks:
            await session.commit()
            return

        by_request: Dict[str, list[ApprovalTask]] = {}
        for t in tasks:
            t.status = "expired"
            t.completed_at = utcnow()
            by_request.setdefault(t.request_id, []).append(t)
        await session.flush()

        for request_id, expired_tasks in by_request.items():
            request = await session.get(ApprovalRequest, request_id)
            if request is None or request.status not in ("pending", "in_review"):
                continue

            # Emit per-task expiry events first (Caller always sees them).
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

            # Group expired tasks by stage so we only fire the breach action once.
            stages_touched = {t.stage_order for t in expired_tasks}
            policy = await _load_policy(session, request.policy_id)
            for stage_order in stages_touched:
                stage = next(
                    (s for s in policy.stages if s.stage_order == stage_order), None
                )
                if stage is None:
                    continue
                await _apply_on_breach(session, request, stage)

        await session.commit()
        logger.info(
            "SLA monitor expired %d task(s) across %d request(s)",
            len(tasks),
            len(by_request),
        )


async def _apply_on_breach(
    session,
    request: ApprovalRequest,
    stage: ApprovalStage,
) -> None:
    action = (stage.on_breach or "notify").lower()
    if action == "notify":
        return  # already emitted task_expired events
    if action == "escalate":
        added = await engine_svc.escalate_stage(
            session, request, stage, actor="sla-monitor"
        )
        logger.info(
            "SLA escalate: stage %d of %s added %d approver(s)",
            stage.stage_order,
            request.id,
            added,
        )
        return
    if action in ("auto_approve", "auto_reject"):
        synthetic_action = "approve" if action == "auto_approve" else "reject"
        await engine_svc.synthesize_decision(
            session,
            request,
            stage,
            action=synthetic_action,
            actor="sla-monitor",
            reason=f"SLA breach auto-{synthetic_action}",
        )
        logger.info(
            "SLA %s: stage %d of %s",
            action,
            stage.stage_order,
            request.id,
        )
        return
    logger.warning(
        "Unknown on_breach='%s' on stage %d of policy %s — treating as notify",
        action,
        stage.stage_order,
        request.policy_id,
    )


async def _load_policy(session, policy_id: str) -> ApprovalPolicy:
    row = await session.execute(
        select(ApprovalPolicy)
        .options(
            selectinload(ApprovalPolicy.stages).selectinload(ApprovalStage.rules)
        )
        .where(ApprovalPolicy.id == policy_id)
    )
    return row.scalar_one()
