"""Small Gmail OAuth and REST client using fixed Google endpoints."""
from __future__ import annotations

import os
import base64
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from . import secrets_store
from .models import IntegrationConnection

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
HTTP_TIMEOUT_SECONDS = 15.0


class GmailProviderError(RuntimeError):
    pass


class GmailReauthorizationRequired(GmailProviderError):
    pass


class GmailHistoryExpired(GmailProviderError):
    pass


class GmailMessageUnavailable(GmailProviderError):
    pass


def oauth_configured() -> bool:
    return bool(os.getenv("GMAIL_OAUTH_CLIENT_ID", "").strip() and os.getenv("GMAIL_OAUTH_CLIENT_SECRET", "").strip())


def redirect_uri() -> str:
    return os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/") + "/api/integrations/gmail/callback"


def authorization_url(state: str, code_challenge: str) -> str:
    if not oauth_configured():
        raise GmailProviderError("Gmail connection is not configured")
    values = {
        "client_id": os.environ["GMAIL_OAUTH_CLIENT_ID"].strip(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": GMAIL_SCOPE,
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return GOOGLE_AUTH_URL + "?" + urlencode(values)


def _token_post(data: dict[str, str]) -> dict:
    values = {
        **data,
        "client_id": os.getenv("GMAIL_OAUTH_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("GMAIL_OAUTH_CLIENT_SECRET", "").strip(),
    }
    try:
        response = httpx.post(GOOGLE_TOKEN_URL, data=values, timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False)
    except httpx.HTTPError as error:
        raise GmailProviderError("Google OAuth could not be reached") from error
    if response.status_code >= 400:
        raise GmailProviderError("Google OAuth rejected the request")
    try:
        token = response.json()
    except ValueError as error:
        raise GmailProviderError("Google OAuth returned an invalid response") from error
    if not isinstance(token, dict) or not isinstance(token.get("access_token"), str):
        raise GmailProviderError("Google OAuth returned an invalid response")
    return token


def exchange_code(code: str, code_verifier: str) -> dict:
    return _token_post({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(), "code_verifier": code_verifier})


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _api_get(path: str, access_token: str, params: dict | None = None) -> dict:
    try:
        response = httpx.get(
            f"{GMAIL_API}{path}", params=params,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False,
        )
    except httpx.HTTPError as error:
        raise GmailProviderError("Gmail API could not be reached") from error
    if response.status_code == 401:
        raise GmailReauthorizationRequired("Gmail authorization expired")
    if response.status_code == 403:
        raise GmailReauthorizationRequired("Gmail permission was denied")
    if response.status_code == 404 and path.endswith("/history"):
        raise GmailHistoryExpired("Gmail history cursor expired")
    if response.status_code == 404 and "/messages/" in path:
        raise GmailMessageUnavailable("Gmail message is no longer available")
    if response.status_code >= 500 or response.status_code == 429:
        raise GmailProviderError("Gmail API is temporarily unavailable")
    if response.status_code >= 400:
        raise GmailProviderError("Gmail API rejected the request")
    try:
        result = response.json()
    except ValueError as error:
        raise GmailProviderError("Gmail API returned an invalid response") from error
    if not isinstance(result, dict):
        raise GmailProviderError("Gmail API returned an invalid response")
    return result


def get_profile(access_token: str) -> dict:
    result = _api_get("/users/me/profile", access_token)
    email = result.get("emailAddress")
    history_id = result.get("historyId")
    if not isinstance(email, str) or "@" not in email or not isinstance(history_id, str):
        raise GmailProviderError("Gmail returned an incomplete mailbox profile")
    return result


def gmail_get(path: str, access_token: str, params: dict | None = None) -> dict:
    return _api_get(path, access_token, params)


def refresh_connection_access_token(connection: IntegrationConnection) -> tuple[str, datetime, str | None]:
    if not connection.refresh_token_ciphertext:
        raise GmailReauthorizationRequired("Gmail connection needs authorization")
    try:
        refresh_token = secrets_store.decrypt(connection.refresh_token_ciphertext)
    except (ValueError, RuntimeError) as error:
        raise GmailReauthorizationRequired("Gmail connection needs authorization") from error
    try:
        token = _token_post({"grant_type": "refresh_token", "refresh_token": refresh_token})
    except GmailProviderError as error:
        # Google uses invalid_grant for a revoked or expired refresh token. Keep
        # the user-facing state generic and never log its response body.
        if "rejected" in str(error).lower():
            raise GmailReauthorizationRequired("Gmail connection needs authorization") from error
        raise
    access_token = token["access_token"]
    expires_in = token.get("expires_in", 3600)
    if not isinstance(expires_in, int) or expires_in < 60:
        expires_in = 3600
    expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=expires_in)
    next_refresh = token.get("refresh_token") if isinstance(token.get("refresh_token"), str) else None
    connection.access_token_ciphertext = secrets_store.encrypt(access_token)
    connection.access_token_expires_at = expiry
    if next_refresh:
        connection.refresh_token_ciphertext = secrets_store.encrypt(next_refresh)
    return access_token, expiry, next_refresh


def get_access_token(connection: IntegrationConnection) -> str:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if connection.access_token_ciphertext and connection.access_token_expires_at and connection.access_token_expires_at > now + timedelta(seconds=90):
        try:
            return secrets_store.decrypt(connection.access_token_ciphertext)
        except (ValueError, RuntimeError):
            pass
    access_token, _, _ = refresh_connection_access_token(connection)
    return access_token
