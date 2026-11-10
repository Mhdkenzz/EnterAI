"""User-consented workspace integrations. Gmail is the first provider adapter."""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import secrets_store
from .auth import current_user
from .database import get_db
from .gmail_api import (
    GOOGLE_REVOKE_URL, GMAIL_SCOPE, GmailProviderError, authorization_url,
    exchange_code, get_profile, oauth_configured, pkce_challenge,
)
from . import slack_api
from . import notion_api
from .models import InboundMessage, IntegrationConnection, IntegrationOAuthState, Notification, User
from .services import log

router = APIRouter(prefix="/api/integrations", tags=["integrations"])
OAUTH_STATE_TTL = timedelta(minutes=10)
_APP_BASE_URL = lambda: os.getenv("APP_BASE_URL", "http://localhost:3000").rstrip("/")


def _human(user: User) -> None:
    if user.kind != "human" or not user.active:
        raise HTTPException(403, "An active human account is required")


def integration_out(connection: IntegrationConnection, db: Session, viewer: User) -> dict:
    owner = db.get(User, connection.connected_by)
    return {
        "id": connection.id, "provider": connection.provider,
        "account": connection.external_account_id, "display_name": connection.display_name,
        "status": connection.status, "scopes": connection.granted_scopes,
        "connected_by": owner.name if owner and owner.organization_id == connection.organization_id else None,
        "can_manage": connection.connected_by == viewer.id or viewer.role == "admin",
        "last_sync_at": connection.last_sync_at, "last_error": connection.last_error,
        "created_at": connection.created_at,
    }


@router.get("")
def list_integrations(user: User = Depends(current_user), db: Session = Depends(get_db)):
    _human(user)
    rows = db.scalars(select(IntegrationConnection).where(
        IntegrationConnection.organization_id == user.organization_id,
    ).order_by(IntegrationConnection.provider, IntegrationConnection.external_account_id)).all()
    return {"gmail_available": oauth_configured(), "slack_available": slack_api.fully_configured(), "notion_available": notion_api.configured(), "items": [integration_out(row, db, user) for row in rows]}


@router.get("/gmail/connect")
def connect_gmail(user: User = Depends(current_user), db: Session = Depends(get_db)):
    _human(user)
    if user.role != "admin":
        raise HTTPException(403, "A workspace admin must connect a shared Gmail mailbox")
    if not oauth_configured():
        raise HTTPException(503, "Gmail integration is not configured")
    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(delete(IntegrationOAuthState).where(IntegrationOAuthState.expires_at < now))
    db.add(IntegrationOAuthState(
        state_hash=hashlib.sha256(state.encode()).hexdigest(), provider="gmail",
        organization_id=user.organization_id, user_id=user.id,
        code_verifier_ciphertext=secrets_store.encrypt(code_verifier),
        expires_at=now + OAUTH_STATE_TTL,
    ))
    db.commit()
    try:
        target = authorization_url(state, pkce_challenge(code_verifier))
    except GmailProviderError as error:
        raise HTTPException(503, str(error)) from error
    return RedirectResponse(target, status_code=302)


def _oauth_result(ok: bool) -> RedirectResponse:
    result = "connected" if ok else "error"
    return RedirectResponse(f"{_APP_BASE_URL()}/integrations?gmail={result}", status_code=302)


def _slack_oauth_result(ok: bool, *, workspace_in_use: bool = False) -> RedirectResponse:
    result = "connected" if ok else "workspace-in-use" if workspace_in_use else "error"
    return RedirectResponse(f"{_APP_BASE_URL()}/integrations?slack={result}", status_code=302)


def _notion_oauth_result(ok: bool) -> RedirectResponse:
    result = "connected" if ok else "error"
    return RedirectResponse(f"{_APP_BASE_URL()}/integrations?notion={result}", status_code=302)


@router.get("/notion/connect")
def connect_notion(user: User = Depends(current_user), db: Session = Depends(get_db)):
    _human(user)
    if user.role != "admin":
        raise HTTPException(403, "A workspace admin must connect a shared Notion workspace")
    if not notion_api.configured():
        raise HTTPException(503, "Notion integration is not configured")
    state = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(delete(IntegrationOAuthState).where(IntegrationOAuthState.expires_at < now))
    db.add(IntegrationOAuthState(
        state_hash=hashlib.sha256(state.encode()).hexdigest(), provider="notion",
        organization_id=user.organization_id, user_id=user.id,
        code_verifier_ciphertext=secrets_store.encrypt(secrets.token_urlsafe(32)),
        expires_at=now + OAUTH_STATE_TTL,
    ))
    db.commit()
    try:
        return RedirectResponse(notion_api.authorization_url(state), status_code=302)
    except notion_api.NotionProviderError as error:
        raise HTTPException(503, str(error)) from error


