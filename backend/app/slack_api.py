"""Slack OAuth and Events API request verification helpers."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from . import secrets_store
from .models import IntegrationConnection

SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
SLACK_ACCESS_URL = "https://slack.com/api/oauth.v2.access"
SLACK_REVOKE_URL = "https://slack.com/api/auth.revoke"
SLACK_BOT_SCOPES = (
    "channels:history,groups:history,im:history,mpim:history"
)
SLACK_USER_SCOPES = "im:history,mpim:history"
SLACK_REDIRECT_PATH = "/api/integrations/slack/callback"
SLACK_USER_REDIRECT_PATH = "/api/integrations/slack/user/callback"
SLACK_EVENT_PATH = "/api/integrations/slack/events"
HTTP_TIMEOUT_SECONDS = 15.0


class SlackProviderError(RuntimeError):
    pass


class SlackReauthorizationRequired(SlackProviderError):
    pass


def oauth_configured() -> bool:
    return bool(os.getenv("SLACK_CLIENT_ID", "").strip() and os.getenv("SLACK_CLIENT_SECRET", "").strip())


def events_configured() -> bool:
    return bool(os.getenv("SLACK_SIGNING_SECRET", "").strip())


def fully_configured() -> bool:
    return oauth_configured() and events_configured()


def redirect_uri(*, user_scopes: bool = False) -> str:
    path = SLACK_USER_REDIRECT_PATH if user_scopes else SLACK_REDIRECT_PATH
    return os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/") + path


def authorization_url(state: str, *, user_scopes: bool = False) -> str:
    if not oauth_configured():
        raise SlackProviderError("Slack OAuth is not configured")
    params = {
        "client_id": os.environ["SLACK_CLIENT_ID"].strip(),
        "redirect_uri": redirect_uri(user_scopes=user_scopes),
        "state": state,
    }
    if user_scopes:
        params["user_scope"] = SLACK_USER_SCOPES
    else:
        params["scope"] = SLACK_BOT_SCOPES
    return SLACK_AUTHORIZE_URL + "?" + urlencode(params)


def exchange_code(code: str, *, user_scopes: bool = False) -> dict:
    try:
        response = httpx.post(SLACK_ACCESS_URL, data={
            "client_id": os.getenv("SLACK_CLIENT_ID", "").strip(),
            "client_secret": os.getenv("SLACK_CLIENT_SECRET", "").strip(),
            "code": code,
            "redirect_uri": redirect_uri(user_scopes=user_scopes),
        }, timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False)
    except httpx.HTTPError as error:
        raise SlackProviderError("Slack OAuth could not be reached") from error
    try:
        result = response.json()
    except ValueError as error:
        raise SlackProviderError("Slack OAuth returned an invalid response") from error
    if response.status_code >= 400 or not isinstance(result, dict) or result.get("ok") is not True:
        raise SlackProviderError("Slack rejected the installation request")
    authed_user = result.get("authed_user") or {}
    if not isinstance(result.get("access_token"), str) and not (
        isinstance(authed_user, dict) and isinstance(authed_user.get("access_token"), str)
    ):
        raise SlackProviderError("Slack did not return an access token")
    return result


def refresh_connection_access_token(connection: IntegrationConnection) -> tuple[str, datetime]:
    if not connection.refresh_token_ciphertext:
        raise SlackReauthorizationRequired("Slack installation needs authorization")
    try:
        refresh_token = secrets_store.decrypt(connection.refresh_token_ciphertext)
    except (ValueError, RuntimeError) as error:
        raise SlackReauthorizationRequired("Slack installation needs authorization") from error
    try:
        response = httpx.post(SLACK_ACCESS_URL, data={
            "client_id": os.getenv("SLACK_CLIENT_ID", "").strip(),
            "client_secret": os.getenv("SLACK_CLIENT_SECRET", "").strip(),
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }, timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False)
    except httpx.HTTPError as error:
        raise SlackProviderError("Slack token refresh could not be reached") from error
    try:
        result = response.json()
    except ValueError as error:
        raise SlackProviderError("Slack token refresh returned an invalid response") from error
    if response.status_code >= 400 or not isinstance(result, dict) or result.get("ok") is not True:
        if isinstance(result, dict) and result.get("error") == "invalid_refresh_token":
            raise SlackReauthorizationRequired("Slack installation needs authorization")
        raise SlackProviderError("Slack token refresh was rejected")
    token_result = result
    if connection.provider == "slack_user":
        authed_user = result.get("authed_user")
        token_result = authed_user if isinstance(authed_user, dict) else result
    access_token = token_result.get("access_token")
    next_refresh = token_result.get("refresh_token")
    expires_in = token_result.get("expires_in", result.get("expires_in", 43200))
    if not isinstance(access_token, str) or not isinstance(next_refresh, str) or not isinstance(expires_in, int) or expires_in < 60:
        raise SlackProviderError("Slack token refresh returned incomplete credentials")
    expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=expires_in)
    connection.access_token_ciphertext = secrets_store.encrypt(access_token)
    connection.refresh_token_ciphertext = secrets_store.encrypt(next_refresh)
    connection.access_token_expires_at = expiry
    return access_token, expiry


def verify_request(timestamp: str | None, signature: str | None, body: bytes) -> bool:
    if not timestamp or not signature or not events_configured():
        return False
    try:
        request_time = int(timestamp)
        timestamp_bytes = timestamp.encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError):
        return False
    if abs(time.time() - request_time) > 60 * 5:
        return False
    base = b"v0:" + timestamp_bytes + b":" + body
    digest = hmac.new(os.environ["SLACK_SIGNING_SECRET"].strip().encode(), base, hashlib.sha256).hexdigest()
    expected = "v0=" + digest
    return hmac.compare_digest(expected, signature)


def parse_payload(body: bytes) -> dict:
    try:
        result = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SlackProviderError("Slack sent an invalid event payload") from error
    if not isinstance(result, dict):
        raise SlackProviderError("Slack sent an invalid event payload")
    return result
