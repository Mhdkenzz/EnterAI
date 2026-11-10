"""One place to read AI-related environment configuration.

A plain frozen dataclass, not a settings library: no settings class exists
anywhere else in this app (embeddings.py, ratelimit.py, execution.py all read
os.getenv directly), so this matches the codebase's actual convention rather
than introducing a new one.

`timeout_seconds` is deliberately `None` when AI_TIMEOUT_SECONDS is unset,
rather than falling back to a hardcoded number here -- callers resolve the
final value themselves (explicit env var > model capability profile >
provider-class fallback), so this type must not bake in a guess.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# Each provider class has always had its own base_url/model default; preserved
# here per-provider rather than collapsed into one pair, so this type's
# defaults match today's actual behavior exactly.
_DEFAULT_BASE_URL = {
    "anthropic": "https://api.anthropic.com",
    "openrouter": "https://openrouter.ai/api/v1",
    "ifm": "https://api.ifm.ai/v1",
    "openai_compatible": "http://127.0.0.1:1234/v1",
    "lmstudio": "http://127.0.0.1:1234/v1",
    "ollama": "http://127.0.0.1:11434/v1",
}
_DEFAULT_MODEL = {
    "anthropic": "claude-sonnet-5",
    "openrouter": "openai/gpt-4o-mini",
    "ifm": "IFM/K2-Horizon-375B-A23B",
    "openai_compatible": "local-model",
    "lmstudio": "local-model",
    "ollama": "enterai-qwen3:4b",
}


@dataclass(frozen=True)
class AISettings:
    provider: str
    base_url: str
    model: str
    api_key: str
    timeout_seconds: float | None
    anthropic_version: str
    embedding_dimension: int

    @classmethod
    def from_env(cls) -> "AISettings":
        provider = os.getenv("AI_PROVIDER", "deterministic").strip().lower()
        raw_timeout = os.getenv("AI_TIMEOUT_SECONDS")
        base_url = (os.getenv("IFM_BASE_URL", "") if provider == "ifm" else os.getenv("AI_BASE_URL", "")).strip()
        model = (os.getenv("IFM_MODEL", "") if provider == "ifm" else os.getenv("AI_MODEL", "")).strip()
        api_key = (os.getenv("IFM_API_KEY", "") or os.getenv("AI_API_KEY", "")) if provider == "ifm" else os.getenv("AI_API_KEY", "")
        return cls(
            provider=provider,
            base_url=(base_url or _DEFAULT_BASE_URL.get(provider, "http://127.0.0.1:1234/v1")).rstrip("/"),
            model=model or _DEFAULT_MODEL.get(provider, "local-model"),
            api_key=api_key,
            timeout_seconds=float(raw_timeout) if raw_timeout else None,
            anthropic_version=os.getenv("AI_ANTHROPIC_VERSION", "2023-06-01"),
            embedding_dimension=int(os.getenv("EMBEDDING_DIMENSION", "1536")),
        )
