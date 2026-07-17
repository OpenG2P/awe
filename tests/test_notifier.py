"""Unit tests for awe.services.notifier."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from awe.services import notifier as notifier_svc


def test_notify_assignees_disabled():
    cfg = MagicMock()
    cfg.enabled = False
    with patch("awe.services.notifier.get_settings") as gs:
        gs.return_value.awe.notifier = cfg
        notifier_svc.notify_assignees(["user@test"], "req", "type", "id")


def test_send_email_tls_with_login():
    cfg = MagicMock()
    cfg.enabled = True
    cfg.use_tls = True
    cfg.smtp_host = "smtp.test"
    cfg.smtp_port = 587
    cfg.smtp_user = "u"
    cfg.smtp_password = "p"
    cfg.from_address = "noreply@test"

    smtp = MagicMock()
    smtp.__enter__ = MagicMock(return_value=smtp)
    smtp.__exit__ = MagicMock(return_value=False)

    with patch("awe.services.notifier.get_settings") as gs, patch(
        "awe.services.notifier.smtplib.SMTP", return_value=smtp
    ) as smtp_cls:
        gs.return_value.awe.notifier = cfg
        notifier_svc._send_email("to@test", "subj", "body")
        smtp_cls.assert_called_once_with("smtp.test", 587, timeout=10)
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("u", "p")
        smtp.send_message.assert_called_once()


def test_send_email_plain_no_login():
    cfg = MagicMock()
    cfg.enabled = True
    cfg.use_tls = False
    cfg.smtp_host = "smtp.test"
    cfg.smtp_port = 25
    cfg.smtp_user = ""
    cfg.from_address = "noreply@test"

    smtp = MagicMock()
    smtp.__enter__ = MagicMock(return_value=smtp)
    smtp.__exit__ = MagicMock(return_value=False)

    with patch("awe.services.notifier.get_settings") as gs, patch(
        "awe.services.notifier.smtplib.SMTP", return_value=smtp
    ):
        gs.return_value.awe.notifier = cfg
        notifier_svc._send_email("to@test", "subj", "body")
        smtp.login.assert_not_called()


def test_send_email_logs_failure():
    cfg = MagicMock()
    cfg.enabled = True
    cfg.use_tls = False
    cfg.smtp_host = "smtp.test"
    cfg.smtp_port = 25
    cfg.smtp_user = ""
    cfg.from_address = "noreply@test"

    with patch("awe.services.notifier.get_settings") as gs, patch(
        "awe.services.notifier.smtplib.SMTP", side_effect=OSError("network down")
    ):
        gs.return_value.awe.notifier = cfg
        notifier_svc._send_email("to@test", "subj", "body")
