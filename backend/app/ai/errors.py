"""Typed, retryable-aware error taxonomy for the AI execution layer.

`retryable` tells a caller whether the same request is safe to retry as-is (a
transient transport/timeout failure) versus one that will fail identically
every time until something about the request itself changes (bad arguments,
a permission denial, an unsupported tool, a loop that's run too long).
"""
from __future__ import annotations


class AIError(RuntimeError):
    retryable: bool = False


class ProviderUnavailableError(AIError):
    retryable = True


class ProviderTimeoutError(AIError):
    retryable = True


class ProviderAuthError(AIError):
    retryable = False


class ProviderRateLimitedError(AIError):
    retryable = True


class ToolCallValidationError(AIError):
    retryable = False


class LoopBudgetExceededError(AIError):
    retryable = False


class DuplicateCallDetectedError(AIError):
    retryable = False
