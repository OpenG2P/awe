"""Extended admin endpoint tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from awe.services.keycloak_admin import KeycloakAdminError

from .conftest import auth_header


@pytest.mark.asyncio
async def test_list_deliveries_unfiltered(client, admin_token) -> None:
    resp = await client.get(
        "/v1/awe/admin/deliveries",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_retry_delivery_not_found(client, admin_token) -> None:
    resp = await client.post(
        "/v1/awe/admin/deliveries/missing/retry",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_retry_delivery_already_delivered(client, admin_token, service_token) -> None:
    policy = {
        "policy_key": "admin.retry.delivered",
        "name": "P",
        "artifact_type": "test",
        "stages": [
            {
                "name": "S",
                "stage_order": 1,
                "mode": "all",
                "rules": [{"rule_type": "user", "rule_value": {"user_id": "u-alice"}}],
            }
        ],
    }
    await client.post("/v1/awe/policies", json=policy, headers=auth_header(admin_token))
    await client.post(
        "/v1/awe/policies/admin.retry.delivered/versions/1/activate",
        headers=auth_header(admin_token),
    )
    await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "admin.retry.delivered",
            "artifact_type": "test",
            "artifact_id": "a1",
            "callback_url": "https://example/cb",
        },
        headers=auth_header(service_token),
    )
    listed = await client.get(
        "/v1/awe/admin/deliveries",
        headers=auth_header(admin_token),
    )
    if not listed.json():
        pytest.skip("No deliveries seeded")
    delivery_id = listed.json()[0]["id"]
    from awe.db import get_engine
    from awe.models import WebhookDelivery
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with sm() as session:
        d = await session.get(WebhookDelivery, delivery_id)
        d.status = "delivered"
        await session.commit()
    resp = await client.post(
        f"/v1/awe/admin/deliveries/{delivery_id}/retry",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_audit_filters(client, admin_token) -> None:
    policy = {
        "policy_key": "admin.audit.filters",
        "name": "P",
        "artifact_type": "test",
        "stages": [
            {
                "name": "S",
                "stage_order": 1,
                "mode": "all",
                "rules": [{"rule_type": "user", "rule_value": {"user_id": "u-alice"}}],
            }
        ],
    }
    await client.post("/v1/awe/policies", json=policy, headers=auth_header(admin_token))
    qs = (
        "?actor=test-admin&action=policy.create&resource_type=policy"
        "&resource_id=admin.audit.filters:1&limit=10"
    )
    resp = await client.get(
        f"/v1/awe/admin/audit{qs}",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_keycloak_endpoints_keycloak_errors(client, admin_token) -> None:
    for path in ("/clients", "/roles", "/groups"):
        with patch(
            "awe.controllers.admin.keycloak_admin_svc.list_clients",
            new=AsyncMock(side_effect=KeycloakAdminError("down")),
        ), patch(
            "awe.controllers.admin.keycloak_admin_svc.list_roles",
            new=AsyncMock(side_effect=KeycloakAdminError("down")),
        ), patch(
            "awe.controllers.admin.keycloak_admin_svc.list_groups",
            new=AsyncMock(side_effect=KeycloakAdminError("down")),
        ):
            resp = await client.get(
                f"/v1/awe/admin/keycloak{path}",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 503
