"""Shared assignee-id resolution for tasks and approver rules.

Task assignees must use the same identifier whether they come from a JWT
(caller acting on a task) or from Keycloak admin lookups (role/group rules).
"""

from __future__ import annotations

from typing import Optional, Sequence

# Always resolve in this order.
ASSIGNEE_ID_KEYS: Sequence[str] = ("preferred_username", "username", "sub")


def first_assignee_id(source: dict, keys: Sequence[str] = ASSIGNEE_ID_KEYS) -> Optional[str]:
    for key in keys:
        value = source.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return None


def assignee_id_from_claims(claims: dict) -> Optional[str]:
    """Resolve assignee id from a bearer token."""
    return first_assignee_id(claims)


def assignee_id_from_keycloak_user(user: dict) -> Optional[str]:
    """Resolve assignee id from a Keycloak Admin API user object.

    Admin API records expose `username` and internal UUID as `id` (same value
    as JWT `sub`). Map `id` → `sub` so both paths share the same fallback order.
    """
    normalized = dict(user)
    if not normalized.get("sub") and normalized.get("id"):
        normalized["sub"] = normalized["id"]
    return first_assignee_id(normalized)
