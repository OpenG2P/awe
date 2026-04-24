"""
Keycloak OIDC integration.

Two flavours of caller:
  * **Service-to-service** (Caller Svc → AWE) — JWT bearer obtained via
    client_credentials. Validated against the Keycloak JWKS; we just need
    a valid signature + audience.
  * **End-user** (approver via Caller UI proxy) — JWT bearer with `sub`
    claim used as the assignee id, plus realm roles for admin gating.

Validation is best-effort: if `keycloak.issuer` is unset (dev mode), we trust
the bearer's `sub` claim without signature verification — useful for the
docker-compose stack and unit tests, but **never** for production. The Helm
chart sets `awe.keycloak.issuer` so this fallback is unreachable in real
deployments.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from ..config import get_settings

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass
class CallerIdentity:
    """The authenticated caller — either a human approver or a service token."""

    subject: str
    roles: List[str]
    is_service_account: bool
    raw_claims: dict


_jwks_cache: Optional[dict] = None


async def _fetch_jwks(jwks_url: str) -> dict:
    global _jwks_cache
    if _jwks_cache is not None:
        return _jwks_cache
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(jwks_url)
        resp.raise_for_status()
        _jwks_cache = resp.json()
    return _jwks_cache


def _extract_roles(claims: dict) -> List[str]:
    """Union of realm-scoped and client-scoped roles on the token.

    OpenG2P's staff-realm convention (matching Registry / PBMS) scopes
    authorization roles under a per-service client — `awe-admin` lives
    under `resource_access.awe-admin-portal.roles`. We also accept realm
    roles so legacy deployments and dev-mode fixtures keep working.
    """
    roles: set[str] = set()

    realm_access = claims.get("realm_access") or {}
    roles.update(realm_access.get("roles") or [])

    # Every client-scoped role block under `resource_access` is included.
    resource_access = claims.get("resource_access") or {}
    for client_entry in resource_access.values():
        if isinstance(client_entry, dict):
            roles.update(client_entry.get("roles") or [])

    return sorted(roles)


async def _verify_token(token: str) -> dict:
    cfg = get_settings().awe.keycloak

    if not cfg.issuer:
        # Dev mode: decode without signature verification. The decoded `sub`
        # is still required so we can attribute decisions to a user.
        try:
            return jwt.get_unverified_claims(token)
        except JWTError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid bearer token",
            ) from e

    # Fetch the JWKS (with our in-memory cache). Network / TLS / DNS
    # failures here used to bubble up as bare 500s — catch them explicitly
    # so operators see the reason in the response + logs.
    try:
        jwks = await _fetch_jwks(cfg.jwks_url)
    except httpx.HTTPError as e:
        logger.exception("JWKS fetch failed: %s", cfg.jwks_url)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not reach Keycloak JWKS at {cfg.jwks_url}: {e}",
        ) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("JWKS load failed unexpectedly")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"JWKS load failed: {e}",
        ) from e

    try:
        return jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            issuer=cfg.issuer,
            audience=cfg.audience or None,
            options={"verify_aud": bool(cfg.audience)},
        )
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid bearer token: {e}",
        ) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("Unexpected token-decode failure")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token verification failed: {e}",
        ) from e


async def current_identity(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> CallerIdentity:
    if creds is None or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )

    claims = await _verify_token(creds.credentials)
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing `sub` claim",
        )

    return CallerIdentity(
        subject=sub,
        roles=_extract_roles(claims),
        # client_credentials tokens carry `clientId` / `azp` but no human user;
        # treat the absence of `email` as a reasonable proxy.
        is_service_account="email" not in claims,
        raw_claims=claims,
    )


def require_role(role: str):
    """Dependency factory — gate an endpoint on a single Keycloak role."""

    async def _checker(
        identity: CallerIdentity = Depends(current_identity),
    ) -> CallerIdentity:
        if role not in identity.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role `{role}` required",
            )
        return identity

    return _checker


def require_role_any(*roles: str):
    """Dependency factory — gate an endpoint on ANY of the listed roles.

    Useful for read endpoints that should accept AWE_VIEWER or AWE_ADMIN,
    since admins implicitly have viewer privileges.
    """

    role_set = set(roles)

    async def _checker(
        identity: CallerIdentity = Depends(current_identity),
    ) -> CallerIdentity:
        if not role_set.intersection(identity.roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"One of {sorted(role_set)} required",
            )
        return identity

    return _checker


# Canonical role names used across the codebase.
ROLE_ADMIN = "AWE_ADMIN"
ROLE_VIEWER = "AWE_VIEWER"
