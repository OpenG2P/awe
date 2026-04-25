"""
Policy CRUD + versioning.

Versioning rule:
  * Editing a policy creates a NEW draft version of the same `policy_key`.
  * Activating a new version flips it to `active` and any previously active
    version (with the same `policy_key`) to `archived`.
  * In-flight requests are unaffected — they hold a foreign key to the exact
    policy version (`approval_request.policy_id`).
"""

from __future__ import annotations

import logging
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models import ApprovalPolicy, ApprovalStage, ApproverRule
from ..schemas.policy import PolicyCreate, StageIn

logger = logging.getLogger(__name__)


class PolicyError(Exception):
    pass


class PolicyNotFound(PolicyError):
    pass


async def create_draft(
    session: AsyncSession,
    payload: PolicyCreate,
    actor: Optional[str] = None,
) -> ApprovalPolicy:
    """Create a fresh draft policy at version 1.

    Use `add_draft_version()` to bump existing keys.
    """
    existing = await _latest_version(session, payload.policy_key)
    if existing is not None:
        raise PolicyError(
            f"policy_key '{payload.policy_key}' already exists (latest v{existing.version}); "
            "use `add_draft_version` to add a new version"
        )

    policy = ApprovalPolicy(
        policy_key=payload.policy_key,
        version=1,
        name=payload.name,
        description=payload.description,
        artifact_type=payload.artifact_type,
        forbid_self_approval=payload.forbid_self_approval,
        forbid_repeat_approvers=payload.forbid_repeat_approvers,
        status="draft",
        created_by=actor,
    )
    _attach_stages(policy, payload.stages)
    session.add(policy)
    await session.flush()
    return policy


async def add_draft_version(
    session: AsyncSession,
    policy_key: str,
    payload: PolicyCreate,
    actor: Optional[str] = None,
) -> ApprovalPolicy:
    latest = await _latest_version(session, policy_key)
    if latest is None:
        raise PolicyNotFound(f"No prior versions of '{policy_key}'")

    policy = ApprovalPolicy(
        policy_key=policy_key,
        version=latest.version + 1,
        name=payload.name,
        description=payload.description,
        artifact_type=payload.artifact_type,
        forbid_self_approval=payload.forbid_self_approval,
        forbid_repeat_approvers=payload.forbid_repeat_approvers,
        status="draft",
        created_by=actor,
    )
    _attach_stages(policy, payload.stages)
    session.add(policy)
    await session.flush()
    return policy


async def update_draft(
    session: AsyncSession,
    policy_key: str,
    version: int,
    payload: PolicyCreate,
    actor: Optional[str] = None,
) -> ApprovalPolicy:
    """Replace a draft version's metadata + stages in place.

    Drafts are mutable because they've never been activated — no in-flight
    requests reference them. Active and archived versions reject this call.
    """
    policy = await _get_version(session, policy_key, version, with_stages=True)
    if policy is None:
        raise PolicyNotFound(f"Policy {policy_key} v{version} not found")
    if policy.status != "draft":
        raise PolicyError(
            f"Cannot edit v{version}: status is '{policy.status}'. "
            "Active/archived versions are immutable — create a new draft version instead."
        )

    policy.name = payload.name
    policy.description = payload.description
    policy.artifact_type = payload.artifact_type
    policy.forbid_self_approval = payload.forbid_self_approval
    policy.forbid_repeat_approvers = payload.forbid_repeat_approvers
    if actor:
        policy.created_by = actor

    # Rebuild stages — `cascade="all, delete-orphan"` drops the old ones.
    policy.stages.clear()
    await session.flush()
    _attach_stages(policy, payload.stages)
    await session.flush()
    return policy


async def activate_version(
    session: AsyncSession, policy_key: str, version: int
) -> ApprovalPolicy:
    target = await _get_version(session, policy_key, version)
    if target is None:
        raise PolicyNotFound(f"Policy {policy_key} v{version} not found")
    if target.status == "active":
        return target

    # Archive any currently-active version of the same key.
    rows = await session.execute(
        select(ApprovalPolicy).where(
            ApprovalPolicy.policy_key == policy_key,
            ApprovalPolicy.status == "active",
        )
    )
    for prior in rows.scalars():
        prior.status = "archived"

    target.status = "active"
    await session.flush()
    return target


