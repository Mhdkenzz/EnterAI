"""Notion OAuth and API helpers for permission-scoped workspace intake."""
from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from . import secrets_store
from .models import IntegrationConnection

API_BASE = "https://api.notion.com/v1"
OAUTH_BASE = f"{API_BASE}/oauth"
NOTION_VERSION = "2025-09-03"
REDIRECT_PATH = "/api/integrations/notion/callback"
TIMEOUT_SECONDS = 20.0


class NotionProviderError(RuntimeError):
    pass


class NotionReauthorizationRequired(NotionProviderError):
    pass


def configured() -> bool:
    return bool(os.getenv("NOTION_CLIENT_ID", "").strip() and os.getenv("NOTION_CLIENT_SECRET", "").strip())


def redirect_uri() -> str:
    return os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/") + REDIRECT_PATH


def authorization_url(state: str) -> str:
    if not configured():
        raise NotionProviderError("Notion OAuth is not configured")
    params = {
        "owner": "user", "client_id": os.environ["NOTION_CLIENT_ID"].strip(),
        "redirect_uri": redirect_uri(), "response_type": "code", "state": state,
    }
    return "https://api.notion.com/v1/oauth/authorize?" + urlencode(params)


def _json_response(response: httpx.Response, action: str) -> dict:
    try:
        payload = response.json()
    except ValueError as error:
        raise NotionProviderError(f"Notion {action} returned an invalid response") from error
    if response.status_code in (401, 403):
        raise NotionReauthorizationRequired(f"Notion {action} requires reconnection")
    if response.status_code >= 400 or not isinstance(payload, dict):
        raise NotionProviderError(f"Notion {action} failed")
    return payload


def exchange_code(code: str) -> dict:
    credentials = f"{os.getenv('NOTION_CLIENT_ID', '').strip()}:{os.getenv('NOTION_CLIENT_SECRET', '').strip()}".encode()
    try:
        response = httpx.post(f"{OAUTH_BASE}/token", headers={
            "Authorization": "Basic " + base64.b64encode(credentials).decode(),
            "Content-Type": "application/json", "Notion-Version": NOTION_VERSION,
        }, json={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri()},
            timeout=TIMEOUT_SECONDS, follow_redirects=False)
    except httpx.HTTPError as error:
        raise NotionProviderError("Notion OAuth could not be reached") from error
    result = _json_response(response, "OAuth")
    if not isinstance(result.get("access_token"), str) or not isinstance(result.get("workspace_id"), str):
        raise NotionProviderError("Notion OAuth returned incomplete credentials")
    return result


def revoke_token(access_token: str) -> None:
    credentials = f"{os.getenv('NOTION_CLIENT_ID', '').strip()}:{os.getenv('NOTION_CLIENT_SECRET', '').strip()}".encode()
    try:
        response = httpx.post(f"{OAUTH_BASE}/revoke", headers={
            "Authorization": "Basic " + base64.b64encode(credentials).decode(),
            "Content-Type": "application/json", "Notion-Version": NOTION_VERSION,
        }, json={"token": access_token}, timeout=TIMEOUT_SECONDS, follow_redirects=False)
    except httpx.HTTPError as error:
        raise NotionProviderError("Notion token revocation could not be reached") from error
    _json_response(response, "token revocation")


def _api_request(method: str, path: str, access_token: str, *, json: dict | None = None) -> dict:
    try:
        response = httpx.request(method, f"{API_BASE}{path}", headers={
            "Authorization": f"Bearer {access_token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }, json=json, timeout=TIMEOUT_SECONDS, follow_redirects=False)
    except httpx.HTTPError as error:
        raise NotionProviderError("Notion API could not be reached") from error
    return _json_response(response, "API request")


def refresh_connection_access_token(connection: IntegrationConnection) -> tuple[str, datetime]:
    if not connection.refresh_token_ciphertext:
        raise NotionReauthorizationRequired("Notion connection needs authorization")
    try:
        refresh_token = secrets_store.decrypt(connection.refresh_token_ciphertext)
    except (ValueError, RuntimeError) as error:
        raise NotionReauthorizationRequired("Notion connection needs authorization") from error
    credentials = f"{os.getenv('NOTION_CLIENT_ID', '').strip()}:{os.getenv('NOTION_CLIENT_SECRET', '').strip()}".encode()
    try:
        response = httpx.post(f"{OAUTH_BASE}/token", headers={
            "Authorization": "Basic " + base64.b64encode(credentials).decode(),
            "Content-Type": "application/json", "Notion-Version": NOTION_VERSION,
        }, json={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=TIMEOUT_SECONDS, follow_redirects=False)
    except httpx.HTTPError as error:
        raise NotionProviderError("Notion token refresh could not be reached") from error
    result = _json_response(response, "token refresh")
    access_token, next_refresh = result.get("access_token"), result.get("refresh_token")
    expires_in = result.get("expires_in", 3600)
    if not isinstance(access_token, str) or not isinstance(next_refresh, str) or not isinstance(expires_in, int) or expires_in < 60:
        raise NotionProviderError("Notion token refresh returned incomplete credentials")
    expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=expires_in)
    connection.access_token_ciphertext = secrets_store.encrypt(access_token)
    connection.refresh_token_ciphertext = secrets_store.encrypt(next_refresh)
    connection.access_token_expires_at = expiry
    return access_token, expiry


def get_access_token(connection: IntegrationConnection) -> str:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if connection.access_token_expires_at and connection.access_token_expires_at <= now + timedelta(minutes=5):
        token, _ = refresh_connection_access_token(connection)
        return token
    if not connection.access_token_ciphertext:
        raise NotionReauthorizationRequired("Notion connection needs authorization")
    try:
        return secrets_store.decrypt(connection.access_token_ciphertext)
    except (ValueError, RuntimeError) as error:
        raise NotionReauthorizationRequired("Notion connection needs authorization") from error


def search_pages(access_token: str, cursor: str | None = None) -> dict:
    payload = {"sort": {"direction": "descending", "timestamp": "last_edited_time"},
               "filter": {"property": "object", "value": "page"}, "page_size": 100}
    if cursor:
        payload["start_cursor"] = cursor
    return _api_request("POST", "/search", access_token, json=payload)


def retrieve_page(access_token: str, page_id: str) -> dict:
    return _api_request("GET", f"/pages/{page_id}", access_token)


def block_children(access_token: str, block_id: str, cursor: str | None = None) -> dict:
    path = f"/blocks/{block_id}/children?page_size=100"
    if cursor:
        from urllib.parse import quote
        path += "&start_cursor=" + quote(cursor, safe="")
    return _api_request("GET", path, access_token)
