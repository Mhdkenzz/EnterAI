"""Document indexing service for RAG.

Handles indexing, re-indexing, and batch indexing of ProjectDocuments into
document_chunks with vector embeddings. All operations enforce organization
and project ownership.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import DocumentChunk, ProjectDocument, User
from .embeddings import get_embedding_provider
from .chunking import chunk_document_text, Chunk

log = logging.getLogger(__name__)

# document_chunks.embedding is a fixed VECTOR(1536) column (see models.py /
# alembic 0009_rag_chunks). EMBEDDING_PROVIDER=local's default model
# (all-MiniLM-L6-v2) produces 384-dim vectors, which pgvector would otherwise
# reject at insert time with an opaque dimension-mismatch error.
DOCUMENT_CHUNK_EMBEDDING_DIM = 1536


def _sanitize_content(content: str) -> str:
    """Sanitize document content before indexing.

    - Strips null bytes
    - Limits to 100k characters
    - Ensures valid UTF-8
    """
    if not content:
        return ""
    # Remove null bytes
    content = content.replace("\x00", "")
    # Limit length
    if len(content) > 100_000:
        content = content[:100_000]
    # Ensure valid UTF-8
    return content.encode("utf-8", errors="replace").decode("utf-8")


def index_document(db: Session, document: ProjectDocument) -> int:
    """Index a single document: chunk, embed, and store.

    Deletes any existing chunks for this document first (idempotent).
    Returns the number of chunks created.
    """
    # Verify document has content
    text = _sanitize_content(document.extracted_text or "")
    if not text.strip():
        log.info("Document %s has no extractable text, skipping indexing", document.id)
        return 0

    # Delete existing chunks for this document
    db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document.id))

    # Chunk the text
    chunks = chunk_document_text(text)
    if not chunks:
        log.info("Document %s produced no chunks", document.id)
        return 0

    # Generate embeddings
    provider = get_embedding_provider()
    if provider.dimension() != DOCUMENT_CHUNK_EMBEDDING_DIM:
        raise RuntimeError(
            f"Embedding provider produces {provider.dimension()}-dim vectors, but "
            f"document_chunks.embedding is a fixed VECTOR({DOCUMENT_CHUNK_EMBEDDING_DIM}) "
            "column. Set EMBEDDING_MODEL to a model with a matching dimension, or "
            "re-run the pgvector migration with the provider's actual dimension."
        )
    chunk_texts = [c.content for c in chunks]
    embeddings = provider.embed(chunk_texts)

    if len(embeddings) != len(chunks):
        raise RuntimeError(f"Embedding count mismatch: {len(embeddings)} != {len(chunks)}")

    # Verify dimension matches
    expected_dim = provider.dimension()
    for i, emb in enumerate(embeddings):
        if len(emb) != expected_dim:
            raise RuntimeError(f"Embedding {i} has wrong dimension: {len(emb)} != {expected_dim}")

    # Bulk insert chunks
    db_chunks = [
        DocumentChunk(
            organization_id=document.organization_id,
            project_id=document.project_id,
            document_id=document.id,
            chunk_index=chunk.chunk_index,
            content=chunk.content,
            token_count=chunk.token_count,
            embedding=emb,
        )
        for chunk, emb in zip(chunks, embeddings)
    ]
    db.add_all(db_chunks)
    db.flush()

    log.info("Indexed document %s: %d chunks", document.id, len(db_chunks))
    return len(db_chunks)


def reindex_document(db: Session, document_id: str, user: User) -> int:
    """Re-index a document after content change.

    Verifies the user has access to the document's organization.
    Returns the number of chunks created.
    """
    document = db.get(ProjectDocument, document_id)
    if not document:
        raise ValueError(f"Document {document_id!r} not found")

    # Enforce ownership
    if document.organization_id != user.organization_id:
        raise ValueError("Document not found in this workspace")

    return index_document(db, document)


def index_all_documents(db: Session, organization_id: str) -> int:
    """Index all documents for an organization (batch operation).

    Useful for initial setup or after embedding model changes.
    Returns total chunks created across all documents.
    """
    documents = db.scalars(
        select(ProjectDocument).where(ProjectDocument.organization_id == organization_id)
    ).all()

    total_chunks = 0
    for doc in documents:
        try:
            total_chunks += index_document(db, doc)
        except Exception as e:  # pragma: no cover - log and continue
            log.error("Failed to index document %s: %s", doc.id, e)
            # Continue with other documents

    log.info("Batch indexing complete for org %s: %d total chunks", organization_id, total_chunks)
    return total_chunks


def get_document_chunks(
    db: Session,
    document_id: str,
    user: User,
    limit: int = 100,
) -> list[DocumentChunk]:
    """Retrieve chunks for a document (org-scoped)."""
    document = db.get(ProjectDocument, document_id)
    if not document or document.organization_id != user.organization_id:
        return []

    return list(
        db.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index)
            .limit(limit)
        ).all()
    )


def search_similar_chunks(
    db: Session,
    query_embedding: list[float],
    user: User,
    project_id: Optional[str] = None,
    limit: int = 10,
    similarity_threshold: float = 0.7,
) -> list[DocumentChunk]:
    """Search for chunks similar to the query embedding.

    Uses cosine similarity via pgvector. All results are scoped to the user's
    organization, optionally filtered by project.

    Args:
        db: Database session
        query_embedding: Embedding vector to search for
        user: Current user (for org scoping)
        project_id: Optional project filter
        limit: Maximum results
        similarity_threshold: Minimum cosine similarity (0-1)

    Returns:
        List of matching DocumentChunks ordered by similarity (highest first)
    """
    from sqlalchemy import text

    # Build the query with pgvector cosine similarity
    # cosine similarity = 1 - cosine_distance
    # We want similarity >= threshold, so distance <= 1 - threshold
    max_distance = 1.0 - similarity_threshold

    project_filter = "AND dc.project_id = :project_id" if project_id else ""
    sql = f"""
        SELECT dc.*
        FROM document_chunks dc
        WHERE dc.organization_id = :org_id
        AND dc.embedding IS NOT NULL
        AND (dc.embedding::vector(1536) <=> :query_vec) <= :max_distance
        {project_filter}
        ORDER BY (dc.embedding::vector(1536) <=> :query_vec)
        LIMIT :limit
    """

    params = {
        "org_id": user.organization_id,
        "query_vec": query_embedding,
        "max_distance": max_distance,
        "limit": limit,
    }
    if project_id:
        params["project_id"] = project_id

    result = db.execute(text(sql), params)
    # Map to DocumentChunk objects
    chunks = []
    for row in result:
        chunk = DocumentChunk(
            id=row.id,
            organization_id=row.organization_id,
            project_id=row.project_id,
            document_id=row.document_id,
            chunk_index=row.chunk_index,
            content=row.content,
            token_count=row.token_count,
            embedding=row.embedding,
            created_at=row.created_at,
        )
        chunks.append(chunk)
    return chunks


def delete_document_chunks(db: Session, document_id: str, user: User) -> int:
    """Delete all chunks for a document (org-scoped).

    Returns number of deleted chunks.
    """
    document = db.get(ProjectDocument, document_id)
    if not document or document.organization_id != user.organization_id:
        return 0

    result = db.execute(
        delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
    )
    return result.rowcount