"""
Policy CRUD + simulate.

Admin-only — gated on the Keycloak realm role `awe-admin`. The simulate
endpoint runs the resolver against a sample context so admins can preview
which approvers a given input would resolve to.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from ..db import get_db
from ..models import ApprovalStage
from ..schemas.policy import (
    PolicyCreate,
    PolicyOut,
    PolicyVersionOut,
    SimulateRequest,
    SimulateResponse,
    SimulateStageOut,
)
from ..services import policy as policy_svc
from ..services import resolver as resolver_svc
from ..services.auth import CallerIdentity, require_role
from ._helpers import error, policy_to_out, policy_version_to_out

router = APIRouter(prefix="/v1/awe/policies", tags=["policies"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=PolicyOut,
    summary="Create the first draft of a new policy",
)
async def create_policy(
    payload: PolicyCreate,
    identity: CallerIdentity = Depends(require_role("awe-admin")),
    session=Depends(get_db),
):
    try:
        policy = await policy_svc.create_draft(session, payload, actor=identity.subject)
    except policy_svc.PolicyError as e:
        return error(409, "AWE-002", str(e))
    return policy_to_out(policy)


@router.get(
    "",
    response_model=list[PolicyOut],
    summary="List policies (newest version of each policy_key)",
)
async def list_policies(
    identity: CallerIdentity = Depends(require_role("awe-admin")),
    session=Depends(get_db),
):
    policies = await policy_svc.list_policies(session)
    return [policy_to_out(p) for p in policies]


@router.get(
    "/{policy_key}/versions",
    response_model=list[PolicyVersionOut],
    summary="List all versions of a policy_key",
)
async def list_versions(
    policy_key: str,
    identity: CallerIdentity = Depends(require_role("awe-admin")),
    session=Depends(get_db),
):
    versions = await policy_svc.list_versions(session, policy_key)
    if not versions:
        return error(404, "AWE-001", f"Policy '{policy_key}' not found")
    return [policy_version_to_out(p) for p in versions]


@router.get(
    "/{policy_key}/versions/{version}",
    response_model=PolicyOut,
    summary="Fetch a specific policy version with stages and rules",
)
async def get_version(
    policy_key: str,
    version: int,
    identity: CallerIdentity = Depends(require_role("awe-admin")),
    session=Depends(get_db),
):
    policy = await policy_svc.get_version(session, policy_key, version)
    if policy is None:
        return error(404, "AWE-001", f"Policy '{policy_key}' v{version} not found")
    return policy_to_out(policy)


@router.put(
    "/{policy_key}",
    status_code=status.HTTP_201_CREATED,
    response_model=PolicyOut,
    summary="Add a new draft version under an existing policy_key",
)
async def add_version(
    policy_key: str,
    payload: PolicyCreate,
    identity: CallerIdentity = Depends(require_role("awe-admin")),
    session=Depends(get_db),
):
    if payload.policy_key != policy_key:
        return error(400, "AWE-010", "policy_key in body must match URL")
    try:
        policy = await policy_svc.add_draft_version(
            session, policy_key, payload, actor=identity.subject
        )
    except policy_svc.PolicyNotFound as e:
        return error(404, "AWE-001", str(e))
    return policy_to_out(policy)


@router.patch(
    "/{policy_key}/versions/{version}",
    response_model=PolicyOut,
    summary="Edit a draft version in place (drafts only)",
)
async def edit_draft(
    policy_key: str,
    version: int,
    payload: PolicyCreate,
    identity: CallerIdentity = Depends(require_role("awe-admin")),
    session=Depends(get_db),
):
    if payload.policy_key != policy_key:
        return error(400, "AWE-010", "policy_key in body must match URL")
    try:
        policy = await policy_svc.update_draft(
            session, policy_key, version, payload, actor=identity.subject
        )
    except policy_svc.PolicyNotFound as e:
        return error(404, "AWE-001", str(e))
    except policy_svc.PolicyError as e:
        return error(409, "AWE-007", str(e))
    return policy_to_out(policy)


@router.post(
    "/{policy_key}/versions/{version}/activate",
    response_model=PolicyOut,
    summary="Activate a specific version (archives the previously active one)",
)
async def activate(
    policy_key: str,
    version: int,
    identity: CallerIdentity = Depends(require_role("awe-admin")),
    session=Depends(get_db),
):
    try:
        policy = await policy_svc.activate_version(session, policy_key, version)
    except policy_svc.PolicyNotFound as e:
        return error(404, "AWE-001", str(e))
    return policy_to_out(policy)


@router.post(
    "/{policy_key}/versions/{version}/deactivate",
    response_model=PolicyOut,
    summary=(
        "Archive an active version without activating a replacement. "
        "New POST /requests for this policy_key will fail with AWE-001 "
        "until another version is activated; in-flight requests continue."
    ),
)
async def deactivate(
    policy_key: str,
    version: int,
    identity: CallerIdentity = Depends(require_role("awe-admin")),
    session=Depends(get_db),
):
    try:
        policy = await policy_svc.deactivate_version(session, policy_key, version)
    except policy_svc.PolicyNotFound as e:
        return error(404, "AWE-001", str(e))
    except policy_svc.PolicyError as e:
        return error(409, "AWE-007", str(e))
    return policy_to_out(policy)


@router.post(
    "/{policy_key}/versions/{version}/simulate",
    response_model=SimulateResponse,
    summary="Resolve approvers for a sample context — no DB writes",
)
async def simulate(
    policy_key: str,
    version: int,
    payload: SimulateRequest,
    identity: CallerIdentity = Depends(require_role("awe-admin")),
    session=Depends(get_db),
):
    policy = await policy_svc.get_version(session, policy_key, version)
    if policy is None:
        return error(404, "AWE-001", f"Policy '{policy_key}' v{version} not found")

    out_stages: list[SimulateStageOut] = []
    cache: dict = {}
    for stage in sorted(policy.stages, key=lambda s: s.stage_order):
        skip_reason = _evaluate_skip(stage, payload.context)
        if skip_reason:
            out_stages.append(
                SimulateStageOut(
                    stage_order=stage.stage_order,
                    name=stage.name,
                    mode=stage.mode,
                    mode_value=stage.mode_value,
                    resolved_approvers=[],
                    skipped=True,
                    skip_reason=skip_reason,
                )
            )
            continue

        try:
            approvers = await resolver_svc.resolve_stage(
                stage.rules, payload.context, cache
            )
        except resolver_svc.ResolutionError as e:
            return error(503, "AWE-007", f"Resolver failed: {e}")

        out_stages.append(
            SimulateStageOut(
                stage_order=stage.stage_order,
                name=stage.name,
                mode=stage.mode,
                mode_value=stage.mode_value,
                resolved_approvers=approvers,
                skipped=False,
            )
        )

    return SimulateResponse(
        policy_id=policy.id,
        policy_version=policy.version,
        stages=out_stages,
    )


def _evaluate_skip(stage: ApprovalStage, context: dict) -> str | None:
    if not stage.skip_if:
        return None
    try:
        from json_logic import jsonLogic  # type: ignore

        if jsonLogic(stage.skip_if, context):
            return "skip_if"
    except Exception:  # noqa: BLE001
        return None
    return None
