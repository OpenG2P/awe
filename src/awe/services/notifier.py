"""
Approver notification — minimal SMTP implementation.

Notifications are best-effort: a delivery failure here never blocks the
approval workflow. The dispatcher invokes `notify_assignees()` after a stage
starts; if SMTP is disabled (the v1 default) it's a no-op.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Iterable

from ..config import get_settings

logger = logging.getLogger(__name__)


def _send_email(to_address: str, subject: str, body: str) -> None:
    cfg = get_settings().awe.notifier
    if not cfg.enabled:
        logger.debug("Notifier disabled — skipping email to %s", to_address)
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.from_address
    msg["To"] = to_address
    msg.set_content(body)

    try:
        if cfg.use_tls:
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=10) as smtp:
                smtp.starttls()
                if cfg.smtp_user:
                    smtp.login(cfg.smtp_user, cfg.smtp_password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=10) as smtp:
                if cfg.smtp_user:
                    smtp.login(cfg.smtp_user, cfg.smtp_password)
                smtp.send_message(msg)
    except Exception as e:  # noqa: BLE001
        logger.warning("Notifier failed to email %s: %s", to_address, e)


def notify_assignees(
    assignees: Iterable[str],
    request_id: str,
    artifact_type: str,
    artifact_id: str,
) -> None:
    subject = f"[OpenG2P] Approval pending: {artifact_type}/{artifact_id}"
    body = (
        "An approval task has been assigned to you.\n\n"
        f"Request: {request_id}\n"
        f"Artifact: {artifact_type}/{artifact_id}\n\n"
        "Visit your inbox in the originating service to review."
    )
    for assignee in assignees:
        # `assignee` is a Keycloak user id; the caller is responsible for
        # resolving it to an email — for v1 we treat it as the email itself
        # so the SMTP path is exercisable in the dev stack. Wire to a Keycloak
        # email lookup before production.
        _send_email(assignee, subject, body)