@router.get("/notion/callback")
def notion_callback(
    state: str = Query(min_length=20, max_length=200),
    code: str | None = Query(default=None, min_length=1, max_length=4096),
    error: str | None = Query(default=None, max_length=200),
    db: Session = Depends(get_db),
):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row = db.scalar(select(IntegrationOAuthState).where(
        IntegrationOAuthState.state_hash == hashlib.sha256(state.encode()).hexdigest(),
        IntegrationOAuthState.provider == "notion",
        IntegrationOAuthState.expires_at >= now,
        IntegrationOAuthState.consumed_at.is_(None),
    ))
    if not row:
        return _notion_oauth_result(False)
    consumed = db.execute(update(IntegrationOAuthState).where(
        IntegrationOAuthState.id == row.id,
        IntegrationOAuthState.consumed_at.is_(None),
        IntegrationOAuthState.expires_at >= now,
    ).values(consumed_at=now))
    if consumed.rowcount != 1:
        db.rollback()
        return _notion_oauth_result(False)
    user = db.get(User, row.user_id)
    organization_id = row.organization_id
    if not user or user.organization_id != organization_id or user.kind != "human" or not user.active:
        db.commit()
        return _notion_oauth_result(False)
    db.commit()
    if error or not code:
        return _notion_oauth_result(False)
    try:
        token = notion_api.exchange_code(code)
        workspace_id = token["workspace_id"]
        owner = token.get("owner") or {}
        owner_user = owner.get("user") if isinstance(owner, dict) else None
        display_name = token.get("workspace_name") or workspace_id
        expires_in = token.get("expires_in", 3600)
        expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=expires_in) if isinstance(expires_in, int) and expires_in >= 60 else None
        existing = db.scalar(select(IntegrationConnection).where(
            IntegrationConnection.organization_id == organization_id,
            IntegrationConnection.provider == "notion",
            IntegrationConnection.external_account_id == workspace_id,
        ).with_for_update())
        refresh_token = token.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            refresh_token = secrets_store.decrypt(existing.refresh_token_ciphertext) if existing and existing.refresh_token_ciphertext else None
        if not refresh_token:
            return _notion_oauth_result(False)
        if existing:
            existing.display_name = display_name
            existing.status = "connected"
            existing.granted_scopes = ["read_content"]
            existing.access_token_ciphertext = secrets_store.encrypt(token["access_token"])
            existing.refresh_token_ciphertext = secrets_store.encrypt(refresh_token) if refresh_token else None
            existing.access_token_expires_at = expiry
            existing.history_cursor = datetime.now(timezone.utc).isoformat()
            existing.connected_by = user.id
            existing.last_error = None
        else:
            existing = IntegrationConnection(
                organization_id=organization_id, provider="notion", external_account_id=workspace_id,
                display_name=display_name, status="connected", granted_scopes=["read_content"],
                access_token_ciphertext=secrets_store.encrypt(token["access_token"]),
                refresh_token_ciphertext=secrets_store.encrypt(refresh_token) if refresh_token else None,
                access_token_expires_at=expiry, history_cursor=datetime.now(timezone.utc).isoformat(),
                connected_by=user.id,
            )
            db.add(existing)
        db.flush()
        log(db, organization_id, user.id, "integration", existing.id, "connected", provider="notion", owner_id=owner_user.get("id") if isinstance(owner_user, dict) else None)
        db.commit()
        return _notion_oauth_result(True)
    except (notion_api.NotionProviderError, ValueError, RuntimeError):
        db.rollback()
        return _notion_oauth_result(False)


