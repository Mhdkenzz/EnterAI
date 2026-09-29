"""Small release-gate checks for dependencies used only at runtime.

These checks intentionally construct the OpenAI client without making a network
request.  The production image installs the same pinned requirements, so this
catches dependency combinations that import successfully but fail as soon as a
user uploads a document with OpenAI embeddings enabled.
"""

from importlib.metadata import version

import pytest


def test_pinned_openai_client_initializes_with_pinned_httpx():
    """The OpenAI embedding path must start before a first request is needed."""
    import httpx
    from openai import OpenAI

    assert version("httpx") == "0.28.1"
    assert version("openai") == "1.58.1"

    client = OpenAI(api_key="ci-only-key", base_url="http://127.0.0.1:9/v1")
    assert client.embeddings is not None


def test_openai_embedding_provider_initializes_without_network(monkeypatch):
    """Provider construction is a local release check, not an API smoke call."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    from app.embeddings import OpenAIEmbeddingProvider

    provider = OpenAIEmbeddingProvider(
        api_key="ci-only-key",
        model="text-embedding-3-small",
        base_url="http://127.0.0.1:9/v1",
    )
    assert provider.dimension() == 1536
    assert provider.max_batch_size() == 2048


def test_production_embedding_provider_fails_closed_when_unconfigured(monkeypatch):
    """A production boot cannot silently use non-semantic fake embeddings."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)

    from app.embeddings import get_embedding_provider

    with pytest.raises(RuntimeError, match="EMBEDDING_PROVIDER must be set"):
        get_embedding_provider()
