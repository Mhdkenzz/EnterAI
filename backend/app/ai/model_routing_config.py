"""Config-driven model routing roles (foundation only, no complex multi-agent council).

Possible roles (not all active by default):
- FAST_MODEL: simple reads / routing
- TOOL_MODEL: tool-heavy execution
- REASONING_MODEL: complex planning / reasoning-heavy prompts
- VISION_MODEL: image/document vision
- EMBEDDING_MODEL: RAG only

For now, the default provider/model remains active unless environment/config
explicitly selects another role. This preserves existing behavior while
making future routing possible.
"""
from __future__ import annotations
import os

MODEL_ROLES: dict[str, dict[str, str]] = {
    # Example configuration structure; actual selection requires provider setup
    "FAST_MODEL": {"provider": "openai_compatible", "model": "gpt-4o-mini"},
    "TOOL_MODEL": {"provider": "openai_compatible", "model": "fabliq-8b-agent-reasoning"},
    "REASONING_MODEL": {"provider": "openrouter", "model": "stealth/union-alpha"},
    "VISION_MODEL": {},  # Not configured by default
    "EMBEDDING_MODEL": {"provider": "local", "model": "all-MiniLM-L6-v2"},
}


def get_role_config(role: str) -> dict[str, str]:
    role = role.upper()
    default = MODEL_ROLES.get(role, {})
    # Environment overrides
    env_provider = os.getenv(f"{role}_PROVIDER")
    env_model = os.getenv(f"{role}_MODEL")
    if env_provider:
        default = {**default, "provider": env_provider}
    if env_model:
        default = {**default, "model": env_model}
    return default
