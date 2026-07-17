"""Unit tests for awe.services.keycloak_admin."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from awe.services import keycloak_admin as kc


@pytest.mark.asyncio
async def test_keycloak_admin_token_not_configured():
    with patch("awe.services.keycloak_admin.get_settings") as gs:
        gs.return_value.awe.keycloak.base_url = ""
        with pytest.raises(kc.KeycloakAdminError, match="base_url"):
            await kc.keycloak_admin_token()


@pytest.mark.asyncio
async def test_keycloak_admin_token_success():
    with patch("awe.services.keycloak_admin.get_settings") as gs:
        gs.return_value.awe.keycloak.base_url = "https://kc"
        gs.return_value.awe.keycloak.realm = "staff"
        gs.return_value.awe.keycloak.admin_client_id = "client"
        gs.return_value.awe.keycloak.admin_client_secret = "secret"
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"access_token": "tok"})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=mock_client):
            token = await kc.keycloak_admin_token()
    assert token == "tok"


def test_format_user_skips_invalid():
    assert kc._format_user({}) is None
    assert kc._format_user({"username": "a", "firstName": "A", "lastName": "B"})["name"] == "A B"


def test_admin_base_url():
    with patch("awe.services.keycloak_admin.get_settings") as gs:
        gs.return_value.awe.keycloak.base_url = "https://kc/"
        gs.return_value.awe.keycloak.realm = "staff"
        assert kc._admin_base_url() == "https://kc/admin/realms/staff"


@pytest.mark.asyncio
async def test_admin_get_http_error():
    with patch(
        "awe.services.keycloak_admin.keycloak_admin_token",
        new=AsyncMock(return_value="tok"),
    ), patch("awe.services.keycloak_admin.get_settings") as gs:
        gs.return_value.awe.keycloak.base_url = "https://kc"
        gs.return_value.awe.keycloak.realm = "staff"
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.HTTPError("fail"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(kc.KeycloakAdminError):
                await kc._admin_get("/users")


@pytest.mark.asyncio
async def test_list_users_clients_roles_groups():
    async def fake_get(path, params=None):
        if path == "/users":
            return [{"username": "alice", "email": "a@x.org"}]
        if path == "/clients":
            if params and params.get("clientId"):
                return [{"id": "uuid-1"}]
            return [{"clientId": "portal", "name": "Portal"}]
        if path.endswith("/roles"):
            return [{"name": "ADMIN", "description": "Admin role"}]
        if path == "/groups":
            return [{"path": "/g1", "name": "g1", "subGroups": [{"path": "/g1/c", "name": "c"}]}]
        raise AssertionError(path)

    with patch("awe.services.keycloak_admin._admin_get", side_effect=fake_get):
        out_users = await kc.list_users("alice", 10)
        out_clients = await kc.list_clients()
        out_roles = await kc.list_roles(None, "adm")
        out_groups = await kc.list_groups("g1", 50)
        out_client_roles = await kc.list_roles("portal")

    assert out_users[0]["user_id"] == "alice"
    assert out_clients[0]["client_id"] == "portal"
    assert out_roles[0]["name"] == "ADMIN"
    assert any(g["path"] == "/g1/c" for g in out_groups)
    assert out_client_roles[0]["client"] == "portal"


@pytest.mark.asyncio
async def test_client_uuid_not_found():
    with patch("awe.services.keycloak_admin._admin_get", new=AsyncMock(return_value=[])):
        with pytest.raises(kc.KeycloakAdminError, match="client not found"):
            await kc.list_roles("missing-client")
