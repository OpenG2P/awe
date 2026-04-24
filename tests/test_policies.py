"""Policy CRUD + simulate smoke tests."""

from __future__ import annotations

import pytest

from .conftest import auth_header


def _sample_policy() -> dict:
    return {
        "policy_key": "registry.cr.v1",
        "name": "Registry CR approval",
        "artifact_type": "registry.change_request",
        "stages": [
            {
                "name": "District officers",
                "stage_order": 1,
                "mode": "any-n",
                "mode_value": 1,
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-officer-A"}},
                    {"rule_type": "user", "rule_value": {"user_id": "u-officer-B"}},
                ],
            },
            {
                "name": "State directors",
                "stage_order": 2,
                "mode": "all",
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-director-X"}},
                    {"rule_type": "user", "rule_value": {"user_id": "u-director-Y"}},
                ],
            },
        ],
    }


@pytest.mark.asyncio
async def test_create_list_activate_simulate(client, admin_token) -> None:
    h = auth_header(admin_token)

    # Create draft v1
    resp = await client.post("/v1/awe/policies", json=_sample_policy(), headers=h)
    assert resp.status_code == 201, resp.text
    p = resp.json()
    assert p["policy_key"] == "registry.cr.v1"
    assert p["version"] == 1
    assert p["status"] == "draft"
    assert len(p["stages"]) == 2

    # List policies — should include our new one
    resp = await client.get("/v1/awe/policies", headers=h)
    assert resp.status_code == 200
    assert any(item["policy_key"] == "registry.cr.v1" for item in resp.json())

    # List versions
    resp = await client.get("/v1/awe/policies/registry.cr.v1/versions", headers=h)
    assert resp.status_code == 200
    versions = resp.json()
    assert len(versions) == 1 and versions[0]["status"] == "draft"

    # Activate v1
    resp = await client.post(
        "/v1/awe/policies/registry.cr.v1/versions/1/activate", headers=h
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"

    # Simulate
    resp = await client.post(
        "/v1/awe/policies/registry.cr.v1/versions/1/simulate",
        json={"context": {"district": "D1"}},
        headers=h,
    )
    assert resp.status_code == 200
    sim = resp.json()
    assert sim["policy_version"] == 1
    assert len(sim["stages"]) == 2
    assert sim["stages"][0]["resolved_approvers"] == ["u-officer-A", "u-officer-B"]


@pytest.mark.asyncio
async def test_unauthenticated_blocked(client) -> None:
    resp = await client.post("/v1/awe/policies", json=_sample_policy())
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_non_admin_blocked(client, user_token) -> None:
    resp = await client.post(
        "/v1/awe/policies", json=_sample_policy(), headers=auth_header(user_token)
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_unknown_version_404(client, admin_token) -> None:
    resp = await client.get(
        "/v1/awe/policies/no.such.key/versions", headers=auth_header(admin_token)
    )
    assert resp.status_code == 404
    assert resp.json()["errors"][0]["errorCode"] == "AWE-001"


@pytest.mark.asyncio
async def test_edit_draft_in_place(client, admin_token) -> None:
    h = auth_header(admin_token)
    payload = _sample_policy()
    payload["policy_key"] = "registry.cr.edit"
    resp = await client.post("/v1/awe/policies", json=payload, headers=h)
    assert resp.status_code == 201

    # Edit the draft: swap approver in stage 1, rename stage 2.
    updated = {**payload, "name": "CR approval (edited)"}
    updated["stages"][0]["rules"] = [
        {"rule_type": "user", "rule_value": {"user_id": "u-new-officer"}}
    ]
    updated["stages"][1]["name"] = "Renamed directors"

    resp = await client.patch(
        "/v1/awe/policies/registry.cr.edit/versions/1", json=updated, headers=h
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "CR approval (edited)"
    assert body["stages"][0]["rules"][0]["rule_value"]["user_id"] == "u-new-officer"
    assert body["stages"][1]["name"] == "Renamed directors"


@pytest.mark.asyncio
async def test_deactivate_flips_active_to_archived(
    client, admin_token, service_token
) -> None:
    h = auth_header(admin_token)
    payload = _sample_policy()
    payload["policy_key"] = "registry.cr.deact"
    await client.post("/v1/awe/policies", json=payload, headers=h)
    await client.post(
        "/v1/awe/policies/registry.cr.deact/versions/1/activate", headers=h
    )

    # Deactivate → archived
    resp = await client.post(
        "/v1/awe/policies/registry.cr.deact/versions/1/deactivate", headers=h
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "archived"

    # Deactivating twice returns 409 (not active any more)
    resp = await client.post(
        "/v1/awe/policies/registry.cr.deact/versions/1/deactivate", headers=h
    )
    assert resp.status_code == 409
    assert resp.json()["errors"][0]["errorCode"] == "AWE-007"

    # New POST /requests now fails — no active version for this key
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "registry.cr.deact",
            "artifact_type": "x",
            "artifact_id": "y",
            "context": {},
        },
        headers=auth_header(service_token),
    )
    assert resp.status_code == 404
    assert resp.json()["errors"][0]["errorCode"] == "AWE-001"


@pytest.mark.asyncio
async def test_edit_active_version_rejected(client, admin_token) -> None:
    h = auth_header(admin_token)
    payload = _sample_policy()
    payload["policy_key"] = "registry.cr.locked"
    await client.post("/v1/awe/policies", json=payload, headers=h)
    await client.post(
        "/v1/awe/policies/registry.cr.locked/versions/1/activate", headers=h
    )

    resp = await client.patch(
        "/v1/awe/policies/registry.cr.locked/versions/1", json=payload, headers=h
    )
    assert resp.status_code == 409
    assert resp.json()["errors"][0]["errorCode"] == "AWE-007"