@router.get("/slack/connect")
def connect_slack(user: User = Depends(current_user), db: Session = Depends(get_db)):
    _human(user)
    if user.role != "admin":
        raise HTTPException(403, "A workspace admin must connect a shared Slack workspace")
    if not slack_api.fully_configured():
        raise HTTPException(503, "Slack OAuth and event signing are not configured")
    state = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(delete(IntegrationOAuthState).where(IntegrationOAuthState.expires_at < now))
    db.add(IntegrationOAuthState(
        state_hash=hashlib.sha256(state.encode()).hexdigest(), provider="slack",
        organization_id=user.organization_id, user_id=user.id,
        code_verifier_ciphertext=secrets_store.encrypt(secrets.token_urlsafe(32)),
        expires_at=now + OAUTH_STATE_TTL,
    ))
    db.commit()
    try:
        return RedirectResponse(slack_api.authorization_url(state), status_code=302)
    except slack_api.SlackProviderError as error:
        raise HTTPException(503, str(error)) from error


@router.get("/slack/user/connect")
def connect_slack_user(user: User = Depends(current_user), db: Session = Depends(get_db)):
    _human(user)
    if not slack_api.fully_configured():
        raise HTTPException(503, "Slack OAuth and event signing are not configured")
    state = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(delete(IntegrationOAuthState).where(IntegrationOAuthState.expires_at < now))
    db.add(IntegrationOAuthState(
        state_hash=hashlib.sha256(state.encode()).hexdigest(), provider="slack_user",
        organization_id=user.organization_id, user_id=user.id,
        code_verifier_ciphertext=secrets_store.encrypt(secrets.token_urlsafe(32)),
        expires_at=now + OAUTH_STATE_TTL,
    ))
    db.commit()
    try:
        return RedirectResponse(slack_api.authorization_url(state, user_scopes=True), status_code=302)
    except slack_api.SlackProviderError as error:
        raise HTTPException(503, str(error)) from error


@router.get("/slack/callback")
def slack_callback(
    state: str = Query(min_length=20, max_length=200),
    code: str | None = Query(default=None, min_length=1, max_length=4096),
    error: str | None = Query(default=None, max_length=200),
    db: Session = Depends(get_db),
):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row = db.scalar(select(IntegrationOAuthState).where(
        IntegrationOAuthState.state_hash == hashlib.sha256(state.encode()).hexdigest(),
        IntegrationOAuthState.provider == "slack",
        IntegrationOAuthState.expires_at >= now,
        IntegrationOAuthState.consumed_at.is_(None),
    ))
    if not row:
        return _slack_oauth_result(False)
    consumed = db.execute(update(IntegrationOAuthState).where(
        IntegrationOAuthState.id == row.id,
        IntegrationOAuthState.consumed_at.is_(None),
        IntegrationOAuthState.expires_at >= now,
    ).values(consumed_at=now))
    if consumed.rowcount != 1:
        db.rollback()
        return _slack_oauth_result(False)
    user = db.get(User, row.user_id)
    organization_id = row.organization_id
    if not user or user.organization_id != organization_id or user.kind != "human" or not user.active:
        db.commit()
        return _slack_oauth_result(False)
    db.commit()
    if error or not code:
        return _slack_oauth_result(False)
    try:
        token = slack_api.exchange_code(code)
        if not isinstance(token.get("access_token"), str):
            return _slack_oauth_result(False)
        team = token.get("team") or {}
        team_id = team.get("id") if isinstance(team, dict) else None
        team_name = team.get("name") if isinstance(team, dict) else None
        scopes = token.get("scope", "")
        granted_scopes = scopes.split(",") if isinstance(scopes, str) else []
        required_scopes = set(slack_api.SLACK_BOT_SCOPES.split(","))
        if not isinstance(team_id, str) or not team_id or not required_scopes.issubset(set(granted_scopes)):
            return _slack_oauth_result(False)
        owned_elsewhere = db.scalar(select(IntegrationConnection.id).where(
            IntegrationConnection.provider == "slack",
            IntegrationConnection.external_account_id == team_id,
            IntegrationConnection.status == "connected",
            IntegrationConnection.organization_id != organization_id,
        ).limit(1))
        if owned_elsewhere:
            return _slack_oauth_result(False, workspace_in_use=True)
        existing = db.scalar(select(IntegrationConnection).where(
            IntegrationConnection.organization_id == organization_id,
            IntegrationConnection.provider == "slack",
            IntegrationConnection.external_account_id == team_id,
        ).with_for_update())
        refresh_token = token.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            refresh_token = None
            if existing and existing.refresh_token_ciphertext:
                refresh_token = secrets_store.decrypt(existing.refresh_token_ciphertext)
        expires_in = token.get("expires_in")
        expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=expires_in) if isinstance(expires_in, int) and expires_in >= 60 else None
        if existing:
            existing.display_name = team_name or team_id
            existing.status = "connected"
            existing.granted_scopes = granted_scopes
            existing.access_token_ciphertext = secrets_store.encrypt(token["access_token"])
            existing.refresh_token_ciphertext = secrets_store.encrypt(refresh_token) if refresh_token else None
            existing.access_token_expires_at = expiry
            existing.connected_by = user.id
            existing.last_error = None
        else:
            existing = IntegrationConnection(
                organization_id=organization_id, provider="slack", external_account_id=team_id,
                display_name=team_name or team_id, status="connected", granted_scopes=granted_scopes,
                access_token_ciphertext=secrets_store.encrypt(token["access_token"]),
                refresh_token_ciphertext=secrets_store.encrypt(refresh_token) if refresh_token else None,
                access_token_expires_at=expiry, connected_by=user.id,
            )
            db.add(existing)
        db.flush()
        log(db, organization_id, user.id, "integration", existing.id, "connected", provider="slack")
        db.commit()
        return _slack_oauth_result(True)
    except IntegrityError:
        db.rollback()
        return _slack_oauth_result(False, workspace_in_use=True)
    except (slack_api.SlackProviderError, ValueError, RuntimeError):
        db.rollback()
        return _slack_oauth_result(False)


