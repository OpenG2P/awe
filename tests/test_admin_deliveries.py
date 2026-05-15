"""Admin/ops endpoints: list webhook deliveries + manual retry."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .conftest import auth_header


def _policy_with_sla() -> dict:
    return {
        "policy_key": "registry.cr.admin",
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


async def _seed_exhausted_delivery(request_id: str):
    """Pretend the SLA monitor ran and the dispatcher exhausted the retries.

    A `POST /requests` already emits `request_created` + `stage_started` each
    of which enqueue a pending delivery; the SLA tick adds a `task_expired`
    delivery. Flip them all to `exhausted` (no HTTP target in tests) and
    return the `task_expired` one — that's the one the assertions check.
    """
    from awe.db import get_engine
    from awe.models import ApprovalEvent, ApprovalTask, WebhookDelivery
    from awe.models.base import utcnow
    from awe.workers.sla_monitor import _tick

    engine = get_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)

    async with sm() as session:
        rows = await session.execute(
            select(ApprovalTask).where(ApprovalTask.request_id == request_id)
        )
        for task in rows.scalars():
            task.due_at = utcnow() - timedelta(hours=2)
        await session.commit()

    await _tick(sm)

    async with sm() as session:
        rows = await session.execute(
            select(WebhookDelivery, ApprovalEvent).join(
                ApprovalEvent, ApprovalEvent.id == WebhookDelivery.event_id
            )
        )
        task_expired_id = None
        for delivery, event in rows.all():
            delivery.status = "exhausted"
            delivery.attempt = 6
            delivery.last_status_code = 500
            delivery.last_error = "connection refused"
            if event.event_type == "task_expired":
                task_expired_id = delivery.id
        await session.commit()
        assert task_expired_id, "expected a task_expired delivery to have been enqueued"
        return task_expired_id


@pytest.mark.asyncio
async def test_list_deliveries_filters_by_status_and_request(
    client, admin_token, service_token
) -> None:
    # Set up: active policy, request with callback_url, seeded exhausted delivery.
    await client.post(
        "/v1/awe/policies", json=_policy_with_sla(), headers=auth_header(admin_token)
    )
    await client.post(
        "/v1/awe/policies/registry.cr.admin/versions/1/activate",
        headers=auth_header(admin_token),
    )
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "registry.cr.admin",
            "artifact_type": "registry.change_request",
            "artifact_id": "cr-admin-1",
            "context": {},
            "callback_url": "https://registry.local/cb",
        },
        headers=auth_header(service_token),
    )
    request_id = resp.json()["request_id"]
    delivery_id = await _seed_exhausted_delivery(request_id)

    # Filter by status=exhausted → sees our exhausted deliveries.
    # The seed helper exhausts request_created, stage_started, and task_expired.
    resp = await client.get(
        "/v1/awe/admin/deliveries?status=exhausted",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    rows = resp.json()
    task_expired_rows = [r for r in rows if r["id"] == delivery_id]
    assert len(task_expired_rows) == 1
    row = task_expired_rows[0]
    assert row["status"] == "exhausted"
    assert row["request_id"] == request_id
    assert row["event_type"] == "task_expired"
    assert row["last_status_code"] == 500

    # Filter by request_id also works.
    resp = await client.get(
        f"/v1/awe/admin/deliveries?request_id={request_id}",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    assert len(resp.json()) >= 1

    # Filter by status=delivered returns empty.
    resp = await client.get(
        "/v1/awe/admin/deliveries?status=delivered",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_retry_delivery_resets_state(
    client, admin_token, service_token
) -> None:
    payload = _policy_with_sla()
    payload["policy_key"] = "registry.cr.retry"
    await client.post(
        "/v1/awe/policies", json=payload, headers=auth_header(admin_token)
    )
    await client.post(
        "/v1/awe/policies/registry.cr.retry/versions/1/activate",
        headers=auth_header(admin_token),
    )
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "registry.cr.retry",
            "artifact_type": "registry.change_request",
            "artifact_id": "cr-retry-1",
            "context": {},
            "callback_url": "https://registry.local/cb",
        },
        headers=auth_header(service_token),
    )
    delivery_id = await _seed_exhausted_delivery(resp.json()["request_id"])

    # Retry flips it back to pending + resets counters.
    resp = await client.post(
        f"/v1/awe/admin/deliveries/{delivery_id}/retry",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["attempt"] == 0
    assert body["last_error"] is None


@pytest.mark.asyncio
async def test_admin_endpoints_gated_on_awe_admin_role(
    client, user_token
) -> None:
    resp = await client.get(
        "/v1/awe/admin/deliveries", headers=auth_header(user_token)
    )
    assert resp.status_code == 403

    resp = await client.post(
        "/v1/awe/admin/deliveries/some-id/retry", headers=auth_header(user_token)
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_tasks_request_id_filter(
    client, admin_token, service_token
) -> None:
    payload = _policy_with_sla()
    payload["policy_key"] = "registry.cr.tasks-filter"
    await client.post(
        "/v1/awe/policies", json=payload, headers=auth_header(admin_token)
    )
    await client.post(
        "/v1/awe/policies/registry.cr.tasks-filter/versions/1/activate",
        headers=auth_header(admin_token),
    )
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "registry.cr.tasks-filter",
            "artifact_type": "registry.change_request",
            "artifact_id": "cr-tf-1",
            "context": {},
        },
        headers=auth_header(service_token),
    )
    request_id = resp.json()["request_id"]

    # request_id filter + assignee=* returns all tasks for that request.
    resp = await client.get(
        f"/v1/awe/tasks?assignee=*&request_id={request_id}",
        headers=auth_header(service_token),
    )
    assert resp.status_code == 200
    tasks = resp.json()["items"]
    assert len(tasks) == 1
    assert tasks[0]["request_id"] == request_id
