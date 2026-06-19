"""Extended policy API coverage — conflicts, versions, simulate paths."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from awe.services import resolver as resolver_svc

from .conftest import auth_header


def _sample_policy(*, policy_key: str = "ext.policy.v1") -> dict:
    return {
        "policy_key": policy_key,
        "name": "Extended policy",
        "artifact_type": "registry.change_request",
        "stages": [
            {
                "name": "Officers",
                "stage_order": 1,
                "mode": "any-n",
                "mode_value": 1,
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-officer-A"}},
                ],
            },
        ],
    }


def _skip_policy(*, policy_key: str = "ext.policy.skip") -> dict:
    return {
        "policy_key": policy_key,
        "name": "Skip-if policy",
        "artifact_type": "test",
        "stages": [
            {
                "name": "Optional",
                "stage_order": 1,
                "mode": "all",
                "skip_if": {"==": [{"var": "skip_stage"}, True]},
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-never"}},
                ],
            },
            {
                "name": "Required",
                "stage_order": 2,
                "mode": "all",
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-required"}},
                ],
            },
        ],
    }


@pytest.mark.asyncio
async def test_create_policy_conflict(client, admin_token) -> None:
    h = auth_header(admin_token)
    payload = _sample_policy(policy_key="ext.policy.conflict")
    resp = await client.post("/v1/awe/policies", json=payload, headers=h)
    assert resp.status_code == 201

    resp = await client.post("/v1/awe/policies", json=payload, headers=h)
    assert resp.status_code == 409
    assert resp.json()["errors"][0]["errorCode"] == "AWE-002"


@pytest.mark.asyncio
async def test_list_policies_viewer_and_admin(client, admin_token, viewer_token) -> None:
    h_admin = auth_header(admin_token)
    payload = _sample_policy(policy_key="ext.policy.list")
    await client.post("/v1/awe/policies", json=payload, headers=h_admin)

    for token in (admin_token, viewer_token):
        resp = await client.get("/v1/awe/policies", headers=auth_header(token))
        assert resp.status_code == 200
        assert any(p["policy_key"] == "ext.policy.list" for p in resp.json())


@pytest.mark.asyncio
async def test_versions_and_get_version_404(client, admin_token) -> None:
    h = auth_header(admin_token)
    resp = await client.get("/v1/awe/policies/missing.key/versions", headers=h)
    assert resp.status_code == 404
    assert resp.json()["errors"][0]["errorCode"] == "AWE-001"

    resp = await client.get("/v1/awe/policies/missing.key/versions/99", headers=h)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_add_version_and_mismatch(client, admin_token) -> None:
    h = auth_header(admin_token)
    key = "ext.policy.addver"
    await client.post("/v1/awe/policies", json=_sample_policy(policy_key=key), headers=h)

    v2 = _sample_policy(policy_key=key)
    v2["name"] = "Version two"
    resp = await client.put(f"/v1/awe/policies/{key}", json=v2, headers=h)
    assert resp.status_code == 201
    assert resp.json()["version"] == 2

    resp = await client.get(f"/v1/awe/policies/{key}/versions", headers=h)
    assert len(resp.json()) == 2

    bad = {**v2, "policy_key": "other.key"}
    resp = await client.put(f"/v1/awe/policies/{key}", json=bad, headers=h)
    assert resp.status_code == 400
    assert resp.json()["errors"][0]["errorCode"] == "AWE-010"

    resp = await client.put(
        "/v1/awe/policies/no.such.policy",
        json=_sample_policy(policy_key="no.such.policy"),
        headers=h,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_simulate_skip_and_resolver_error(client, admin_token) -> None:
    h = auth_header(admin_token)
    key = "ext.policy.simulate"
    await client.post("/v1/awe/policies", json=_skip_policy(policy_key=key), headers=h)

    resp = await client.post(
        f"/v1/awe/policies/{key}/versions/1/simulate",
        json={"context": {"skip_stage": True}},
        headers=h,
    )
    assert resp.status_code == 200
    stages = resp.json()["stages"]
    assert stages[0]["skipped"] is True
    assert stages[0]["skip_reason"] == "skip_if"
    assert stages[1]["skipped"] is False
    assert stages[1]["resolved_approvers"] == ["u-required"]

    with patch(
        "awe.controllers.policy.resolver_svc.resolve_stage",
        side_effect=resolver_svc.ResolutionError("boom"),
    ):
        resp = await client.post(
            f"/v1/awe/policies/{key}/versions/1/simulate",
            json={"context": {"skip_stage": False}},
            headers=h,
        )
    assert resp.status_code == 503
    assert resp.json()["errors"][0]["errorCode"] == "AWE-007"
    assert "Resolver failed" in resp.json()["errors"][0]["message"]


@pytest.mark.asyncio
async def test_simulate_not_found(client, admin_token) -> None:
    h = auth_header(admin_token)
    resp = await client.post(
        "/v1/awe/policies/ghost/versions/1/simulate",
        json={"context": {}},
        headers=h,
    )
    assert resp.status_code == 404
