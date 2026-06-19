"""Extended request API coverage — idempotency, cancel, search, events."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

from .conftest import auth_header


def _policy(policy_key: str) -> dict:
    return {
        "policy_key": policy_key,
        "name": "Request extended",
        "artifact_type": "registry.change_request",
        "stages": [
            {
                "name": "Stage",
                "stage_order": 1,
                "mode": "all",
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}},
                ],
            },
        ],
    }


async def _activate(client, admin_token, policy_key: str) -> None:
    h = auth_header(admin_token)
    await client.post("/v1/awe/policies", json=_policy(policy_key), headers=h)
    await client.post(f"/v1/awe/policies/{policy_key}/versions/1/activate", headers=h)


@pytest.mark.asyncio
async def test_idempotency_replay_and_integrity_fallback(
    client, admin_token, service_token
) -> None:
    await _activate(client, admin_token, "ext.req.idem")
    h = {**auth_header(service_token), "Idempotency-Key": "ext-idem-key-1"}
    body = {
        "policy_key": "ext.req.idem",
        "artifact_type": "registry.change_request",
        "artifact_id": "idem-1",
        "context": {},
    }

    resp = await client.post("/v1/awe/requests", json=body, headers=h)
    assert resp.status_code == 201
    first_id = resp.json()["request_id"]

    resp = await client.post("/v1/awe/requests", json=body, headers=h)
    assert resp.status_code == 201
    assert resp.json()["request_id"] == first_id

    from awe.models import IdempotencyKey
    from sqlalchemy.ext.asyncio import AsyncSession

    real_flush = AsyncSession.flush

    async def flush_fail_on_idempotency_insert(self, *args, **kwargs):
        if any(isinstance(obj, IdempotencyKey) for obj in self.new):
            raise IntegrityError("INSERT", {}, Exception("dup"))
        return await real_flush(self, *args, **kwargs)

    h2 = {**auth_header(service_token), "Idempotency-Key": "ext-idem-key-2"}
    body2 = {**body, "artifact_id": "idem-2"}

    with patch.object(AsyncSession, "flush", flush_fail_on_idempotency_insert):
        resp = await client.post("/v1/awe/requests", json=body2, headers=h2)
    assert resp.status_code == 201
    assert resp.json()["request_id"]


@pytest.mark.asyncio
async def test_get_search_events_and_cancel_errors(
    client, admin_token, service_token
) -> None:
    await _activate(client, admin_token, "ext.req.lifecycle")
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "ext.req.lifecycle",
            "artifact_type": "registry.change_request",
            "artifact_id": "life-1",
            "context": {"k": "v"},
        },
        headers=auth_header(service_token),
    )
    request_id = resp.json()["request_id"]

    resp = await client.get(
        f"/v1/awe/requests/{request_id}", headers=auth_header(service_token)
    )
    assert resp.status_code == 200

    resp = await client.get(
        "/v1/awe/requests?artifact_type=registry.change_request&artifact_id=life-1&status=in_review&limit=10",
        headers=auth_header(service_token),
    )
    assert resp.status_code == 200
    assert any(r["id"] == request_id for r in resp.json())

    resp = await client.get(
        f"/v1/awe/requests/{request_id}/events",
        headers=auth_header(service_token),
    )
    assert resp.status_code == 200
    assert any(e["event_type"] == "request_created" for e in resp.json())

    resp = await client.get(
        "/v1/awe/requests/missing-id", headers=auth_header(service_token)
    )
    assert resp.status_code == 404
    assert resp.json()["errors"][0]["errorCode"] == "AWE-003"

    resp = await client.post(
        f"/v1/awe/requests/{request_id}/cancel",
        json={"reason": "withdrawn"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    resp = await client.post(
        f"/v1/awe/requests/{request_id}/cancel",
        json={"reason": "again"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 409
    assert resp.json()["errors"][0]["errorCode"] == "AWE-007"

    resp = await client.post(
        "/v1/awe/requests/missing-id/cancel",
        json={"reason": "nope"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404
