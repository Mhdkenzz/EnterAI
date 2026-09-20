"""AI request telemetry (structured, safe logging).

Records per request:
- provider / model / profile
- tool count offered / called
- adapter used
- parse/validation failures
- rounds / retries / timeout category
- usage (input / output / reasoning where available)
- latency (inference, tool, total)
- confirmation generated/redeemed
- final status

Does NOT log: passwords, auth tokens, SCIM tokens, encryption keys, API keys,
raw confirmation tokens (only token presence/consumption status), JWTs,
OIDC secrets.
"""
from __future__ import annotations
from typing import Any

SAFE_METRICS: set[str] = {
    "provider", "model", "profile_name", "adapter_used",
    "tools_offered", "tools_called", "rounds", "retries",
    "timeout_category", "input_tokens", "output_tokens", "reasoning_tokens",
    "inference_latency_ms", "tool_latency_ms", "total_latency_ms",
    "parse_failures", "validation_failures", "final_status",
    "confirmation_generated", "confirmation_redeemed",
    "routing_mode", "routed_domains", "routed_tool_names",
    "full_tool_names",
}

_SENSITIVE_SUBSTRINGS = {
    "password", "token", "secret", "key", "api_key", "credential",
    "scim", "jwt", "oidc", "authorization", "auth", "bearer",
}


def _is_sensitive_key(key: str) -> bool:
    key_lower = key.lower()
    for sub in _SENSITIVE_SUBSTRINGS:
        if sub in key_lower:
            return True
    return False


def sanitize_metrics(raw: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for k, v in raw.items():
        # Redact any key that contains a sensitive substring.
        if isinstance(k, str) and _is_sensitive_key(k):
            result[k] = "[REDACTED]"
            continue
        # Only keep explicitly safe metric keys; reject anything unexpected.
        if isinstance(k, str) and k in SAFE_METRICS:
            result[k] = v
    return result
