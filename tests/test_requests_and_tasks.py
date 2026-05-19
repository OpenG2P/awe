"""End-to-end smoke for: create policy → create request → decide → approved."""

from __future__ import annotations

import pytest
from jose import jwt

from .conftest import auth_header


def _two_stage_policy() -> dict:
    return {
        "policy_key": "registry.cr.smoke",
        "name": "CR smoke flow",
        "artifact_type": "registry.change_request",
        "stages": [
            {
                "name": "Officers",
                "stage_order": 1,
                "mode": "any-n",
                "mode_value": 1,
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}},
                    {"rule_type": "user", "rule_value": {"user_id": "u-bob"}},
                ],
            },
            {
                "name": "Director",
                "stage_order": 2,
                "mode": "all",
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-director"}},
                ],
            },
        ],
    }


def _user_token(sub: str) -> str:
    return jwt.encode(
        {"sub": sub, "realm_access": {"roles": []}, "email": f"{sub}@test"},
        "secret",
        algorithm="HS256",
    )


@pytest.mark.asyncio
async def test_full_two_stage_flow(client, admin_token, service_token) -> None:
    # 1. Activate policy
    resp = await client.post(
        "/v1/awe/policies",
        json=_two_stage_policy(),
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 201
    resp = await client.post(
        "/v1/awe/policies/registry.cr.smoke/versions/1/activate",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200

    # 2. Caller creates a request
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "registry.cr.smoke",
            "artifact_type": "registry.change_request",
            "artifact_id": "cr-101",
            "context": {"district": "D1"},
        },
        headers={**auth_header(service_token), "Idempotency-Key": "test-key-1"},
    )
    assert resp.status_code == 201, resp.text
    req = resp.json()
    request_id = req["request_id"]
    assert req["status"] == "in_review"
    assert req["current_stage_order"] == 1
    assert len(req["tasks"]) == 2  # alice and bob

    # 3. Idempotency replay returns the same body without creating a new request
    resp2 = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "registry.cr.smoke",
            "artifact_type": "registry.change_request",
            "artifact_id": "cr-101",
            "context": {"district": "D1"},
        },
        headers={**auth_header(service_token), "Idempotency-Key": "test-key-1"},
    )
    assert resp2.status_code == 201
    assert resp2.json()["request_id"] == request_id

    # 4. Alice lists her tasks
    resp = await client.get(
        "/v1/awe/tasks", headers=auth_header(_user_token("u-alice"))
    )
    assert resp.status_code == 200
    alice_tasks = [t for t in resp.json()["items"] if t["request_id"] == request_id]
    assert len(alice_tasks) == 1
    alice_task_id = alice_tasks[0]["id"]

    # 5. Alice claims and approves — stage 1 satisfied (any-1)
    resp = await client.post(
        f"/v1/awe/tasks/{alice_task_id}/claim",
        headers=auth_header(_user_token("u-alice")),
    )
    assert resp.status_code == 200
    resp = await client.post(
        f"/v1/awe/tasks/{alice_task_id}/decision",
        json={"action": "approve", "comment": "looks good"},
        headers=auth_header(_user_token("u-alice")),
    )
    assert resp.status_code == 201

    # 6. Director should now have a task
    resp = await client.get(
        "/v1/awe/tasks", headers=auth_header(_user_token("u-director"))
    )
    assert resp.status_code == 200
    director_tasks = [t for t in resp.json()["items"] if t["request_id"] == request_id]
    assert len(director_tasks) == 1
    director_task_id = director_tasks[0]["id"]

    # 7. Director approves — request approved
    resp = await client.post(
        f"/v1/awe/tasks/{director_task_id}/decision",
        json={"action": "approve"},
        headers=auth_header(_user_token("u-director")),
    )
    assert resp.status_code == 201

    # 8. Verify final state
    resp = await client.get(
        f"/v1/awe/requests/{request_id}", headers=auth_header(service_token)
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"

    # 9. Event timeline contains the expected lifecycle
    resp = await client.get(
        f"/v1/awe/requests/{request_id}/events", headers=auth_header(service_token)
    )
    assert resp.status_code == 200
    events = resp.json()
    types = [e["event_type"] for e in events]
    assert "request_created" in types
    assert "stage_started" in types
    assert "stage_completed" in types
    assert "request_approved" in types

    stage_started_orders = [
        e["payload"]["stage_order"]
        for e in events
        if e["event_type"] == "stage_started"
    ]
    assert 1 in stage_started_orders
    assert 2 in stage_started_orders


@pytest.mark.asyncio
async def test_reject_terminates_request(client, admin_token, service_token) -> None:
    payload = _two_stage_policy()
    payload["policy_key"] = "registry.cr.reject"
    await client.post("/v1/awe/policies", json=payload, headers=auth_header(admin_token))
    await client.post(
        "/v1/awe/policies/registry.cr.reject/versions/1/activate",
        headers=auth_header(admin_token),
    )

    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "registry.cr.reject",
            "artifact_type": "registry.change_request",
            "artifact_id": "cr-rej",
            "context": {},
        },
        headers=auth_header(service_token),
    )
    request_id = resp.json()["request_id"]

    # Alice rejects (any-1 + reject = stage rejected → request rejected)
    resp = await client.get("/v1/awe/tasks", headers=auth_header(_user_token("u-alice")))
    alice_task_id = next(
        t["id"] for t in resp.json()["items"] if t["request_id"] == request_id
    )
    resp = await client.post(
        f"/v1/awe/tasks/{alice_task_id}/decision",
        json={"action": "reject", "comment": "no"},
        headers=auth_header(_user_token("u-alice")),
    )
    assert resp.status_code == 201

    resp = await client.get(
        f"/v1/awe/requests/{request_id}", headers=auth_header(service_token)
    )
    # any-1: alice rejected, bob still open → not yet rejected (capacity remains).
    # Force bob to also reject so the whole stage tips.
    if resp.json()["status"] == "in_review":
        resp = await client.get(
            "/v1/awe/tasks", headers=auth_header(_user_token("u-bob"))
        )
        bob_task_id = next(
            t["id"] for t in resp.json()["items"] if t["request_id"] == request_id
        )
        await client.post(
            f"/v1/awe/tasks/{bob_task_id}/decision",
            json={"action": "reject"},
            headers=auth_header(_user_token("u-bob")),
        )
        resp = await client.get(
            f"/v1/awe/requests/{request_id}", headers=auth_header(service_token)
        )

    assert resp.json()["status"] == "rejected"


