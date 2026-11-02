"""Restricted, isolated browser sessions for the CEO agent's UI CLI.

The browser is useful for reading the rendered UI and operating its navigation,
filters, dialogs, and viewport. Mutating API requests are blocked by default;
a human administrator can enable direct ordinary workspace changes for the CEO.
"""
from __future__ import annotations

import atexit
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .auth import SESSION_COOKIE, create_token


class BrowserStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(pattern=r"^(inspect|navigate|back|forward|click|hover|fill|select|press|drag|scroll|zoom)$")
    path: str | None = None
    role: str | None = Field(default=None, max_length=32)
    target: str | None = Field(default=None, max_length=180)
    to_target: str | None = Field(default=None, max_length=180)
    value: str | None = Field(default=None, max_length=4000)
    direction: str | None = Field(default=None, pattern=r"^(up|down|left|right)$")
    pixels: int | None = Field(default=None, ge=1, le=5000)
    factor: float | None = Field(default=None, ge=0.5, le=2.0)


class BrowserCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(default="/", max_length=500)
    steps: list[BrowserStep] = Field(min_length=1, max_length=8)


_APP_ROUTES = {
    "/", "/projects", "/my-work", "/inbox", "/teams", "/agents",
    "/ai", "/settings", "/admin",
}
_ROLE_NAMES = {"button", "link", "textbox", "checkbox", "radio", "tab", "menuitem", "heading", "option", "combobox"}
_sessions: dict[str, "_AgentSession"] = {}
_lock = threading.RLock()
_playwright = None
_browser = None
_SESSION_IDLE_SECONDS = 900
_MAX_SNAPSHOT_CHARS = 10000


@dataclass
class _AgentSession:
    context: Any
    page: Any
    app_url: str
    api_url: str
    allow_writes: bool = False
    last_used: float = field(default_factory=time.monotonic)
    blocked_writes: list[str] = field(default_factory=list)


def _configured_urls() -> tuple[str, str]:
    app_url = (os.getenv("ENTER_AI_BROWSER_APP_URL") or os.getenv("APP_BASE_URL") or "http://localhost:3000").rstrip("/")
    api_url = (
        os.getenv("ENTER_AI_BROWSER_API_URL")
        or os.getenv("NEXT_PUBLIC_API_URL")
        or f"{os.getenv('API_BASE_URL', 'http://localhost:8000').rstrip('/')}/api"
    ).rstrip("/")
    for label, value in (("app", app_url), ("API", api_url)):
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError(f"The CEO browser {label} URL must be an absolute HTTP(S) URL without credentials.")
    return app_url, api_url


def _safe_app_path(path: str) -> str:
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or not path.startswith("/") or path.startswith("//") or "\\" in path:
        raise ValueError("Use an Enter AI path such as /agents or /projects/PROJECT_ID.")
    if any(part in {".", ".."} for part in parsed.path.split("/")):
        raise ValueError("Path traversal is not allowed.")
    if parsed.path not in _APP_ROUTES and not re.fullmatch(r"/projects/[A-Za-z0-9_-]+", parsed.path):
        raise ValueError("That route is outside the Enter AI workspace.")
    return path


def _ensure_browser():
    global _playwright, _browser
    if _browser is not None and _browser.is_connected():
        return _browser
    from playwright.sync_api import sync_playwright

    _playwright = sync_playwright().start()
    _browser = _playwright.chromium.launch(headless=True, args=["--no-sandbox"])
    return _browser


def _close_session(session: _AgentSession) -> None:
    try:
        session.context.close()
    except Exception:
        pass


