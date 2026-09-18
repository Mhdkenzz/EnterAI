"""Transactional email, behind one `send(message)` call.

Two providers. `console` records messages in memory and logs that one was sent --
the default for development and tests, where nothing should ever leave the machine.
`smtp` talks to a real relay, which is how every hosted provider (SES, SendGrid,
Mailgun, Postmark) can be reached without a vendor SDK.

Production fails closed: password reset and email verification are security
controls, and a deployment whose mail silently goes to a console log would lock
users out of their own accounts while appearing to work. Required configuration is
therefore validated at import, the same way JWT_SECRET and REDIS_URL are.

Message bodies are assembled here rather than at the call sites so that no caller
can accidentally put a raw token into a subject line or a log.
"""
from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage as MIMEMessage

logger = logging.getLogger("enterai.email")

_provider = None


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    body: str


class ConsoleEmailProvider:
    """Captures messages instead of sending them. `outbox` is what tests read; it
    is never populated in production because production refuses this provider."""

    name = "console"

    def __init__(self):
        self.outbox: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.outbox.append(message)
        # The body carries single-use credentials, so only the subject is logged.
        logger.info("email_sent", extra={"event": "email_sent", "provider": self.name,
                                         "subject": message.subject})


class SMTPEmailProvider:
    name = "smtp"

    def __init__(self, host: str, port: int, username: str | None, password: str | None,
                 sender: str, use_tls: bool):
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.sender, self.use_tls = sender, use_tls

    def send(self, message: EmailMessage) -> None:
        mime = MIMEMessage()
        mime["From"] = self.sender
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.body)
        with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
            if self.use_tls:
                smtp.starttls()
            if self.username and self.password:
                smtp.login(self.username, self.password)
            smtp.send_message(mime)
        logger.info("email_sent", extra={"event": "email_sent", "provider": self.name,
                                         "subject": message.subject})


def _build_provider():
    environment = os.getenv("ENVIRONMENT", "development").strip().lower()
    configured = os.getenv("EMAIL_PROVIDER", "").strip().lower()
    if environment == "production":
        if configured != "smtp":
            raise RuntimeError(
                "EMAIL_PROVIDER must be set to 'smtp' in production. Password reset,"
                " email verification and invites are delivered by email: a deployment"
                " that kept the console provider would appear to send them while"
                " locking real users out of their own accounts."
            )
        missing = [name for name in ("SMTP_HOST", "EMAIL_FROM", "APP_BASE_URL") if not os.getenv(name, "").strip()]
        if missing:
            raise RuntimeError(
                f"{', '.join(missing)} must be set in production for transactional email."
                " APP_BASE_URL is required because reset and invite links are built from it."
            )
    if configured == "smtp":
        return SMTPEmailProvider(
            host=os.getenv("SMTP_HOST", "").strip(),
            port=int(os.getenv("SMTP_PORT", "587")),
            username=os.getenv("SMTP_USERNAME") or None,
            password=os.getenv("SMTP_PASSWORD") or None,
            sender=os.getenv("EMAIL_FROM", "no-reply@enterai.local").strip(),
            use_tls=os.getenv("SMTP_STARTTLS", "true").strip().lower() in {"1", "true", "yes", "on"},
        )
    if configured in {"", "console"}:
        return ConsoleEmailProvider()
    raise RuntimeError(f"Unsupported EMAIL_PROVIDER={configured!r}; expected 'console' or 'smtp'")


def get_email_provider():
    global _provider
    if _provider is None:
        _provider = _build_provider()
    return _provider


def reset_email_provider() -> None:
    """Tests move between provider configurations; drop the memoized instance."""
    global _provider
    _provider = None


def app_base_url() -> str:
    return os.getenv("APP_BASE_URL", "http://localhost:3000").strip().rstrip("/")


def send(message: EmailMessage) -> None:
    """Delivery failures never propagate to the caller. Every send sits behind an
    endpoint that must not reveal whether an address exists, and an exception would
    turn a 202 into a 500 for real addresses only -- an enumeration oracle built
    out of error handling."""
    try:
        get_email_provider().send(message)
    except Exception as error:
        logger.warning("email_send_failed", extra={"event": "email_send_failed",
                                                   "error": type(error).__name__})


def send_verification(to: str, name: str, token: str) -> None:
    link = f"{app_base_url()}/?verify={token}"
    send(EmailMessage(to, "Confirm your Enter AI email address", (
        f"Hi {name},\n\nConfirm this address to finish setting up your Enter AI account:\n\n"
        f"{link}\n\nThe link expires in 24 hours. If you did not create an account, ignore this email.\n"
    )))


def send_password_reset(to: str, name: str, token: str) -> None:
    link = f"{app_base_url()}/?reset={token}"
    send(EmailMessage(to, "Reset your Enter AI password", (
        f"Hi {name},\n\nUse this link to choose a new password:\n\n{link}\n\n"
        "The link expires in 60 minutes and can only be used once. If you did not "
        "request a reset, nothing has changed and you can ignore this email.\n"
    )))


def send_password_reset_unknown(to: str) -> None:
    """Sent when a reset is requested for an address with no account. Every request
    gets a reply, so the presence or absence of an email is not a signal either."""
    send(EmailMessage(to, "Reset your Enter AI password", (
        "Someone asked to reset the Enter AI password for this address, but there is "
        "no account here.\n\nIf this was you, you may have signed up with a different "
        "address. If not, you can ignore this email.\n"
    )))


def send_invite(to: str, organization: str, inviter: str, token: str) -> None:
    link = f"{app_base_url()}/?invite={token}"
    send(EmailMessage(to, f"{inviter} invited you to {organization} on Enter AI", (
        f"{inviter} has invited you to join {organization} on Enter AI.\n\n"
        f"Accept the invitation and choose a password:\n\n{link}\n\n"
        "The invitation expires in 7 days and can only be used once.\n"
    )))


def _validate_configuration_at_import() -> None:
    """Resolve the provider once at import, the way JWT_SECRET and REDIS_URL are.

    Left lazy, a production deployment with no mail configuration would start
    cleanly and only fail at the first password reset -- by which point a locked-out
    user is already waiting on an email that will never arrive.
    """
    get_email_provider()


def send_existing_account_notice(to: str, organization: str) -> None:
    """Sent when someone tries to sign up with an address that already has an
    account: the signup response itself must not say so."""
    send(EmailMessage(to, "Someone tried to sign up with your email", (
        f"An Enter AI signup was attempted with this address, which already belongs to "
        f"an account in {organization}.\n\nIf this was you, sign in instead, or reset "
        "your password if you have forgotten it. If it was not, no action is needed.\n"
    )))


_validate_configuration_at_import()
