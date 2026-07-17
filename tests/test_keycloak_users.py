"""Keycloak user lookup admin endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from .conftest import auth_header


@pytest.mark.asyncio
async def test_list_keycloak_users_requires_auth(client) -> None:
    resp = await client.get("/v1/awe/admin/keycloak/users")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_keycloak_users_not_configured(client, viewer_token) -> None:
    from awe.services.keycloak_admin import KeycloakAdminError

    with patch(
        "awe.controllers.admin.keycloak_admin_svc.list_users",
        new=AsyncMock(side_effect=KeycloakAdminError("Keycloak base_url not configured")),
    ):
        resp = await client.get(
            "/v1/awe/admin/keycloak/users",
            headers=auth_header(viewer_token),
        )
    assert resp.status_code == 503
    assert resp.json()["errors"][0]["errorCode"] == "AWE-011"


@pytest.mark.asyncio
async def test_list_keycloak_users_success(client, admin_token) -> None:
    mock_users = [
        {
            "user_id": "alex.carter",
            "username": "alex.carter",
            "email": "alex@example.org",
            "name": "Alex Carter",
        },
        {
            "user_id": "jane.smith",
            "username": "jane.smith",
            "email": "jane@example.org",
            "name": None,
        },
    ]

    with patch(
        "awe.controllers.admin.keycloak_admin_svc.list_users",
        new=AsyncMock(return_value=mock_users),
    ) as list_users:
        resp = await client.get(
            "/v1/awe/admin/keycloak/users?q=alex",
            headers=auth_header(admin_token),
        )

    list_users.assert_awaited_once_with("alex", 100)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["user_id"] == "alex.carter"
    assert body[0]["name"] == "Alex Carter"
    assert body[1]["user_id"] == "jane.smith"


@pytest.mark.asyncio
async def test_list_keycloak_roles_success(client, admin_token) -> None:
    mock_roles = [
        {"name": "AWE_ADMIN", "client": None, "description": "Admin"},
        {"name": "PROGRAM_MANAGER", "client": "registry-staff-portal", "description": None},
    ]

    with patch(
        "awe.controllers.admin.keycloak_admin_svc.list_roles",
        new=AsyncMock(return_value=mock_roles),
    ) as list_roles:
        resp = await client.get(
            "/v1/awe/admin/keycloak/roles?client=registry-staff-portal&q=prog",
            headers=auth_header(admin_token),
        )

    list_roles.assert_awaited_once_with("registry-staff-portal", "prog")
    assert resp.status_code == 200
    assert resp.json()[0]["name"] == "AWE_ADMIN"


@pytest.mark.asyncio
async def test_list_keycloak_groups_success(client, admin_token) -> None:
    mock_groups = [
        {"path": "/states/d1/officers", "name": "officers"},
        {"path": "/states/d2/officers", "name": "officers"},
    ]

    with patch(
        "awe.controllers.admin.keycloak_admin_svc.list_groups",
        new=AsyncMock(return_value=mock_groups),
    ) as list_groups:
        resp = await client.get(
            "/v1/awe/admin/keycloak/groups?q=officers",
            headers=auth_header(admin_token),
        )

    list_groups.assert_awaited_once_with("officers", 100)
    assert resp.status_code == 200
    assert len(resp.json()) == 2


@pytest.mark.asyncio
async def test_list_keycloak_clients_success(client, admin_token) -> None:
    mock_clients = [
        {"client_id": "awe-admin-portal", "name": "AWE Admin Portal"},
        {"client_id": "registry-staff-portal", "name": "Registry Staff Portal"},
    ]

    with patch(
        "awe.controllers.admin.keycloak_admin_svc.list_clients",
        new=AsyncMock(return_value=mock_clients),
    ) as list_clients:
        resp = await client.get(
            "/v1/awe/admin/keycloak/clients",
            headers=auth_header(admin_token),
        )

    list_clients.assert_awaited_once_with(200)
    assert resp.status_code == 200
    assert resp.json()[1]["client_id"] == "registry-staff-portal"
