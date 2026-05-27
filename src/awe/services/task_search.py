"""Build denormalized search text for approval tasks from request context."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..models import ApprovalRequest

# Preferred order for registry task display/search fields.
_CORE_CONTEXT_KEYS = (
    "record_name",
    "register_mnemonic",
    "section_mnemonic",
    "intake_form_mnemonic",
    "change_request_id",
    "submission_id",
)

def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value).strip()


def build_task_search_text(request: "ApprovalRequest") -> Optional[str]:
    """Flatten human-readable request context into a single searchable string.

    Includes change_request_id / submission_id (or artifact_id fallback) so tasks
    are searchable by CR or intake submission id. Excludes artifact_type and
    policy_key.
    """
    parts: list[str] = []
    ctx = request.context or {}
    artifact_id = (request.artifact_id or "").strip()
    id_in_search = False

    for key in _CORE_CONTEXT_KEYS:
        text = _stringify(ctx.get(key))
        if not text:
            continue
        if key in ("change_request_id", "submission_id"):
            id_in_search = True
        parts.append(text)

    included = set(_CORE_CONTEXT_KEYS)
    for key in sorted(ctx):
        if key in included:
            continue
        val = ctx[key]
        if isinstance(val, (dict, list)):
            continue
        text = _stringify(val)
        if text:
            parts.append(text)

    if not id_in_search and artifact_id:
        parts.append(artifact_id)

    combined = " ".join(parts).strip()
    return combined or None
