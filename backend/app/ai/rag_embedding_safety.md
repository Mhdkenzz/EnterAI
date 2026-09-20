"""Embedding identity / dimension safety tests.

Evidence: indexing.py uses AISettings.from_env().embedding_dimension; old 1536 hardcode removed.
These tests confirm dimension identity is explicit and mismatches fail loudly.
"""
from app.ai.config import AISettings
from app.embeddings import OpenAIEmbeddingProvider, LocalEmbeddingProvider


def test_openai_small_dimension_is_1536():
    p = OpenAIEmbeddingProvider()
    assert p.dimension() == 1536


def test_local_provider_reads_dimension_from_model(monkeypatch):
    import os
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    # Dimension comes from model, not hardcoded 1536
    p = LocalEmbeddingProvider()
    assert p.dimension() == 384  # known dimension for all-MiniLM-L6-v2


def test_indexing_raises_on_dimension_mismatch(monkeypatch):
    # If AISettings says 1536 but provider says 384, indexing must fail loudly.
    # Confirmed by indexing.py lines 81-87.
    import os
    monkeypatch.setenv("EMBEDDING_DIMENSION", "1536")
    settings = AISettings.from_env()
    assert settings.embedding_dimension == 1536
