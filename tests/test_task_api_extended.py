"""Extended task API coverage — stats, filters, claim/decide errors."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from jose import jwt

from .conftest import auth_header


def _policy(*, policy_key: str, artifact_type: str = "registry.change_request") -> dict:
    return {
        "policy_key": policy_key,
        "name": "Task extended",
        "artifact_type": artifact_type,
        "stages": [
            {
                "name": "Stage 1",
                "stage_order": 1,
                "mode": "any-n",
                "mode_value": 1,
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}},
                    {"rule_type": "user", "rule_value": {"user_id": "u-bob"}},
                ],
            },
        ],
    }


def _user_token(sub: str) -> str:
    return jwt.encode(
        {
            "sub": sub,
            "preferred_username": sub,
            "realm_access": {"roles": []},
            "email": f"{sub}@test",
        },
        "secret",
        algorithm="HS256",
    )


async def _setup_request(client, admin_token, service_token, *, policy_key: str, artifact_type: str, artifact_id: str, context: dict | None = None) -> tuple[str, str]:
    h_admin = auth_header(admin_token)
    await client.post("/v1/awe/policies", json=_policy(policy_key=policy_key, artifact_type=artifact_type), headers=h_admin)
    await client.post(f"/v1/awe/policies/{policy_key}/versions/1/activate", headers=h_admin)
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": policy_key,
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
            "context": context or {},
        },
        headers=auth_header(service_token),
    )
    assert resp.status_code == 201, resp.text
    request_id = resp.json()["request_id"]
    tasks_resp = await client.get(
        f"/v1/awe/tasks?assignee=*&request_id={request_id}",
        headers=auth_header(service_token),
    )
    alice_task = next(t for t in tasks_resp.json()["items"] if t["assignee"] == "u-alice")
    return request_id, alice_task["id"]


@pytest.mark.asyncio
async def test_task_stats_and_filters(client, admin_token, service_token) -> None:
    await _setup_request(
        client,
        admin_token,
        service_token,
        policy_key="ext.task.cr",
        artifact_type="registry.change_request",
        artifact_id="cr-1",
    )
    await _setup_request(
        client,
        admin_token,
        service_token,
        policy_key="ext.task.intake",
        artifact_type="registry.intake_form",
        artifact_id="if-1",
    )
    await _setup_request(
        client,
        admin_token,
        service_token,
        policy_key="ext.task.other",
        artifact_type="custom.other",
        artifact_id="oth-1",
    )

    resp = await client.get("/v1/awe/tasks/stats", headers=auth_header(_user_token("u-alice")))
    assert resp.status_code == 200
    stats = resp.json()
    assert stats["total"] >= 3
    assert stats["change_request_count"] >= 1
    assert stats["intake_form_count"] >= 1

    resp = await client.get(
        "/v1/awe/tasks/stats?status=open",
        headers=auth_header(_user_token("u-alice")),
    )
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1

    resp = await client.get(
        "/v1/awe/tasks?artifact_type=registry.change_request&search_text=cr-1",
        headers=auth_header(_user_token("u-alice")),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert body["items"]

    resp = await client.get(
        "/v1/awe/tasks?policy_key=ext.task.cr&status=open&page=1&page_size=5",
        headers=auth_header(_user_token("u-alice")),
    )
    assert resp.status_code == 200
    assert resp.json()["page_size"] == 5


@pytest.mark.asyncio
async def test_task_stats_missing_assignee(client) -> None:
    from jose import jwt

    whitespace_sub = jwt.encode(
        {"sub": "   ", "realm_access": {"roles": []}, "email": "x@test"},
        "secret",
        algorithm="HS256",
    )
    resp = await client.get("/v1/awe/tasks/stats", headers=auth_header(whitespace_sub))
    assert resp.status_code == 401
    assert resp.json()["errors"][0]["errorCode"] == "AWE-001"


@pytest.mark.asyncio
async def test_claim_and_decide_errors(client, admin_token, service_token) -> None:
    request_id, alice_task_id = await _setup_request(
        client,
        admin_token,
        service_token,
        policy_key="ext.task.errors",
        artifact_type="registry.change_request",
        artifact_id="err-1",
        context={"note": "searchable"},
    )

    resp = await client.post(
        "/v1/awe/tasks/nonexistent/claim",
        headers=auth_header(_user_token("u-alice")),
    )
    assert resp.status_code == 404
    assert resp.json()["errors"][0]["errorCode"] == "AWE-004"

    resp = await client.post(
        f"/v1/awe/tasks/{alice_task_id}/claim",
        headers=auth_header(_user_token("u-bob")),
    )
    assert resp.status_code == 403
    assert resp.json()["errors"][0]["errorCode"] == "AWE-008"

    tasks_resp = await client.get(
        f"/v1/awe/tasks?assignee=*&request_id={request_id}",
        headers=auth_header(service_token),
    )
    bob_task_id = next(t["id"] for t in tasks_resp.json()["items"] if t["assignee"] == "u-bob")

    from sqlalchemy.ext.asyncio import AsyncSession
    from awe.models import ApprovalRequest

    real_get = AsyncSession.get

    async def hide_request(self, entity, ident, **kwargs):
        row = await real_get(self, entity, ident, **kwargs)
        if entity is ApprovalRequest and ident == request_id:
            return None
        return row

    with patch.object(AsyncSession, "get", hide_request):
        resp = await client.post(
            f"/v1/awe/tasks/{bob_task_id}/decision",
            json={"action": "approve"},
            headers=auth_header(_user_token("u-bob")),
        )
    assert resp.status_code == 404
    assert resp.json()["errors"][0]["errorCode"] == "AWE-003"

    resp = await client.post(
        f"/v1/awe/tasks/{alice_task_id}/claim",
        headers=auth_header(_user_token("u-alice")),
    )
    assert resp.status_code == 200

    resp = await client.post(
        f"/v1/awe/tasks/{alice_task_id}/decision",
        json={"action": "approve"},
        headers=auth_header(_user_token("u-alice")),
    )
    assert resp.status_code == 201

    resp = await client.post(
        f"/v1/awe/tasks/{alice_task_id}/claim",
        headers=auth_header(_user_token("u-alice")),
    )
    assert resp.status_code == 409
    assert resp.json()["errors"][0]["errorCode"] == "AWE-007"

    resp = await client.post(
        f"/v1/awe/tasks/{alice_task_id}/decision",
        json={"action": "approve"},
        headers=auth_header(_user_token("u-alice")),
    )
    assert resp.status_code == 409

    resp = await client.get(
        f"/v1/awe/tasks?assignee=*&request_id={request_id}&search_text=searchable",
        headers=auth_header(service_token),
    )
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1
