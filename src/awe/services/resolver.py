"""
Approver resolution.

Each `ApproverRule` produces a list of user ids when evaluated against a
request's context. Stage 2+ rules re-resolve against the same frozen context
snapshot stored on the request.

Rule types:
  * `user`       — literal `{"user_id": "<username>"}`
  * `role`       — Keycloak realm-role member lookup (returns usernames)
  * `group`      — Keycloak group member lookup (returns usernames)
  * `expression` — JSONLogic over the context, returning nested rule(s)
  * `http`       — POST {context} to caller's resolver endpoint, returns
                   `{"user_ids": [...]}`

The Keycloak admin API requires a service-account token with realm-management
roles (`view-users`, `query-groups`); operators provision this when standing
up the deployment.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Sequence

import httpx

from ..config import get_settings
from ..models import ApproverRule
from .assignee_id import assignee_id_from_keycloak_user

logger = logging.getLogger(__name__)


class ResolutionError(Exception):
    """Raised when a rule cannot be evaluated (network failure, etc.)."""


def _keycloak_assignee_id(user: dict) -> str:
    """Map a Keycloak admin API user record to the task assignee id.

    Same order as JWT resolution: `preferred_username`, `username`, `sub`
    (Keycloak internal UUID is read from `id` as `sub`).
    """
    assignee_id = assignee_id_from_keycloak_user(user)
    if assignee_id is None:
        raise ResolutionError(
            "Keycloak user record missing preferred_username, username, and id"
        )
    return assignee_id


# Cache by (rule_type, hashable rule_value, context_hash) for the duration of
# a single request lifecycle. The engine resets this map per request.
_ResolutionCache = Dict[tuple, List[str]]


async def resolve_stage(
    rules: Sequence[ApproverRule],
    context: Dict[str, Any],
    cache: _ResolutionCache | None = None,
) -> List[str]:
    """Evaluate every rule, union the resulting user ids, dedup, return sorted."""
    if cache is None:
        cache = {}

    seen: List[str] = []
    seen_set: set[str] = set()
    for rule in rules:
        ids = await _resolve_one(rule, context, cache)
        for user_id in ids:
            if user_id not in seen_set:
                seen_set.add(user_id)
                seen.append(user_id)
    return seen


async def _resolve_one(
    rule: ApproverRule, context: Dict[str, Any], cache: _ResolutionCache
) -> List[str]:
    rule_type = rule.rule_type
    value = rule.rule_value or {}

    if rule_type == "user":
        return [value["user_id"]]

    if rule_type == "role":
        # `client` is optional. If omitted → realm role. If present → the
        # role is looked up on that client (useful when your staff roles
        # live under a portal client, e.g. registry-staff-portal).
        return await _resolve_keycloak_role(value["role"], value.get("client"))

    if rule_type == "group":
        return await _resolve_keycloak_group(value["group"])

    if rule_type == "expression":
        return _resolve_expression(value.get("logic"), context)

    if rule_type == "http":
        return await _resolve_http(value["url"], context)

    raise ResolutionError(f"Unknown rule type: {rule_type}")


# ---------------------------------------------------------------------------
# Keycloak admin lookups
# ---------------------------------------------------------------------------
async def _keycloak_admin_token() -> str:
    cfg = get_settings().awe.keycloak
    if not cfg.base_url:
        raise ResolutionError("Keycloak base_url not configured")
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


async def _resolve_keycloak_role(role: str, client_id: str | None = None) -> List[str]:
    """List users with `role` — realm role if `client_id` is None, else that client's role.

    Client-role lookup needs the client's internal UUID, so we first translate
    `clientId` → UUID via `GET /clients?clientId=...`. Requires `view-clients`
    on the service account (in addition to `view-users`).
    """
    cfg = get_settings().awe.keycloak
    try:
        token = await _keycloak_admin_token()
        base = f"{cfg.base_url.rstrip('/')}/admin/realms/{cfg.realm}"
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=5.0) as http_client:
            if client_id:
                lookup = await http_client.get(
                    f"{base}/clients",
                    headers=headers,
                    params={"clientId": client_id},
                )
                lookup.raise_for_status()
                found = lookup.json()
                if not found:
                    raise ResolutionError(
                        f"Keycloak client not found: {client_id}"
                    )
                uuid = found[0]["id"]
                url = f"{base}/clients/{uuid}/roles/{role}/users"
            else:
                url = f"{base}/roles/{role}/users"
            resp = await http_client.get(url, headers=headers, params={"max": 200})
            resp.raise_for_status()
            return [_keycloak_assignee_id(u) for u in resp.json()]
    except httpx.HTTPError as e:
        logger.warning(
            "Keycloak role lookup failed for %s (client=%s): %s", role, client_id, e
        )
        raise ResolutionError(f"Keycloak role lookup failed: {e}") from e


async def _resolve_keycloak_group(group_path: str) -> List[str]:
    cfg = get_settings().awe.keycloak
    try:
        token = await _keycloak_admin_token()
        # Resolve the group by path → id, then list members.
        path_url = (
            f"{cfg.base_url.rstrip('/')}/admin/realms/{cfg.realm}"
            f"/group-by-path/{group_path.lstrip('/')}"
        )
        async with httpx.AsyncClient(timeout=5.0) as client:
            grp = await client.get(
                path_url, headers={"Authorization": f"Bearer {token}"}
            )
            grp.raise_for_status()
            group_id = grp.json()["id"]
            mem = await client.get(
                f"{cfg.base_url.rstrip('/')}/admin/realms/{cfg.realm}"
                f"/groups/{group_id}/members",
                headers={"Authorization": f"Bearer {token}"},
                params={"max": 200},
            )
            mem.raise_for_status()
            return [_keycloak_assignee_id(u) for u in mem.json()]
    except httpx.HTTPError as e:
        logger.warning("Keycloak group lookup failed for %s: %s", group_path, e)
        raise ResolutionError(f"Keycloak group lookup failed: {e}") from e


# ---------------------------------------------------------------------------
# JSONLogic expression
# ---------------------------------------------------------------------------
def _resolve_expression(logic: Any, context: Dict[str, Any]) -> List[str]:
    if logic is None:
        return []
    try:
        from json_logic import jsonLogic  # type: ignore

        out = jsonLogic(logic, context)
    except Exception as e:  # noqa: BLE001
        raise ResolutionError(f"JSONLogic evaluation failed: {e}") from e

    # Accept either a string (one user) or a list of strings.
    if isinstance(out, str):
        return [out]
    if isinstance(out, list):
        return [str(x) for x in out if x]
    return []


# ---------------------------------------------------------------------------
# HTTP escape hatch
# ---------------------------------------------------------------------------
async def _resolve_http(url: str, context: Dict[str, Any]) -> List[str]:
    cfg = get_settings().awe.resolver
    try:
        async with httpx.AsyncClient(timeout=cfg.http_timeout_seconds) as client:
            resp = await client.post(url, json={"context": context})
            resp.raise_for_status()
            data = resp.json()
            return list(data.get("user_ids") or [])
    except httpx.HTTPError as e:
        logger.warning("HTTP resolver failed for %s: %s", url, e)
        raise ResolutionError(f"HTTP resolver failed: {e}") from e
