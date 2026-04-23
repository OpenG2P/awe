"""SLA monitor: expiry marks tasks, emits event, enqueues webhook delivery."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .conftest import auth_header


def _sla_policy() -> dict:
    return {
        "policy_key": "registry.cr.sla",
        "name": "CR with SLA",
        "artifact_type": "registry.change_request",
        "stages": [
            {
                "name": "Officers",
                "stage_order": 1,
                "mode": "any-n",
                "mode_value": 1,
                "sla_hours": 1,
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}},
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_sla_expiry_flips_task_emits_event_enqueues_webhook(
    client, admin_token, service_token
) -> None:
    # 1. Activate policy + create a request with a callback_url.
    await client.post(
        "/v1/awe/policies", json=_sla_policy(), headers=auth_header(admin_token)
    )
    await client.post(
        "/v1/awe/policies/registry.cr.sla/versions/1/activate",
        headers=auth_header(admin_token),
    )
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "registry.cr.sla",
            "artifact_type": "registry.change_request",
            "artifact_id": "cr-sla-1",
            "context": {},
            "callback_url": "https://registry.local/internal/approval-callbacks",
        },
        headers=auth_header(service_token),
    )
    assert resp.status_code == 201, resp.text
    request_id = resp.json()["request_id"]

    # 2. Force the task's `due_at` into the past so the monitor picks it up.
    from awe.db import get_engine
    from awe.models import ApprovalEvent, ApprovalTask, WebhookDelivery
    from awe.models.base import utcnow

    engine = get_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)

    async with sm() as session:
        row = await session.execute(
            select(ApprovalTask).where(ApprovalTask.request_id == request_id)
        )
        task = row.scalar_one()
        task.due_at = utcnow() - timedelta(hours=2)
        await session.commit()
        task_id = task.id

    # 3. Tick the SLA monitor once.
    from awe.workers.sla_monitor import _tick

    await _tick(sm)

    # 4. Assert task expired.
    async with sm() as session:
        task = await session.get(ApprovalTask, task_id)
        assert task.status == "expired"
        assert task.completed_at is not None

        # 5. Assert a task_expired event landed with useful payload.
        events = (
            await session.execute(
                select(ApprovalEvent)
                .where(ApprovalEvent.request_id == request_id)
                .where(ApprovalEvent.event_type == "task_expired")
            )
        ).scalars().all()
        assert len(events) == 1
        payload = events[0].payload
        assert payload["task_id"] == task_id
        assert payload["assignee"] == "u-alice"
        assert payload["stage_order"] == 1
        assert "due_at" in payload

        # 6. Assert a webhook_delivery was enqueued for that event.
        deliveries = (
            await session.execute(
                select(WebhookDelivery).where(WebhookDelivery.event_id == events[0].id)
            )
        ).scalars().all()
        assert len(deliveries) == 1
        assert deliveries[0].status == "pending"
        assert deliveries[0].url == "https://registry.local/internal/approval-callbacks"


@pytest.mark.asyncio
async def test_sla_tick_noop_when_no_expired_tasks(client, admin_token) -> None:
    from awe.db import get_engine
    from awe.workers.sla_monitor import _tick

    engine = get_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)
    # No tasks at all — tick should return cleanly without errors.
    await _tick(sm)