@router.get("/slack/user/callback")
def slack_user_callback(
    state: str = Query(min_length=20, max_length=200),
    code: str | None = Query(default=None, min_length=1, max_length=4096),
    error: str | None = Query(default=None, max_length=200),
    db: Session = Depends(get_db),
):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row = db.scalar(select(IntegrationOAuthState).where(
        IntegrationOAuthState.state_hash == hashlib.sha256(state.encode()).hexdigest(),
        IntegrationOAuthState.provider == "slack_user",
        IntegrationOAuthState.expires_at >= now,
        IntegrationOAuthState.consumed_at.is_(None),
    ))
    if not row:
        return _slack_oauth_result(False)
    consumed = db.execute(update(IntegrationOAuthState).where(
        IntegrationOAuthState.id == row.id,
        IntegrationOAuthState.consumed_at.is_(None),
        IntegrationOAuthState.expires_at >= now,
    ).values(consumed_at=now))
    if consumed.rowcount != 1:
        db.rollback()
        return _slack_oauth_result(False)
    user = db.get(User, row.user_id)
    organization_id = row.organization_id
    if not user or user.organization_id != organization_id or user.kind != "human" or not user.active:
        db.commit()
        return _slack_oauth_result(False)
    db.commit()
    if error or not code:
        return _slack_oauth_result(False)
    try:
        token = slack_api.exchange_code(code, user_scopes=True)
        team = token.get("team") or {}
        team_id = team.get("id") if isinstance(team, dict) else None
        authed_user = token.get("authed_user") or {}
        slack_user_id = authed_user.get("id") if isinstance(authed_user, dict) else None
        access_token = authed_user.get("access_token") if isinstance(authed_user, dict) else None
        scopes = authed_user.get("scope", "") if isinstance(authed_user, dict) else ""
        granted_scopes = scopes.split(",") if isinstance(scopes, str) else []
        if (not isinstance(team_id, str) or not team_id or not isinstance(slack_user_id, str)
                or not slack_user_id or not isinstance(access_token, str) or not access_token
                or not set(slack_api.SLACK_USER_SCOPES.split(",")).issubset(set(granted_scopes))):
            return _slack_oauth_result(False)
        account_id = f"{team_id}:{slack_user_id}"
        existing = db.scalar(select(IntegrationConnection).where(
            IntegrationConnection.organization_id == organization_id,
            IntegrationConnection.provider == "slack_user",
            IntegrationConnection.external_account_id == account_id,
        ).with_for_update())
        refresh_token = authed_user.get("refresh_token") if isinstance(authed_user, dict) else None
        if not isinstance(refresh_token, str) or not refresh_token:
            refresh_token = None
            if existing and existing.refresh_token_ciphertext:
                refresh_token = secrets_store.decrypt(existing.refresh_token_ciphertext)
        expires_in = authed_user.get("expires_in") if isinstance(authed_user, dict) else None
        expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=expires_in) if isinstance(expires_in, int) and expires_in >= 60 else None
        if existing:
            existing.display_name = "Your Slack direct messages"
            existing.status = "connected"
            existing.granted_scopes = granted_scopes
            existing.access_token_ciphertext = secrets_store.encrypt(access_token)
            existing.refresh_token_ciphertext = secrets_store.encrypt(refresh_token) if refresh_token else None
            existing.access_token_expires_at = expiry
            existing.connected_by = user.id
            existing.last_error = None
        else:
            existing = IntegrationConnection(
                organization_id=organization_id, provider="slack_user", external_account_id=account_id,
                display_name="Your Slack direct messages", status="connected",
                granted_scopes=granted_scopes, access_token_ciphertext=secrets_store.encrypt(access_token),
                refresh_token_ciphertext=secrets_store.encrypt(refresh_token) if refresh_token else None,
                access_token_expires_at=expiry, connected_by=user.id,
            )
            db.add(existing)
        db.flush()
        log(db, organization_id, user.id, "integration", existing.id, "connected", provider="slack_user")
        db.commit()
        return _slack_oauth_result(True)
    except (slack_api.SlackProviderError, ValueError, RuntimeError):
        db.rollback()
        return _slack_oauth_result(False)