def _new_session(agent, allow_writes: bool) -> _AgentSession:
    browser = _ensure_browser()
    app_url, api_url = _configured_urls()
    api_origin = urlsplit(api_url)
    app_origin = urlsplit(app_url)
    context = browser.new_context(
        viewport={"width": 1440, "height": 1000},
        accept_downloads=False,
        service_workers="block",
    )
    token = create_token(agent)
    context.add_cookies([{
        "name": SESSION_COOKIE,
        "value": token,
        "url": f"{api_origin.scheme}://{api_origin.netloc}",
        "path": "/api",
        "httpOnly": True,
        "secure": api_origin.scheme == "https",
        "sameSite": "None" if app_origin.netloc != api_origin.netloc and app_origin.scheme == "https" else "Lax",
    }])
    page = context.new_page()
    session = _AgentSession(context=context, page=page, app_url=app_url, api_url=api_url, allow_writes=allow_writes)
    page.on("dialog", lambda dialog: dialog.accept() if session.allow_writes else dialog.dismiss())
    app_origin_url = f"{app_origin.scheme}://{app_origin.netloc}"
    api_origin_url = f"{api_origin.scheme}://{api_origin.netloc}"
    api_port = api_origin.port or (443 if api_origin.scheme == "https" else 80)

    def guard(route):
        request = route.request
        request_url = urlsplit(request.url)
        origin = f"{request_url.scheme}://{request_url.netloc}"
        loopback_alias = (
            origin != api_origin_url
            and request_url.hostname in {"localhost", "127.0.0.1"}
            and (request_url.port or (443 if request_url.scheme == "https" else 80)) == api_port
            and api_origin.hostname == "host.docker.internal"
        )
        if origin not in {app_origin_url, api_origin_url} and not loopback_alias:
            route.abort()
            return
        api_path = api_origin.path.rstrip("/")
        api_request = origin == api_origin_url and (not api_path or request_url.path == api_path or request_url.path.startswith(api_path + "/"))
        if (api_request or loopback_alias) and request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            protected_prefixes = (
                "/api/admin", "/api/auth", "/api/billing", "/api/privacy",
                "/api/me/password", "/api/ai/confirm",
            )
            protected = any(request_url.path == prefix or request_url.path.startswith(prefix + "/") for prefix in protected_prefixes)
            if not session.allow_writes or protected:
                session.blocked_writes.append(f"{request.method.upper()} {request_url.path}")
                route.abort("blockedbyclient")
                return
        if loopback_alias:
            # The frontend's dev bundle uses localhost:8000, which inside the API
            # container points back to itself. Rewrite that loopback request to the
            # host gateway and attach only this isolated session's cookie.
            target = f"{api_origin_url}{request_url.path}"
            if request_url.query:
                target += f"?{request_url.query}"
            headers = dict(request.headers)
            headers["cookie"] = f"{SESSION_COOKIE}={token}"
            route.continue_(url=target, headers=headers)
            return
        route.continue_()

    context.route("**/*", guard)
    return session


def _session_for(agent, allow_writes: bool) -> _AgentSession:
    session = _sessions.get(agent.id)
    if session is not None and session.app_url == _configured_urls()[0] and session.api_url == _configured_urls()[1]:
        session.allow_writes = allow_writes
        session.last_used = time.monotonic()
        return session
    if session is not None:
        _close_session(session)
    session = _new_session(agent, allow_writes)
    _sessions[agent.id] = session
    return session


def _snapshot(page) -> str:
    try:
        snapshot = page.locator("body").aria_snapshot(timeout=3000)
    except Exception:
        snapshot = page.locator("body").inner_text(timeout=3000)
    return snapshot[:_MAX_SNAPSHOT_CHARS]


