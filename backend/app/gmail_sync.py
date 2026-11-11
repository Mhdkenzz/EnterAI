"""Incremental Gmail inbox sync into the organization-scoped Requests queue."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import logging
import os
import re
import zlib
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from urllib.parse import quote

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import SessionLocal, engine
from .gmail_api import (
    GmailHistoryExpired, GmailMessageUnavailable, GmailProviderError, GmailReauthorizationRequired,
    get_access_token, get_profile, gmail_get,
)
from .models import InboundMessage, InboundPlan, IntegrationConnection, Notification, User
from .request_planning import triage_inbound_message
from .services import log
from .time_utils import utcnow_naive
from .slack_api import SlackProviderError, SlackReauthorizationRequired, refresh_connection_access_token
from . import notion_api, notion_sync

POLL_INTERVAL_SECONDS = max(10, int(os.getenv("GMAIL_SYNC_INTERVAL_SECONDS", "30")))
LOCK_NAMESPACE = 0x454749  # EGI -- distinct from the agent-execution advisory locks
_background_task: asyncio.Task | None = None
logger = logging.getLogger("enterai.gmail_sync")


def _lock_key(connection_id: str) -> int:
    return zlib.crc32(connection_id.encode()) - 2**31


@contextlib.contextmanager
def _connection_lock(bind, connection_id: str):
    if bind.dialect.name != "postgresql":
        yield True
        return
    connection = bind.connect()
    acquired = False
    try:
        acquired = bool(connection.execute(
            text("SELECT pg_try_advisory_lock(:namespace, :key)"),
            {"namespace": LOCK_NAMESPACE, "key": _lock_key(connection_id)},
        ).scalar())
        yield acquired
    finally:
        if acquired:
            connection.execute(
                text("SELECT pg_advisory_unlock(:namespace, :key)"),
                {"namespace": LOCK_NAMESPACE, "key": _lock_key(connection_id)},
            )
        connection.close()


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (ValueError, UnicodeError):
        return value


def _decode_part(data: str | None) -> str:
    if not data:
        return ""
    try:
        encoded = data.encode("ascii")
        encoded += b"=" * (-len(encoded) % 4)
        return base64.urlsafe_b64decode(encoded).decode("utf-8", errors="replace")
    except (ValueError, UnicodeError):
        return ""


def _collect_body(payload: dict, plain: list[str], rich: list[str]) -> None:
    mime_type = (payload.get("mimeType") or "").lower()
    body = payload.get("body") or {}
    if body.get("data") and not body.get("attachmentId"):
        decoded = _decode_part(body["data"])
        if mime_type == "text/plain":
            plain.append(decoded)
        elif mime_type == "text/html":
            rich.append(decoded)
    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            _collect_body(part, plain, rich)


def _message_body(message: dict) -> str:
    plain: list[str] = []
    rich: list[str] = []
    _collect_body(message.get("payload") or {}, plain, rich)
    text_body = "\n".join(part for part in plain if part.strip())
    if not text_body:
        markup = "\n".join(rich)
        markup = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", markup)
        text_body = html.unescape(re.sub(r"<[^>]+>", " ", markup))
        text_body = re.sub(r"\s+", " ", text_body)
    return text_body.strip()[:50000]


def _inbound_from_gmail(connection: IntegrationConnection, message: dict) -> InboundMessage | None:
    labels = set(message.get("labelIds") or [])
    if "INBOX" not in labels or labels.intersection({"SENT", "DRAFT", "SPAM", "TRASH"}):
        return None
    message_id = message.get("id")
    if not isinstance(message_id, str) or not message_id:
        return None
    headers = {
        item.get("name", "").lower(): _decode_header(item.get("value"))
        for item in (message.get("payload") or {}).get("headers", [])
        if isinstance(item, dict)
    }
    try:
        received_at = datetime.fromtimestamp(int(message.get("internalDate", "0")) / 1000, timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError, OverflowError):
        received_at = utcnow_naive()
    body = _message_body(message)
    if not body:
        body = "(This email did not contain readable text.)"
    return InboundMessage(
        organization_id=connection.organization_id, source="gmail", external_id=message_id,
        thread_id=message.get("threadId"), sender=headers.get("from", "")[:500] or None,
        subject=headers.get("subject", "")[:500] or None, body=body,
        source_url=f"https://mail.google.com/mail/u/0/#all/{quote(message_id, safe='')}",
        received_at=received_at, status="new", triage_status="pending", created_by=connection.connected_by,
    )


def _insert_message(db: Session, connection: IntegrationConnection, message: dict) -> bool:
    incoming = _inbound_from_gmail(connection, message)
    if not incoming:
        return False
    existing = db.scalar(select(InboundMessage.id).where(
        InboundMessage.organization_id == connection.organization_id,
        InboundMessage.source == "gmail", InboundMessage.external_id == incoming.external_id,
    ))
    if existing:
        return False
    try:
        with db.begin_nested():
            db.add(incoming)
            db.flush()
        return True
    except IntegrityError:
        # A second API replica may have committed the same message since the
        # preceding read. The unique org/source/message key makes retry harmless.
        return False


def _fetch_message(access_token: str, message_id: str) -> dict | None:
    try:
        return gmail_get(f"/users/me/messages/{quote(message_id, safe='')}", access_token, {"format": "full"})
    except GmailMessageUnavailable:
        # Deleted or moved messages can disappear between history.list and get.
        return None


def _collect_history_ids(access_token: str, start_history_id: str) -> tuple[list[str], str]:
    page_token: str | None = None
    ids: set[str] = set()
    newest_history_id = start_history_id
    while True:
        params: dict[str, str | int] = {"startHistoryId": start_history_id, "historyTypes": "messageAdded", "maxResults": 500}
        if page_token:
            params["pageToken"] = page_token
        page = gmail_get("/users/me/history", access_token, params)
        history_id = page.get("historyId")
        if isinstance(history_id, str):
            newest_history_id = history_id
        for item in page.get("history") or []:
            for added in item.get("messagesAdded") or []:
                gmail_message = added.get("message") or {}
                if isinstance(gmail_message.get("id"), str):
                    ids.add(gmail_message["id"])
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    return list(ids), newest_history_id


def _list_after(access_token: str, date: datetime) -> list[str]:
    # Gmail search dates use a calendar date. Two days of overlap avoids a gap
    # around mailbox timezone boundaries; message IDs are deduplicated in storage.
    after_date = (date - timedelta(days=2)).strftime("%Y/%m/%d")
    page_token: str | None = None
    ids: set[str] = set()
    while True:
        params: dict[str, str | int] = {"q": f"after:{after_date}", "maxResults": 500}
        if page_token:
            params["pageToken"] = page_token
        page = gmail_get("/users/me/messages", access_token, params)
        for item in page.get("messages") or []:
            if isinstance(item.get("id"), str):
                ids.add(item["id"])
        page_token = page.get("nextPageToken")
        if not page_token:
            return list(ids)


def _recover_expired_cursor(db: Session, connection: IntegrationConnection, access_token: str) -> tuple[int, str]:
    before = connection.last_sync_at or datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    profile = get_profile(access_token)
    ids = _list_after(access_token, before)
    inserted = 0
    for message_id in ids:
        message = _fetch_message(access_token, message_id)
        if message:
            inserted += int(_insert_message(db, connection, message))
    return inserted, profile["historyId"]


def _sync_one(db: Session, connection: IntegrationConnection) -> int:
    access_token = get_access_token(connection)
    if not connection.history_cursor:
        profile = get_profile(access_token)
        connection.history_cursor = profile["historyId"]
        connection.last_sync_at = utcnow_naive()
        connection.last_error = None
        db.commit()
        return 0
    try:
        ids, cursor = _collect_history_ids(access_token, connection.history_cursor)
    except GmailHistoryExpired:
        inserted, cursor = _recover_expired_cursor(db, connection, access_token)
        connection.history_cursor = cursor
        connection.last_sync_at = utcnow_naive()
        connection.last_error = None
        db.commit()
        return inserted
    inserted = 0
    for message_id in ids:
        message = _fetch_message(access_token, message_id)
        if message:
            inserted += int(_insert_message(db, connection, message))
    connection.history_cursor = cursor
    connection.last_sync_at = utcnow_naive()
    connection.last_error = None
    db.commit()
    return inserted


def sync_connection(bind, connection_id: str) -> int:
    with Session(bind=bind) as db:
        connection = db.scalar(select(IntegrationConnection).where(
            IntegrationConnection.id == connection_id,
            IntegrationConnection.provider == "gmail",
            IntegrationConnection.status == "connected",
        ))
        if not connection:
            return 0
        try:
            return _sync_one(db, connection)
        except GmailReauthorizationRequired:
            db.rollback()
            connection = db.get(IntegrationConnection, connection_id)
            if connection:
                connection.status = "reauthorization_required"
                connection.last_error = "reauthorization_required"
                db.commit()
            return 0
        except GmailProviderError:
            db.rollback()
            connection = db.get(IntegrationConnection, connection_id)
            if connection:
                connection.last_error = "temporary_failure"
                db.commit()
            return 0
        except Exception:
            db.rollback()
            connection = db.get(IntegrationConnection, connection_id)
            if connection:
                connection.last_error = "temporary_failure"
                db.commit()
            logger.warning("gmail_sync_failed", extra={"event": "gmail_sync_failed"})
            return 0


def run_sync_tick() -> int:
    with SessionLocal() as db:
        connection_ids = db.scalars(select(IntegrationConnection.id).where(
            IntegrationConnection.provider == "gmail",
            IntegrationConnection.status == "connected",
        )).all()
    total = 0
    bind = engine
    for connection_id in connection_ids:
        with _connection_lock(bind, connection_id) as acquired:
            if acquired:
                total += sync_connection(bind, connection_id)
    return total


async def _loop() -> None:
    while True:
        try:
            await asyncio.to_thread(run_sync_tick)
            if notion_api.configured():
                await asyncio.to_thread(notion_sync.run_sync_tick)
            await asyncio.to_thread(run_slack_token_refresh_tick)
            await asyncio.to_thread(run_auto_triage_tick)
        except Exception:
            logger.warning("gmail_sync_tick_failed", extra={"event": "gmail_sync_failed"})
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def _notify_triage_human(db: Session, message: InboundMessage, reason: str) -> None:
    recipients = set(db.scalars(select(User.id).where(
        User.organization_id == message.organization_id,
        User.kind == "human", User.role == "admin", User.active.is_(True),
    )).all())
    owner = db.get(User, message.created_by) if message.created_by else None
    if owner and owner.organization_id == message.organization_id and owner.kind == "human" and owner.active:
        recipients.add(owner.id)
    for recipient_id in recipients:
        db.add(Notification(
            user_id=recipient_id, title="An incoming request needs human review",
            body=f"The CEO agent could not complete automatic review. {reason} Review the request in Requests.",
            href="requests",
        ))


def _triage_one(message_id: str) -> None:
    with _connection_lock(engine, f"triage:{message_id}") as acquired:
        if not acquired:
            return
        with SessionLocal() as db:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            message = db.scalar(select(InboundMessage).where(
                InboundMessage.id == message_id,
                InboundMessage.source.in_(["gmail", "slack", "notion"]),
            ).with_for_update())
            if not message or message.triage_status not in {"pending", "retry", "processing"}:
                return
            if message.triage_status != "pending" and message.triage_next_attempt_at and message.triage_next_attempt_at > now:
                return
            user = db.get(User, message.created_by) if message.created_by else None
            if not user or not user.active or user.kind != "human" or user.organization_id != message.organization_id:
                message.triage_status = "needs_human"
                message.triage_reason = "The mailbox owner is no longer an active workspace member."
                message.triage_next_attempt_at = None
                _notify_triage_human(db, message, message.triage_reason)
                log(db, message.organization_id, message.created_by, "inbound_message", message.id, "triage_needs_human", reason="owner_unavailable")
                db.commit()
                return
            from .copilot import CopilotService
            if CopilotService(db, user.organization_id).mode == "deterministic":
                message.triage_status = "needs_human"
                message.triage_reason = "Automatic CEO review needs an AI provider configured for this workspace."
                message.triage_next_attempt_at = None
                _notify_triage_human(db, message, message.triage_reason)
                log(db, message.organization_id, user.id, "inbound_message", message.id, "triage_needs_human", reason="ai_provider_unavailable")
                db.commit()
                return
            message.triage_status = "processing"
            message.triage_attempts = (message.triage_attempts or 0) + 1
            message.triage_next_attempt_at = now + timedelta(minutes=10)
            db.commit()
            try:
                result, provider_name = triage_inbound_message(db, user, message)
                message.triage_category = result["category"]
                message.triage_reason = result["reason"]
                message.triage_summary = result["summary"]
                message.triage_concerns = result["concerns"]
                message.triage_next_attempt_at = None
                if result["actionable"]:
                    created_plan = False
                    existing = db.scalar(select(InboundPlan).where(
                        InboundPlan.inbound_message_id == message.id,
                        InboundPlan.organization_id == message.organization_id,
                        InboundPlan.status == "pending",
                    ))
                    if not existing:
                        proposal = result["proposal"]
                        plan = InboundPlan(
                            organization_id=message.organization_id, inbound_message_id=message.id,
                            status="pending", summary=proposal["summary"], proposal=proposal,
                            planner_provider=provider_name, created_by=user.id,
                        )
                        db.add(plan)
                        db.flush()
                        created_plan = True
                        log(db, user.organization_id, user.id, "inbound_plan", plan.id, "auto_drafted", source_message_id=message.id)
                    if created_plan:
                        recipients = {user.id}
                        recipients.update(db.scalars(select(User.id).where(
                            User.organization_id == user.organization_id,
                            User.kind == "human", User.role == "admin", User.active.is_(True),
                        )).all())
                        for recipient_id in recipients:
                            db.add(Notification(
                                user_id=recipient_id, title="A request plan needs approval",
                                body="The CEO agent drafted a project plan from an incoming request. Review it in Requests.",
                                href="requests",
                            ))
                    message.triage_status = "actionable"
                else:
                    message.triage_status = "not_actionable"
                from .billing_service import BillingService
                BillingService(db).record_usage(user.organization_id, "ai_calls", 1)
                log(db, user.organization_id, user.id, "inbound_message", message.id, "auto_triaged", category=message.triage_category, actionable=result["actionable"])
                db.commit()
            except Exception as error:
                db.rollback()
                message = db.get(InboundMessage, message_id)
                if message:
                    message.triage_status = "failed" if message.triage_attempts >= 6 else "retry"
                    delay = min(3600, 30 * (2 ** min(message.triage_attempts - 1, 7)))
                    message.triage_next_attempt_at = None if message.triage_status == "failed" else datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=delay)
                    message.triage_reason = "Automatic review failed; retrying." if message.triage_status == "retry" else "Automatic review failed repeatedly; human review is needed."
                    if message.triage_status == "failed":
                        _notify_triage_human(db, message, message.triage_reason)
                        log(db, message.organization_id, message.created_by, "inbound_message", message.id, "triage_needs_human", reason="retry_limit_reached", attempts=message.triage_attempts)
                    db.commit()
                logger.warning("gmail_auto_triage_failed", extra={"event": "gmail_sync_failed"})


def run_auto_triage_tick(limit: int = 5) -> int:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with SessionLocal() as db:
        ids = db.scalars(select(InboundMessage.id).where(
            InboundMessage.source.in_(["gmail", "slack", "notion"]),
            InboundMessage.triage_status.in_(["pending", "retry", "processing"]),
            (InboundMessage.triage_next_attempt_at.is_(None) | (InboundMessage.triage_next_attempt_at <= now)),
        ).order_by(InboundMessage.received_at, InboundMessage.id).limit(limit)).all()
    for message_id in ids:
        _triage_one(message_id)
    return len(ids)


def run_slack_token_refresh_tick() -> int:
    refresh_before = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5)
    with SessionLocal() as db:
        ids = db.scalars(select(IntegrationConnection.id).where(
            IntegrationConnection.provider.in_(["slack", "slack_user"]),
            IntegrationConnection.status == "connected",
            IntegrationConnection.refresh_token_ciphertext.is_not(None),
            IntegrationConnection.access_token_expires_at <= refresh_before,
        )).all()
    refreshed = 0
    for connection_id in ids:
        with _connection_lock(engine, f"slack-refresh:{connection_id}") as acquired:
            if not acquired:
                continue
            with SessionLocal() as db:
                connection = db.scalar(select(IntegrationConnection).where(
                    IntegrationConnection.id == connection_id,
                    IntegrationConnection.provider.in_(["slack", "slack_user"]),
                    IntegrationConnection.status == "connected",
                ).with_for_update())
                if not connection:
                    continue
                try:
                    refresh_connection_access_token(connection)
                    connection.last_error = None
                    db.commit()
                    refreshed += 1
                except SlackReauthorizationRequired:
                    db.rollback()
                    connection = db.get(IntegrationConnection, connection_id)
                    if connection:
                        connection.status = "reauthorization_required"
                        connection.last_error = "reauthorization_required"
                        db.commit()
                except SlackProviderError:
                    db.rollback()
                    connection = db.get(IntegrationConnection, connection_id)
                    if connection:
                        connection.last_error = "temporary_failure"
                        db.commit()
    return refreshed


def start_gmail_sync_loop() -> None:
    global _background_task
    from .gmail_api import oauth_configured
    from .slack_api import fully_configured as slack_configured
    if (oauth_configured() or slack_configured() or notion_api.configured()) and _background_task is None:
        _background_task = asyncio.ensure_future(_loop())


async def stop_gmail_sync_loop() -> None:
    global _background_task
    task = _background_task
    if task is not None and task.get_loop() is not asyncio.get_running_loop():
        return
    _background_task = None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