@router.post("/slack/events")
async def slack_events(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    if len(body) > 1_000_000:
        raise HTTPException(413, "Slack event payload is too large")
    if not slack_api.verify_request(
        request.headers.get("x-slack-request-timestamp"),
        request.headers.get("x-slack-signature"), body,
    ):
        raise HTTPException(401, "Invalid Slack request signature")
    try:
        payload = slack_api.parse_payload(body)
    except slack_api.SlackProviderError as error:
        raise HTTPException(400, str(error)) from error
    if payload.get("type") == "url_verification":
        challenge = payload.get("challenge")
        if not isinstance(challenge, str):
            raise HTTPException(400, "Slack verification challenge is missing")
        return JSONResponse({"challenge": challenge})
    if payload.get("type") != "event_callback":
        return {"ok": True}
    team_id = payload.get("team_id")
    event = payload.get("event") or {}
    if not isinstance(team_id, str) or not isinstance(event, dict):
        return {"ok": True}
    connections = db.scalars(select(IntegrationConnection).where(
        or_(
            (IntegrationConnection.provider == "slack") & (IntegrationConnection.external_account_id == team_id),
            (IntegrationConnection.provider == "slack_user") & (IntegrationConnection.external_account_id.like(f"{team_id}:%")),
        ),
        IntegrationConnection.status == "connected",
    )).all()
    bot_connections = [connection for connection in connections
                       if connection.provider == "slack" and connection.external_account_id == team_id]
    user_connections = [connection for connection in connections
                        if connection.provider == "slack_user" and connection.external_account_id.startswith(f"{team_id}:")]
    if event.get("type") == "app_uninstalled":
        for connection in bot_connections + user_connections:
            connection.status = "disconnected"
            connection.access_token_ciphertext = None
            connection.refresh_token_ciphertext = None
            connection.access_token_expires_at = None
            connection.last_error = None
            log(db, connection.organization_id, connection.connected_by, "integration", connection.id, "disconnected", provider="slack", reason="app_uninstalled")
        db.commit()
        return {"ok": True}
    if event.get("type") == "message" and len({connection.organization_id for connection in bot_connections}) > 1:
        # Older installations may predate the global active-workspace constraint.
        # Never fan one signed Slack event into multiple Enter AI tenants.
        for connection in bot_connections:
            if connection.last_error != "workspace_conflict":
                connection.last_error = "workspace_conflict"
                reason = "Slack intake is paused because this Slack workspace is connected to more than one Enter AI organization. Disconnect the duplicate connection to resume safely."
                for admin in db.scalars(select(User).where(
                    User.organization_id == connection.organization_id,
                    User.kind == "human", User.role == "admin", User.active.is_(True),
                )).all():
                    db.add(Notification(
                        user_id=admin.id, title="Slack intake paused: workspace conflict",
                        body=reason,
                    ))
                log(db, connection.organization_id, connection.connected_by, "integration", connection.id,
                    "intake_paused", provider="slack", reason="workspace_conflict")
        db.commit()
        bot_connections = []
    if event.get("type") != "message" or event.get("subtype") or event.get("bot_id"):
        return {"ok": True}
    channel = event.get("channel")
    message_ts = event.get("ts")
    sender = event.get("user")
    text = event.get("text")
    if not all(isinstance(value, str) and value for value in (channel, message_ts, sender)):
        return {"ok": True}
    try:
        received_at = datetime.fromtimestamp(float(message_ts), timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError, OverflowError):
        return {"ok": True}
    if not isinstance(text, str):
        text = "(This Slack message did not contain plain text.)"
    text = text[:50000]
    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        event_id = f"{channel}:{message_ts}"
    channel_type = event.get("channel_type")
    authorized_user_ids = {
        item.get("user_id") for item in payload.get("authorizations", [])
        if isinstance(item, dict) and item.get("is_bot") is not True and isinstance(item.get("user_id"), str)
    }
    if not authorized_user_ids:
        authorized_user_ids = {value for value in payload.get("authed_users", []) if isinstance(value, str)}
    user_connections = [connection for connection in user_connections
                        if connection.external_account_id.partition(":")[2] in authorized_user_ids]
    connections = bot_connections + (user_connections if channel_type in {"im", "mpim"} else [])
    channel_name = event.get("channel_name") or channel
    subject = "Direct message" if channel_type == "im" else "Group direct message" if channel_type == "mpim" else f"#{channel_name}"
    source_url = f"https://app.slack.com/archives/{quote(channel, safe='')}/p{message_ts.replace('.', '')}"
    thread_ts = event.get("thread_ts")
    if isinstance(thread_ts, str):
        source_url += f"?thread_ts={thread_ts}&cid={channel}"
    for connection in connections:
        message = InboundMessage(
            organization_id=connection.organization_id, source="slack",
            external_id=f"{team_id}:{event_id}", thread_id=thread_ts,
            sender=sender, subject=subject, body=text, source_url=source_url,
            received_at=received_at, status="new",
            triage_status="pending", created_by=connection.connected_by,
        )
        try:
            with db.begin_nested():
                db.add(message)
                db.flush()
            connection.last_sync_at = datetime.now(timezone.utc).replace(tzinfo=None)
            connection.last_error = None
            log(db, connection.organization_id, connection.connected_by, "inbound_message", message.id, "received", source="slack")
        except IntegrityError:
            # The org/source/external ID unique constraint makes Slack retries and
            # duplicate workspace installs safe to acknowledge without reimporting.
            continue
    db.commit()
    return {"ok": True}


@router.get("/gmail/callback")
def gmail_callback(
    state: str = Query(min_length=20, max_length=200),
    code: str | None = Query(default=None, min_length=1, max_length=4096),
    error: str | None = Query(default=None, max_length=200),
    db: Session = Depends(get_db),
):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    row = db.scalar(select(IntegrationOAuthState).where(
        IntegrationOAuthState.state_hash == state_hash,
        IntegrationOAuthState.provider == "gmail",
        IntegrationOAuthState.expires_at >= now,
        IntegrationOAuthState.consumed_at.is_(None),
    ))
    if not row:
        return _oauth_result(False)
    consumed = db.execute(update(IntegrationOAuthState).where(
        IntegrationOAuthState.id == row.id,
        IntegrationOAuthState.consumed_at.is_(None),
        IntegrationOAuthState.expires_at >= now,
    ).values(consumed_at=now))
    if consumed.rowcount != 1:
        db.rollback()
        return _oauth_result(False)
    user = db.get(User, row.user_id)
    organization_id = row.organization_id
    try:
        code_verifier = secrets_store.decrypt(row.code_verifier_ciphertext)
    except (ValueError, RuntimeError):
        db.commit()
        return _oauth_result(False)
    if not user or user.organization_id != organization_id or user.kind != "human" or not user.active:
        db.commit()
        return _oauth_result(False)
    db.commit()  # consume state before any external request; callbacks are single-use
    if error or not code:
        return _oauth_result(False)
    try:
        token = exchange_code(code, code_verifier)
        scopes = token.get("scope", "")
        granted_scopes = scopes.split() if isinstance(scopes, str) and scopes.strip() else [GMAIL_SCOPE]
        if GMAIL_SCOPE not in granted_scopes:
            return _oauth_result(False)
        profile = get_profile(token["access_token"])
        account = profile["emailAddress"].strip().lower()
        existing = db.scalar(select(IntegrationConnection).where(
            IntegrationConnection.organization_id == organization_id,
            IntegrationConnection.provider == "gmail",
            IntegrationConnection.external_account_id == account,
        ).with_for_update())
        refresh_token = token.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            if existing and existing.refresh_token_ciphertext:
                refresh_token = secrets_store.decrypt(existing.refresh_token_ciphertext)
            else:
                return _oauth_result(False)
        access_token = token["access_token"]
        expires_in = token.get("expires_in", 3600)
        if not isinstance(expires_in, int) or expires_in < 60:
            expires_in = 3600
        expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=expires_in)
        if existing:
            existing.display_name = account
            existing.status = "connected"
            existing.granted_scopes = granted_scopes
            existing.access_token_ciphertext = secrets_store.encrypt(access_token)
            existing.refresh_token_ciphertext = secrets_store.encrypt(refresh_token)
            existing.access_token_expires_at = expiry
            existing.history_cursor = existing.history_cursor or profile["historyId"]
            existing.connected_by = user.id
            existing.last_error = None
        else:
            existing = IntegrationConnection(
                organization_id=organization_id, provider="gmail", external_account_id=account,
                display_name=account, status="connected", granted_scopes=granted_scopes,
                access_token_ciphertext=secrets_store.encrypt(access_token),
                refresh_token_ciphertext=secrets_store.encrypt(refresh_token),
                access_token_expires_at=expiry, history_cursor=profile["historyId"],
                connected_by=user.id,
            )
            db.add(existing)
        db.flush()
        log(db, organization_id, user.id, "integration", existing.id, "connected", provider="gmail")
        db.commit()
        return _oauth_result(True)
    except (GmailProviderError, ValueError, RuntimeError):
        db.rollback()
        return _oauth_result(False)


