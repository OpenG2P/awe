"""Pure-unit test for HMAC webhook signing — no DB, no network."""

from __future__ import annotations

import hashlib
import hmac

from awe.services.webhook import sign_body


def test_sign_body_matches_manual_hmac() -> None:
    secret = "shared-secret"
    ts = 1730000000
    body = b'{"event_id":"abc","status":"approved"}'

    expected = hmac.new(
        secret.encode("utf-8"),
        f"{ts}.".encode("utf-8") + body,
        hashlib.sha256,
    ).hexdigest()

    assert sign_body(secret, ts, body) == f"sha256={expected}"


def test_sign_body_includes_timestamp_in_payload() -> None:
    """Two signatures with different timestamps must differ — proves replay-safety."""
    body = b"{}"
    sig_a = sign_body("k", 1, body)
    sig_b = sign_body("k", 2, body)
    assert sig_a != sig_b
