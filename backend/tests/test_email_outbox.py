"""Undelivered mail is retried, not lost -- and not kept around once it is done with."""
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from app import email as mailer
from app.database import SessionLocal
from app.models import EmailOutbox, utc_naive


class FlakyProvider:
    name = "flaky"

    def __init__(self, fail: bool = True):
        self.fail, self.sent = fail, []

    def send(self, message):
        if self.fail:
            raise ConnectionRefusedError("relay down")
        self.sent.append(message)


def _row(recipient):
    with SessionLocal() as db:
        return db.scalar(select(EmailOutbox).where(EmailOutbox.recipient == recipient))


def _make_due(recipient, **changes):
    with SessionLocal() as db:
        row = db.scalar(select(EmailOutbox).where(EmailOutbox.recipient == recipient))
        row.next_attempt_at = utc_naive() - timedelta(seconds=1)
        for key, value in changes.items():
            setattr(row, key, value)
        db.commit()


def test_a_failed_send_is_queued_and_delivered_on_retry(monkeypatch):
    provider = FlakyProvider(fail=True)
    monkeypatch.setattr(mailer, "_provider", provider)
    recipient = f"{uuid4().hex}@example.com"

    mailer.send_password_reset(recipient, "Pat", "raw-token-value")  # must not raise
    queued = _row(recipient)
    assert queued.status == "pending" and queued.attempts == 1
    assert queued.next_attempt_at > utc_naive()  # backs off rather than hammering the relay

    _make_due(recipient)
    mailer.retry_pending()
    assert _row(recipient).attempts == 2  # still down: rescheduled, not dropped

    provider.fail = False
    _make_due(recipient)
    mailer.retry_pending()
    delivered = _row(recipient)
    assert delivered.status == "sent"
    assert delivered.body is None  # the single-use link is not kept at rest
    assert provider.sent[0].to == recipient and "raw-token-value" in provider.sent[0].body


def test_a_message_is_abandoned_after_its_retry_budget(monkeypatch):
    monkeypatch.setattr(mailer, "_provider", FlakyProvider(fail=True))
    recipient = f"{uuid4().hex}@example.com"
    mailer.send_verification(recipient, "Pat", "raw-token-value")
    _make_due(recipient, attempts=mailer.MAX_ATTEMPTS)
    mailer.retry_pending()
    row = _row(recipient)
    assert row.status == "abandoned" and row.body is None


def test_a_successful_first_send_stores_nothing(monkeypatch):
    provider = FlakyProvider(fail=False)
    monkeypatch.setattr(mailer, "_provider", provider)
    recipient = f"{uuid4().hex}@example.com"
    mailer.send_verification(recipient, "Pat", "raw-token-value")
    assert provider.sent and _row(recipient) is None