@router.post("/{connection_id}/disconnect")
def disconnect_integration(
    connection_id: str, user: User = Depends(current_user), db: Session = Depends(get_db),
):
    _human(user)
    connection = db.scalar(select(IntegrationConnection).where(
        IntegrationConnection.id == connection_id,
        IntegrationConnection.organization_id == user.organization_id,
    ).with_for_update())
    if not connection:
        raise HTTPException(404, "Integration not found")
    if connection.connected_by != user.id and user.role != "admin":
        raise HTTPException(403, "Only the person who connected this account or a workspace admin can disconnect it")
    if connection.provider == "gmail" and connection.refresh_token_ciphertext:
        try:
            token = secrets_store.decrypt(connection.refresh_token_ciphertext)
            httpx.post(GOOGLE_REVOKE_URL, data={"token": token}, timeout=10, follow_redirects=False)
        except (httpx.HTTPError, ValueError, RuntimeError):
            pass  # local access is revoked even if Google's revocation endpoint is unavailable
    elif connection.provider in {"slack", "slack_user"} and connection.access_token_ciphertext:
        try:
            token = secrets_store.decrypt(connection.access_token_ciphertext)
            if connection.access_token_expires_at and connection.access_token_expires_at <= datetime.now(timezone.utc).replace(tzinfo=None):
                token, _ = slack_api.refresh_connection_access_token(connection)
            httpx.post(slack_api.SLACK_REVOKE_URL, headers={"Authorization": f"Bearer {token}"}, timeout=10, follow_redirects=False)
        except (httpx.HTTPError, ValueError, RuntimeError, slack_api.SlackProviderError):
            pass  # workspace admins can also remove the app directly in Slack
    elif connection.provider == "notion" and connection.access_token_ciphertext:
        try:
            notion_api.revoke_token(secrets_store.decrypt(connection.access_token_ciphertext))
        except (notion_api.NotionProviderError, ValueError, RuntimeError):
            pass  # local tokens are still cleared; the admin can revoke in Notion settings
    connection.status = "disconnected"
    connection.access_token_ciphertext = None
    connection.refresh_token_ciphertext = None
    connection.access_token_expires_at = None
    connection.history_cursor = None
    connection.last_error = None
    log(db, user.organization_id, user.id, "integration", connection.id, "disconnected", provider=connection.provider)
    db.commit()
    return {"ok": True}
