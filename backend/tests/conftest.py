"""Suite-wide setup that has to happen before `app` is imported.

The auth rate limiters are per client IP, and every test in this suite shares one
client identity, so the production defaults would throttle the suite itself rather
than anything it is testing. Raising them here keeps the control switched on --
`test_auth_rate_limits.py` drives it directly with its own limits -- while letting
hundreds of logins in a run through.
"""
import os

os.environ.setdefault("AUTH_LOGIN_RATE_LIMIT_PER_MINUTE", "100000")
os.environ.setdefault("AUTH_SIGNUP_RATE_LIMIT_PER_MINUTE", "100000")
os.environ.setdefault("AUTH_RECOVERY_RATE_LIMIT_PER_MINUTE", "100000")
os.environ.setdefault("AUTH_TOKEN_RATE_LIMIT_PER_MINUTE", "100000")

import re

import pytest


@pytest.fixture
def outbox():
    """The console provider's captured messages, emptied around each test."""
    from app import email as mailer
    provider = mailer.get_email_provider()
    provider.outbox.clear()
    yield provider.outbox
    provider.outbox.clear()


def link_token(body: str, parameter: str) -> str:
    """Pull the single-use token out of an emailed link, the way a recipient would."""
    match = re.search(rf"[?&]{parameter}=([A-Za-z0-9_\-]+)", body)
    assert match, f"no {parameter} link in email body: {body!r}"
    return match.group(1)