def _apply_step(session: _AgentSession, step: BrowserStep) -> dict[str, Any]:
    page = session.page
    command = step.command
    if command == "navigate":
        path = _safe_app_path(step.path or "/")
        page.goto(session.app_url + path, wait_until="domcontentloaded", timeout=15000)
    elif command in {"back", "forward"}:
        (page.go_back if command == "back" else page.go_forward)(wait_until="domcontentloaded", timeout=10000)
        if not page.url.startswith(session.app_url):
            page.goto(session.app_url + "/", wait_until="domcontentloaded", timeout=15000)
    elif command == "inspect":
        pass
    elif command in {"click", "hover"}:
        if not step.role or step.role not in _ROLE_NAMES or not step.target:
            raise ValueError(f"{command.title()} needs a supported role and the element's accessible name.")
        locator = page.get_by_role(step.role, name=step.target, exact=True)
        count = locator.count()
        if count != 1:
            raise ValueError(f"Expected one {step.role} named {step.target!r}; found {count}.")
        if command == "click":
            locator.click(timeout=5000)
        else:
            locator.hover(timeout=5000)
    elif command == "fill":
        if not step.target or step.value is None:
            raise ValueError("Fill needs a field label and a value.")
        locator = page.get_by_label(step.target, exact=True)
        count = locator.count()
        if count != 1:
            raise ValueError(f"Expected one field labeled {step.target!r}; found {count}.")
        locator.fill(step.value, timeout=5000)
    elif command == "select":
        if not step.target or step.value is None:
            raise ValueError("Select needs a field label and an option value.")
        locator = page.get_by_label(step.target, exact=True)
        count = locator.count()
        if count != 1:
            raise ValueError(f"Expected one field labeled {step.target!r}; found {count}.")
        locator.select_option(step.value, timeout=5000)
    elif command == "press":
        if not step.value:
            raise ValueError("Press needs a keyboard key such as Enter, Escape, or Tab.")
        page.keyboard.press(step.value)
    elif command == "scroll":
        if not step.direction:
            raise ValueError("Scroll needs a direction: up, down, left, or right.")
        pixels = step.pixels or 500
        x = pixels if step.direction == "right" else -pixels if step.direction == "left" else 0
        y = pixels if step.direction == "down" else -pixels if step.direction == "up" else 0
        if step.target:
            if not step.role or step.role not in _ROLE_NAMES:
                raise ValueError("Scrolling a specific region needs its supported role and accessible name.")
            locator = page.get_by_role(step.role, name=step.target, exact=True)
            if locator.count() != 1:
                raise ValueError(f"Expected one {step.role} named {step.target!r}.")
            locator.evaluate("(el, delta) => el.scrollBy(delta.x, delta.y)", {"x": x, "y": y})
        else:
            page.mouse.wheel(x, y)
    elif command == "drag":
        if not step.role or step.role not in _ROLE_NAMES or not step.target or not step.to_target:
            raise ValueError("Drag needs a supported role and accessible names for the source and destination.")
        source = page.get_by_role(step.role, name=step.target, exact=True)
        destination = page.get_by_role(step.role, name=step.to_target, exact=True)
        if source.count() != 1 or destination.count() != 1:
            raise ValueError("Drag source and destination must each match one accessible element.")
        source.drag_to(destination, timeout=5000)
    elif command == "zoom":
        factor = step.factor or 1.0
        page.evaluate("factor => { document.documentElement.style.zoom = String(factor); }", factor)
    else:
        raise ValueError("Unsupported UI command.")
    page.wait_for_timeout(150)
    return {"command": command, "path": page.url.removeprefix(session.app_url), "screen": _snapshot(page)}


def run_ceo_browser_cli(agent, raw_args: Any, *, allow_writes: bool = False) -> dict[str, Any]:
    """Execute the CEO-only, app-scoped UI CLI with an isolated agent session."""
    if getattr(agent, "kind", None) != "agent" or getattr(agent, "hierarchy_level", None) != "ceo":
        raise ValueError("The UI CLI is available only to the CEO agent.")
    try:
        command = BrowserCommand.model_validate(raw_args)
    except ValidationError as error:
        raise ValueError("Use browser CLI steps with a command, and provide only fields required by that command.") from error
    initial_path = _safe_app_path(command.path)
    with _lock:
        now = time.monotonic()
        for agent_id, session in list(_sessions.items()):
            if now - session.last_used > _SESSION_IDLE_SECONDS:
                _close_session(session)
                _sessions.pop(agent_id, None)
        session = _session_for(agent, allow_writes)
        session.blocked_writes.clear()
        if not session.page.url or not session.page.url.startswith(session.app_url):
            session.page.goto(session.app_url + initial_path, wait_until="domcontentloaded", timeout=15000)
        elif session.page.url.removeprefix(session.app_url) != initial_path:
            session.page.goto(session.app_url + initial_path, wait_until="domcontentloaded", timeout=15000)
        results = [_apply_step(session, step) for step in command.steps]
        return {
            "steps": results,
            "blocked_writes": list(dict.fromkeys(session.blocked_writes)),
            "full_access": session.allow_writes,
            "note": (
                "Full access is enabled for ordinary workspace UI changes. Human-admin, authentication, billing, privacy, security, and confirmation endpoints remain protected."
                if session.allow_writes else
                "UI changes are blocked. Use the normal proposal tool for a human-confirmed change."
            ),
        }


def close_browser_sessions() -> None:
    global _browser, _playwright
    with _lock:
        for session in _sessions.values():
            _close_session(session)
        _sessions.clear()
        if _browser is not None:
            _browser.close()
            _browser = None
        if _playwright is not None:
            _playwright.stop()
            _playwright = None


atexit.register(close_browser_sessions)
