"""Poll Notion pages shared with the OAuth connection and queue new revisions."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import notion_api
from .database import SessionLocal, engine
from .models import InboundMessage, IntegrationConnection
from .services import log

logger = logging.getLogger("enterai.notion_sync")
MAX_PAGES_PER_TICK = 500
MAX_CONTENT_CHARS = 50000


def _parse_time(value: str | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def _cursor_state(value: str | None) -> tuple[datetime | None, datetime | None, str | None]:
    """Decode the durable Notion pagination checkpoint, retaining old ISO cursors."""
    if isinstance(value, str) and value.startswith("@"):
        try:
            base_ms, newest_ms, next_cursor = value[1:].split(":", 2)
            base = datetime.fromtimestamp(int(base_ms) / 1000, timezone.utc).replace(tzinfo=None)
            newest = datetime.fromtimestamp(int(newest_ms) / 1000, timezone.utc).replace(tzinfo=None)
            return base, newest, next_cursor
        except (ValueError, OverflowError):
            return None, None, None
    parsed = _parse_time(value)
    return parsed, parsed, None


def _timestamp_ms(value: datetime) -> int:
    return int(value.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _title(page: dict) -> str:
    props = page.get("properties") or {}
    if isinstance(props, dict):
        for prop in props.values():
            if isinstance(prop, dict) and prop.get("type") == "title":
                parts = prop.get("title") or []
                value = "".join(p.get("plain_text", "") for p in parts if isinstance(p, dict)).strip()
                if value:
                    return value[:500]
    return "Untitled Notion page"


def _block_text(block: dict) -> str:
    kind = block.get("type")
    item = block.get(kind, {}) if isinstance(kind, str) else {}
    if not isinstance(item, dict):
        return ""
    value = item.get("rich_text") or item.get("title") or []
    return "".join(x.get("plain_text", "") for x in value if isinstance(x, dict)).strip()


def _page_content(token: str, page_id: str) -> str:
    output: list[str] = []
    visited = 0

    def collect(parent_id: str, depth: int) -> None:
        nonlocal visited
        if depth > 4 or visited >= 1000 or sum(map(len, output)) >= MAX_CONTENT_CHARS:
            return
        cursor = None
        while visited < 1000 and sum(map(len, output)) < MAX_CONTENT_CHARS:
            response = notion_api.block_children(token, parent_id, cursor)
            for block in response.get("results", []):
                if not isinstance(block, dict):
                    continue
                visited += 1
                text = _block_text(block)
                if text:
                    output.append(text)
                if block.get("has_children") and isinstance(block.get("id"), str):
                    collect(block["id"], depth + 1)
            if not response.get("has_more") or not isinstance(response.get("next_cursor"), str):
                break
            cursor = response["next_cursor"]

    collect(page_id, 0)
    content = "\n".join(output).strip()
    return content[:MAX_CONTENT_CHARS] or "(This Notion page has no readable text blocks.)"


def _insert_revision(db: Session, connection: IntegrationConnection, token: str, hit: dict) -> bool:
    page_id = hit.get("id")
    edited = hit.get("last_edited_time")
    edited_at = _parse_time(edited)
    if not isinstance(page_id, str) or not edited_at:
        return False
    try:
        page = notion_api.retrieve_page(token, page_id)
        title = _title(page)
        body = _page_content(token, page_id)
    except notion_api.NotionProviderError:
        raise
    url = page.get("url") if isinstance(page.get("url"), str) else hit.get("url")
    editor = page.get("last_edited_by") or {}
    source = InboundMessage(
        organization_id=connection.organization_id, source="notion",
        external_id=f"{page_id}:{edited}", thread_id=page_id,
        sender=editor.get("id") if isinstance(editor, dict) else None,
        subject=title, body=body, source_url=url,
        received_at=edited_at, status="new", triage_status="pending",
        created_by=connection.connected_by,
    )
    try:
        with db.begin_nested():
            db.add(source)
            db.flush()
        log(db, connection.organization_id, connection.connected_by, "inbound_message", source.id, "received", source="notion")
        return True
    except IntegrityError:
        return False


def _sync_one(db: Session, connection: IntegrationConnection) -> int:
    token = notion_api.get_access_token(connection)
    cursor_time, newest_seen, next_cursor = _cursor_state(connection.history_cursor)
    if cursor_time is None:
        # Establish a baseline at connect time. Existing shared pages are not
        # copied into the user's Requests queue.
        connection.history_cursor = datetime.now(timezone.utc).isoformat()
        connection.last_sync_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        return 0
    inserted = 0
    visited = 0
    completed_scan = False
    while visited < MAX_PAGES_PER_TICK:
        response = notion_api.search_pages(token, next_cursor)
        results = response.get("results") or []
        if not results:
            completed_scan = True
            break
        reached_watermark = False
        for page in results:
            if not isinstance(page, dict):
                continue
            visited += 1
            edited = _parse_time(page.get("last_edited_time"))
            if not edited:
                continue
            if edited > newest_seen:
                newest_seen = edited
            # Notion's search sort is timestamp-only, so a page newly shared or
            # edited with the exact watermark timestamp can appear after prior
            # pages from that same timestamp. Consume ties and rely on the unique
            # (page_id, edited_time) source key to skip already-seen revisions.
            if edited < cursor_time:
                reached_watermark = True
                continue
            inserted += int(_insert_revision(db, connection, token, page))
        if reached_watermark or not response.get("has_more") or not isinstance(response.get("next_cursor"), str):
            completed_scan = True
            break
        next_cursor = response["next_cursor"]
    if completed_scan:
        connection.history_cursor = newest_seen.isoformat()
    elif next_cursor:
        # Keep the original timestamp watermark and newest timestamp across
        # bounded batches, then resume from Notion's opaque continuation cursor.
        connection.history_cursor = f"@{_timestamp_ms(cursor_time)}:{_timestamp_ms(newest_seen)}:{next_cursor}"
        if len(connection.history_cursor) > 100:
            raise notion_api.NotionProviderError("Notion continuation cursor exceeds storage limit")
    connection.last_sync_at = datetime.now(timezone.utc).replace(tzinfo=None)
    connection.last_error = None
    db.commit()
    return inserted


def sync_connection(connection_id: str) -> int:
    with SessionLocal() as db:
        connection = db.scalar(select(IntegrationConnection).where(
            IntegrationConnection.id == connection_id,
            IntegrationConnection.provider == "notion",
            IntegrationConnection.status == "connected",
        ))
        if not connection:
            return 0
        try:
            return _sync_one(db, connection)
        except notion_api.NotionReauthorizationRequired:
            db.rollback()
            connection = db.get(IntegrationConnection, connection_id)
            if connection:
                connection.status = "reauthorization_required"
                connection.last_error = "reauthorization_required"
                db.commit()
        except notion_api.NotionProviderError:
            db.rollback()
            connection = db.get(IntegrationConnection, connection_id)
            if connection:
                connection.last_error = "temporary_failure"
                db.commit()
        except Exception:
            db.rollback()
            connection = db.get(IntegrationConnection, connection_id)
            if connection:
                connection.last_error = "temporary_failure"
                db.commit()
            logger.warning("notion_sync_failed", extra={"event": "notion_sync_failed"})
        return 0


def run_sync_tick() -> int:
    from .gmail_sync import _connection_lock
    with SessionLocal() as db:
        connection_ids = db.scalars(select(IntegrationConnection.id).where(
            IntegrationConnection.provider == "notion",
            IntegrationConnection.status == "connected",
        )).all()
    total = 0
    for connection_id in connection_ids:
        with _connection_lock(engine, f"notion:{connection_id}") as acquired:
            if acquired:
                total += sync_connection(connection_id)
    return total
