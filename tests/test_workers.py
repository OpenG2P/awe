"""Worker loop coverage — webhook dispatcher and SLA monitor."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .conftest import auth_header


@pytest.mark.asyncio
async def test_webhook_dispatcher_tick_delivers_and_handles_errors(client) -> None:
    from awe.db import get_engine
    from awe.models import ApprovalEvent, ApprovalRequest, WebhookDelivery
    from awe.models.base import utcnow
    from awe.workers.webhook_dispatcher import _tick

    engine = get_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)

    async with sm() as session:
        req = ApprovalRequest(
            policy_id="p-disp",
            policy_key="k",
            policy_version=1,
            artifact_type="t",
            artifact_id="a1",
            source_service="svc",
            context={},
            status="pending",
            current_stage_order=1,
            callback_url="https://cb/hook",
        )
        session.add(req)
        await session.flush()
        event = ApprovalEvent(
            request_id=req.id,
            event_type="stage_started",
            payload={"stage_order": 1},
            created_at=utcnow(),
        )
        session.add(event)
        await session.flush()
        ok_delivery = WebhookDelivery(
            event_id=event.id,
            url="https://cb/hook",
            status="pending",
            attempt=0,
            next_attempt_at=utcnow() - timedelta(seconds=5),
        )
        session.add(ok_delivery)
        bad_event = ApprovalEvent(
            request_id=req.id,
            event_type="task_expired",
            payload={},
            created_at=utcnow(),
        )
        session.add(bad_event)
        await session.flush()
        fail_delivery = WebhookDelivery(
            event_id=bad_event.id,
            url="https://cb/hook",
            status="pending",
            attempt=0,
            next_attempt_at=utcnow() - timedelta(seconds=5),
        )
        session.add(fail_delivery)
        await session.commit()
        ok_id = ok_delivery.id
        fail_id = fail_delivery.id

    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        with patch(
            "awe.workers.webhook_dispatcher.webhook_svc.deliver_one",
            side_effect=[True, RuntimeError("boom")],
        ):
            await _tick(sm, batch_size=10)

    async with sm() as session:
        ok = await session.get(WebhookDelivery, ok_id)
        fail = await session.get(WebhookDelivery, fail_id)
        assert fail.last_error == "internal dispatcher error"

    await _tick(sm, batch_size=10)


@pytest.mark.asyncio
async def test_webhook_dispatcher_loop_cancelled(client) -> None:
    from awe.db import get_engine
    from awe.workers.webhook_dispatcher import webhook_dispatcher_loop

    engine = get_engine()
    with patch("awe.workers.webhook_dispatcher._tick", return_value=None):
        with patch("awe.workers.webhook_dispatcher.asyncio.sleep", side_effect=asyncio.CancelledError):
            task = asyncio.create_task(webhook_dispatcher_loop(engine))
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_sla_monitor_unknown_on_breach(client, admin_token, service_token) -> None:
    policy = {
        "policy_key": "worker.sla.unknown",
        "name": "Unknown breach",
        "artifact_type": "test",
        "stages": [
            {
                "name": "Stage",
                "stage_order": 1,
                "mode": "all",
                "sla_hours": 1,
                "on_breach": "notify",
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}},
                ],
            }
        ],
    }
    await client.post("/v1/awe/policies", json=policy, headers=auth_header(admin_token))
    await client.post(
        "/v1/awe/policies/worker.sla.unknown/versions/1/activate",
        headers=auth_header(admin_token),
    )
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "worker.sla.unknown",
            "artifact_type": "test",
            "artifact_id": "unk-1",
            "context": {},
        },
        headers=auth_header(service_token),
    )
    request_id = resp.json()["request_id"]

    from awe.db import get_engine
    from awe.models import ApprovalPolicy, ApprovalStage, ApprovalTask
    from awe.models.base import utcnow
    from awe.workers.sla_monitor import _tick

    engine = get_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        row = await session.execute(
            select(ApprovalTask).where(ApprovalTask.request_id == request_id)
        )
        task = row.scalar_one()
        task.due_at = utcnow() - timedelta(hours=1)
        from awe.models import ApprovalRequest

        req = await session.get(ApprovalRequest, request_id)
        policy_row = await session.get(ApprovalPolicy, req.policy_id)
        stage = next(s for s in policy_row.stages if s.stage_order == 1)
        stage.on_breach = "bogus_action"
        await session.commit()

    with patch("awe.workers.sla_monitor.logger") as log:
        await _tick(sm)
        assert any(
            "Unknown on_breach" in str(c.args[0])
            for c in log.warning.call_args_list
        )


@pytest.mark.asyncio
async def test_sla_monitor_loop_cancelled(client) -> None:
    from awe.db import get_engine
    from awe.workers.sla_monitor import sla_monitor_loop

    engine = get_engine()
    with patch("awe.workers.sla_monitor._tick", return_value=None):
        with patch("awe.workers.sla_monitor.asyncio.sleep", side_effect=asyncio.CancelledError):
            task = asyncio.create_task(sla_monitor_loop(engine))
            with pytest.raises(asyncio.CancelledError):
                await task
