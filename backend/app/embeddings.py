"""Embedding provider abstraction for RAG.

Supports multiple embedding backends (OpenAI, local sentence-transformers)
with a consistent interface. Provider is selected via EMBEDDING_PROVIDER env var.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Protocol

# Optional imports - only loaded when the provider is used
try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore

try:
    from sentence_transformers import SentenceTransformer
except ImportError:  # pragma: no cover
    SentenceTransformer = None  # type: ignore


class EmbeddingProvider(Protocol):
    """Protocol for embedding providers."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a list of texts."""
        ...

    def dimension(self) -> int:
        """Return the embedding dimension."""
        ...

    def max_batch_size(self) -> int:
        """Return the maximum batch size for a single embed call."""
        ...


class BaseEmbeddingProvider(ABC):
    """Base class with common functionality."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        pass

    @abstractmethod
    def dimension(self) -> int:
        pass

    @abstractmethod
    def max_batch_size(self) -> int:
        pass

    def _validate_inputs(self, texts: list[str]) -> None:
        """Validate input texts before sending to provider."""
        if not texts:
            raise ValueError("Cannot embed empty list")
        for i, text in enumerate(texts):
            if not isinstance(text, str):
                raise TypeError(f"Item {i} is not a string: {type(text)}")
            # Strip null bytes and limit length
            if "\x00" in text:
                raise ValueError(f"Item {i} contains null bytes")
            if len(text) > 100_000:
                raise ValueError(f"Item {i} exceeds 100k character limit")


class OpenAIEmbeddingProvider(BaseEmbeddingProvider):
    """OpenAI embeddings API (text-embedding-3-small by default)."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "text-embedding-3-small",
        base_url: str | None = None,
    ):
        if OpenAI is None:
            raise RuntimeError("openai package not installed. Run: pip install openai")
        self._client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"), base_url=base_url)
        self._model = model
        # text-embedding-3-small = 1536, text-embedding-3-large = 3072, text-embedding-ada-002 = 1536
        self._dimension = 1536 if "small" in model or "ada" in model else 3072

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._validate_inputs(texts)
        # OpenAI accepts up to 2048 inputs per request
        batch_size = min(len(texts), 2048)
        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            response = self._client.embeddings.create(model=self._model, input=batch)
            all_embeddings.extend([d.embedding for d in response.data])
        return all_embeddings

    def dimension(self) -> int:
        return self._dimension

    def max_batch_size(self) -> int:
        return 2048


class LocalEmbeddingProvider(BaseEmbeddingProvider):
    """Local sentence-transformers embeddings (no API key required)."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        if SentenceTransformer is None:
            raise RuntimeError("sentence-transformers package not installed. Run: pip install sentence-transformers")
        self._model = SentenceTransformer(model_name)
        self._dimension = self._model.get_sentence_embedding_dimension()

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._validate_inputs(texts)
        # sentence-transformers handles batching internally
        embeddings = self._model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
        return embeddings.tolist()

    def dimension(self) -> int:
        return self._dimension

    def max_batch_size(self) -> int:
        return 1000  # Reasonable default for local inference


class DeterministicEmbeddingProvider(BaseEmbeddingProvider):
    """Deterministic fake embeddings for testing (no external dependencies).

    Uses a simple hash-based approach to generate consistent pseudo-embeddings.
    NOT suitable for production - only for tests and offline development.
    """

    def __init__(self, dimension: int = 1536):
        self._dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._validate_inputs(texts)
        import hashlib
        import struct

        embeddings: list[list[float]] = []
        for text in texts:
            # Create a deterministic hash-based embedding
            hash_obj = hashlib.sha256(text.encode("utf-8"))
            digest = hash_obj.digest()
            # Expand to desired dimension by repeating and XORing
            vec = []
            for i in range(self._dimension):
                byte_idx = i % len(digest)
                vec.append((digest[byte_idx] / 255.0) * 2 - 1)  # Map to [-1, 1]
            embeddings.append(vec)
        return embeddings

    def dimension(self) -> int:
        return self._dimension

    def max_batch_size(self) -> int:
        return 10000


def get_embedding_provider() -> EmbeddingProvider:
    """Factory function to get the configured embedding provider.

    EMBEDDING_PROVIDER values:
    - "openai" -> OpenAIEmbeddingProvider (requires OPENAI_API_KEY)
    - "local" -> LocalEmbeddingProvider (requires sentence-transformers)
    - "deterministic" -> DeterministicEmbeddingProvider (no deps, for tests)
    - unset/empty -> DeterministicEmbeddingProvider (default for dev), but
      production must set EMBEDDING_PROVIDER explicitly -- see the fail-closed
      check below.
    """
    provider_name = os.getenv("EMBEDDING_PROVIDER", "").strip().lower()
    environment = os.getenv("ENVIRONMENT", "development").strip().lower()

    if not provider_name:
        if environment == "production":
            # Deterministic embeddings are a SHA256 hash, not a semantic vector --
            # search built on them returns essentially random nearest-neighbors.
            # Leaving this unset in production would ship that silently, exactly
            # like the JWT_SECRET/REDIS_URL/STRIPE_WEBHOOK_SECRET checks refuse to
            # silently fall back to an unsafe default.
            raise RuntimeError(
                "EMBEDDING_PROVIDER must be set in production ('openai' or 'local')."
                " Leaving it unset falls back to deterministic fake embeddings, which"
                " are not semantic and make RAG search return meaningless results."
                " Set EMBEDDING_PROVIDER=openai or EMBEDDING_PROVIDER=local, or set it"
                " explicitly to 'deterministic' only if this deployment intentionally"
                " does not use RAG document search."
            )
        provider_name = "deterministic"
    elif provider_name == "deterministic" and environment == "production":
        import logging
        logging.getLogger(__name__).warning(
            "embedding_provider_deterministic_in_production",
            extra={"event": "embedding_provider_deterministic_in_production"},
        )

    if provider_name == "openai":
        return OpenAIEmbeddingProvider(
            api_key=os.getenv("OPENAI_API_KEY"),
            model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
            base_url=os.getenv("EMBEDDING_BASE_URL") or None,
        )
    if provider_name == "local":
        return LocalEmbeddingProvider(
            model_name=os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2"),
        )
    # deterministic is the safe default
    dimension = int(os.getenv("EMBEDDING_DIMENSION", "1536"))
    return DeterministicEmbeddingProvider(dimension=dimension)