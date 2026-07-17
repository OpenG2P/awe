"""Keycloak Admin REST helpers shared by approver resolution and the admin UI."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from ..config import get_settings
from .assignee_id import assignee_id_from_keycloak_user

logger = logging.getLogger(__name__)


class KeycloakAdminError(Exception):
    """Raised when a Keycloak admin API call fails."""


async def keycloak_admin_token() -> str:
    cfg = get_settings().awe.keycloak
    if not cfg.base_url:
        raise KeycloakAdminError("Keycloak base_url not configured")
    token_url = (
        f"{cfg.base_url.rstrip('/')}/realms/{cfg.realm}/protocol/openid-connect/token"
    )
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": cfg.admin_client_id,
                "client_secret": cfg.admin_client_secret,
            },
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


def _admin_base_url() -> str:
    cfg = get_settings().awe.keycloak
    return f"{cfg.base_url.rstrip('/')}/admin/realms/{cfg.realm}"


async def _admin_get(path: str, params: dict[str, Any] | None = None) -> Any:
    try:
        token = await keycloak_admin_token()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{_admin_base_url()}{path}",
                headers={"Authorization": f"Bearer {token}"},
                params=params or {},
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as e:
        logger.warning("Keycloak admin GET %s failed: %s", path, e)
        raise KeycloakAdminError(f"Keycloak admin request failed: {e}") from e


def _format_user(user: dict) -> Optional[Dict[str, Any]]:
    user_id = assignee_id_from_keycloak_user(user)
    if not user_id:
        return None
    first = (user.get("firstName") or "").strip()
    last = (user.get("lastName") or "").strip()
    name = f"{first} {last}".strip() or None
    return {
        "user_id": user_id,
        "username": user.get("username") or user_id,
        "email": user.get("email") or None,
        "name": name,
    }


async def list_users(q: str | None = None, limit: int = 100) -> List[Dict[str, Any]]:
    """Search Keycloak users for the admin UI user picker."""
    params: dict[str, Any] = {"max": limit}
    if q and q.strip():
        params["search"] = q.strip()
    raw = await _admin_get("/users", params)
    out: List[Dict[str, Any]] = []
    for item in raw:
        formatted = _format_user(item)
        if formatted is not None:
            out.append(formatted)
    return out


async def list_clients(limit: int = 200) -> List[Dict[str, Any]]:
    """List Keycloak clients (by clientId) for client-role pickers."""
    raw = await _admin_get("/clients", {"max": limit})
    out: List[Dict[str, Any]] = []
    for item in raw:
        client_id = item.get("clientId") or ""
        if not client_id:
            continue
        out.append(
            {
                "client_id": client_id,
                "name": item.get("name") or client_id,
            }
        )
    return sorted(out, key=lambda x: x["client_id"].lower())


async def _client_uuid(client_id: str) -> str:
    found = await _admin_get("/clients", {"clientId": client_id})
    if not found:
        raise KeycloakAdminError(f"Keycloak client not found: {client_id}")
    return found[0]["id"]


async def list_roles(
    client_id: str | None = None,
    q: str | None = None,
) -> List[Dict[str, Any]]:
    """List realm roles or client roles for the admin UI role picker."""
    if client_id and client_id.strip():
        uuid = await _client_uuid(client_id.strip())
        raw = await _admin_get(f"/clients/{uuid}/roles")
        scope_client = client_id.strip()
    else:
        raw = await _admin_get("/roles")
        scope_client = None

    needle = q.strip().lower() if q and q.strip() else None
    out: List[Dict[str, Any]] = []
    for item in raw:
        name = item.get("name") or ""
        if not name:
            continue
        if needle and needle not in name.lower():
            continue
        out.append(
            {
                "name": name,
                "client": scope_client,
                "description": item.get("description") or None,
            }
        )
    return sorted(out, key=lambda x: x["name"].lower())


def _flatten_groups(groups: list, out: List[Dict[str, Any]]) -> None:
    for item in groups:
        path = item.get("path")
        name = item.get("name") or path or ""
        if path:
            out.append({"path": path, "name": name})
        subs = item.get("subGroups") or []
        if subs:
            _flatten_groups(subs, out)


async def list_groups(q: str | None = None, limit: int = 100) -> List[Dict[str, Any]]:
    """Search Keycloak groups for the admin UI group picker."""
    params: dict[str, Any] = {"max": limit}
    if q and q.strip():
        params["search"] = q.strip()
    raw = await _admin_get("/groups", params)
    flat: List[Dict[str, Any]] = []
    _flatten_groups(raw, flat)
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for item in flat:
        path = item["path"]
        if path in seen:
            continue
        seen.add(path)
        out.append(item)
    return sorted(out, key=lambda x: x["path"].lower())
