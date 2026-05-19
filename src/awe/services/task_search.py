"""Build denormalized search text for approval tasks from request context."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..models import ApprovalRequest


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value).strip()


def build_task_search_text(request: "ApprovalRequest") -> Optional[str]:
    """Flatten artifact ids and request context into a single searchable string."""
    parts: list[str] = [
        request.artifact_type,
        request.artifact_id,
        request.policy_key,
    ]
    ctx = request.context or {}
    for key in sorted(ctx):
        val = ctx[key]
        text = _stringify(val)
        if text:
            parts.append(f"{key}:{text}" if not isinstance(val, (dict, list)) else text)
    combined = " ".join(p for p in parts if p).strip()
    return combined or None