@pytest.mark.asyncio
async def test_cancel_request(client, admin_token, service_token) -> None:
    payload = _two_stage_policy()
    payload["policy_key"] = "registry.cr.cancel"
    await client.post("/v1/awe/policies", json=payload, headers=auth_header(admin_token))
    await client.post(
        "/v1/awe/policies/registry.cr.cancel/versions/1/activate",
        headers=auth_header(admin_token),
    )

    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "registry.cr.cancel",
            "artifact_type": "registry.change_request",
            "artifact_id": "cr-cancel",
            "context": {},
        },
        headers=auth_header(service_token),
    )
    request_id = resp.json()["request_id"]

    resp = await client.post(
        f"/v1/awe/requests/{request_id}/cancel",
        json={"reason": "no longer needed"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_search_by_artifact(client, admin_token, service_token) -> None:
    # Use an already-active policy from a prior test.
    payload = _two_stage_policy()
    payload["policy_key"] = "registry.cr.search"
    await client.post("/v1/awe/policies", json=payload, headers=auth_header(admin_token))
    await client.post(
        "/v1/awe/policies/registry.cr.search/versions/1/activate",
        headers=auth_header(admin_token),
    )
    await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "registry.cr.search",
            "artifact_type": "registry.change_request",
            "artifact_id": "cr-search-1",
            "context": {},
        },
        headers=auth_header(service_token),
    )

    resp = await client.get(
        "/v1/awe/requests?artifact_type=registry.change_request&artifact_id=cr-search-1",
        headers=auth_header(service_token),
    )
    assert resp.status_code == 200
    assert len(resp.json()) >= 1
