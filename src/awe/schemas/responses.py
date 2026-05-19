"""
Reusable FastAPI `responses={...}` declarations.

Every non-2xx exit path returns the standard `ErrorResponse` envelope
(`schemas.common.ErrorResponse`) with a stable `AWE-NNN` error code in
`errors[0].errorCode`. Declaring these against each route makes them
appear in the generated OpenAPI spec under the endpoint's `responses`
map — propagated automatically to the GitBook API reference page.

Usage in a controller:

    from ..schemas.responses import (
        ResponseUnauthorized,
        ResponseForbiddenAdmin,
        ResponseRequestNotFound,
    )

    @router.post(
        "/{request_id}/cancel",
        response_model=RequestOut,
        responses={
            **ResponseUnauthorized,
            **ResponseForbiddenAdmin,
            **ResponseRequestNotFound,
            **ResponseStateConflict,
        },
    )

Each declaration is a one-key dict so spreading them composes the full
response map for the endpoint.
"""

from __future__ import annotations

from typing import Any, Dict

from .common import ErrorResponse


def _resp(
    status_code: int,
    description: str,
    error_code: str,
    message_example: str,
) -> Dict[int, Dict[str, Any]]:
    return {
        status_code: {
            "description": description,
            "model": ErrorResponse,
            "content": {
                "application/json": {
                    "example": {
                        "id": "openg2p.awe",
                        "version": "1.0",
                        "responsetime": "2026-04-23T10:00:00.000Z",
                        "response": None,
                        "errors": [
                            {"errorCode": error_code, "message": message_example}
                        ],
                    }
                }
            },
        }
    }


# ---------------------------------------------------------------------------
# Auth — apply to every authenticated endpoint
# ---------------------------------------------------------------------------
ResponseUnauthorized = _resp(
    401,
    "Bearer token missing, malformed, or fails signature/expiry checks.",
    "AWE-008",
    "Invalid or missing bearer token",
)

# Role-gated mutations
ResponseForbiddenAdmin = _resp(
    403,
    "Token is valid but lacks the `AWE_ADMIN` role required for this mutation.",
    "AWE-008",
    "Caller lacks AWE_ADMIN role",
)

# Role-gated reads that require viewer or admin
ResponseForbiddenViewerOrAdmin = _resp(
    403,
    "Token is valid but lacks `AWE_VIEWER` or `AWE_ADMIN` role.",
    "AWE-008",
    "Caller lacks AWE_VIEWER or AWE_ADMIN role",
)

# Task-level: not the assignee of the task
ResponseForbiddenNotAssignee = _resp(
    403,
    "Task is not assigned to the caller and the caller is not an admin.",
    "AWE-008",
    "Task is not assigned to you",
)

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
ResponseBadPolicyDefinition = _resp(
    400,
    "Policy body violates a structural rule (e.g. URL/body key mismatch).",
    "AWE-010",
    "policy_key in body must match URL",
)

ResponseBadCreateRequest = _resp(
    400,
    "Engine refused to start the request (e.g. malformed context).",
    "AWE-007",
    "Engine could not start request",
)

# ---------------------------------------------------------------------------
# Not-found
# ---------------------------------------------------------------------------
ResponsePolicyNotFound = _resp(
    404,
    "No matching policy / policy version exists.",
    "AWE-001",
    "Policy 'registry.cr.v1' not found",
)

ResponsePolicyKeyHasNoActive = _resp(
    404,
    "Policy key has no version in `active` status — no flow can be started for it.",
    "AWE-001",
    "No active policy for 'registry.cr.v1'",
)

ResponseRequestNotFound = _resp(
    404,
    "No approval request with the given id.",
    "AWE-003",
    "Request not found",
)

ResponseTaskNotFound = _resp(
    404,
    "No task with the given id.",
    "AWE-004",
    "Task not found",
)

ResponseDeliveryNotFound = _resp(
    404,
    "No webhook delivery with the given id.",
    "AWE-007",
    "Delivery not found",
)

ResponseDelegationNotFound = _resp(
    404,
    "No delegation with the given id.",
    "AWE-004",
    "Delegation not found",
)

# ---------------------------------------------------------------------------
# State conflicts (409)
# ---------------------------------------------------------------------------
ResponsePolicyConflict = _resp(
    409,
    (
        "Policy version conflict — typically a duplicate `policy_key` on create, "
        "or attempting to edit/activate a version in the wrong status."
    ),
    "AWE-002",
    "policy_key 'registry.cr.v1' already exists (latest v3)",
)

ResponseStateConflict = _resp(
    409,
    (
        "Resource is in a state that disallows this transition. Examples: "
        "request is already in a terminal state (`approved`/`rejected`/`cancelled`); "
        "task is not `open`/`claimed`; policy version is not `draft` for editing; "
        "delivery has already succeeded."
    ),
    "AWE-007",
    "Request is in terminal state 'approved' — cannot cancel",
)

ResponseIdempotencyConflict = _resp(
    409,
    "`Idempotency-Key` already used with a different request body.",
    "AWE-009",
    "Idempotency-Key reused with a different payload",
)

# ---------------------------------------------------------------------------
# Service availability (503)
# ---------------------------------------------------------------------------
ResponseServiceNotReady = _resp(
    503,
    "Service has not finished startup (DB schema not yet ensured, workers not started).",
    "AWE-005",
    "Service is starting up",
)

ResponseDBUnhealthy = _resp(
    503,
    "Health probe failed to reach the database.",
    "AWE-006",
    "Database health check failed",
)

ResponseResolverFailure = _resp(
    503,
    "An approver-resolution rule's upstream call failed (Keycloak admin API, HTTP resolver, etc.).",
    "AWE-007",
    "Keycloak role lookup failed: connection timed out",
)


# ---------------------------------------------------------------------------
# Convenience composites — most endpoints fall into one of these shapes
# ---------------------------------------------------------------------------
def auth_protected(*extras) -> Dict[int, Dict[str, Any]]:
    """Build a responses map starting from the auth pair (401 always)."""
    out: Dict[int, Dict[str, Any]] = {}
    out.update(ResponseUnauthorized)
    for extra in extras:
        out.update(extra)
    return out