async def deactivate_version(
    session: AsyncSession, policy_key: str, version: int
) -> ApprovalPolicy:
    """Flip an active version to `archived` without activating a replacement.

    After this, `policy_key` has no active version — `POST /requests` for
    that key fails with AWE-001 until a different version is activated.
    In-flight requests are unaffected; they reference this version by id
    and continue resolving stages against its rules.
    """
    target = await _get_version(session, policy_key, version)
    if target is None:
        raise PolicyNotFound(f"Policy {policy_key} v{version} not found")
    if target.status != "active":
        raise PolicyError(
            f"v{version} is '{target.status}', not 'active' — nothing to deactivate"
        )
    target.status = "archived"
    await session.flush()
    return target


async def get_active(
    session: AsyncSession, policy_key: str
) -> Optional[ApprovalPolicy]:
    row = await session.execute(
        select(ApprovalPolicy)
        .options(
            selectinload(ApprovalPolicy.stages).selectinload(ApprovalStage.rules)
        )
        .where(
            ApprovalPolicy.policy_key == policy_key,
            ApprovalPolicy.status == "active",
        )
    )
    return row.scalar_one_or_none()


async def get_version(
    session: AsyncSession, policy_key: str, version: int
) -> Optional[ApprovalPolicy]:
    return await _get_version(session, policy_key, version, with_stages=True)


async def list_policies(session: AsyncSession) -> List[ApprovalPolicy]:
    """Return the newest version per policy_key."""
    rows = await session.execute(
        select(ApprovalPolicy).order_by(
            ApprovalPolicy.policy_key, ApprovalPolicy.version.desc()
        )
    )
    seen: dict[str, ApprovalPolicy] = {}
    for p in rows.scalars():
        if p.policy_key not in seen:
            seen[p.policy_key] = p
    return list(seen.values())


async def list_versions(
    session: AsyncSession, policy_key: str
) -> List[ApprovalPolicy]:
    rows = await session.execute(
        select(ApprovalPolicy)
        .where(ApprovalPolicy.policy_key == policy_key)
        .order_by(ApprovalPolicy.version.desc())
    )
    return list(rows.scalars())


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
async def _latest_version(
    session: AsyncSession, policy_key: str
) -> Optional[ApprovalPolicy]:
    row = await session.execute(
        select(ApprovalPolicy)
        .where(ApprovalPolicy.policy_key == policy_key)
        .order_by(ApprovalPolicy.version.desc())
        .limit(1)
    )
    return row.scalar_one_or_none()


async def _get_version(
    session: AsyncSession,
    policy_key: str,
    version: int,
    with_stages: bool = True,
) -> Optional[ApprovalPolicy]:
    stmt = select(ApprovalPolicy).where(
        ApprovalPolicy.policy_key == policy_key,
        ApprovalPolicy.version == version,
    )
    if with_stages:
        stmt = stmt.options(
            selectinload(ApprovalPolicy.stages).selectinload(ApprovalStage.rules)
        )
    row = await session.execute(stmt)
    return row.scalar_one_or_none()


def _attach_stages(policy: ApprovalPolicy, stages: List[StageIn]) -> None:
    for stage_in in stages:
        stage = ApprovalStage(
            stage_order=stage_in.stage_order,
            name=stage_in.name,
            mode=stage_in.mode,
            mode_value=stage_in.mode_value,
            sla_hours=stage_in.sla_hours,
            skip_if=stage_in.skip_if,
            on_empty=stage_in.on_empty,
            parallel_group=stage_in.parallel_group,
            on_breach=stage_in.on_breach,
            escalation_rules_json=[
                {
                    "rule_type": r.rule_type,
                    "rule_value": r.rule_value,
                    "kind": r.kind,
                    "required": r.required,
                }
                for r in stage_in.escalation_rules
            ] or None,
        )
        for rule_in in stage_in.rules:
            stage.rules.append(
                ApproverRule(
                    rule_type=rule_in.rule_type,
                    rule_value=rule_in.rule_value,
                    kind=rule_in.kind,
                    required=rule_in.required,
                )
            )
        policy.stages.append(stage)
