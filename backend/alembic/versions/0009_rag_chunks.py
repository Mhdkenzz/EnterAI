"""add document chunks with pgvector embeddings for RAG"""
from alembic import op
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, Text, inspect
from sqlalchemy.dialects.postgresql import UUID

revision = "0009_rag_chunks"
down_revision = "0008_auth_hardening"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    # Skip if table already exists (idempotent)
    if "document_chunks" in inspector.get_table_names():
        return

    # Enable pgvector extension (safe to run multiple times)
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Create the table using raw SQL so the embedding column is the native
    # pgvector VECTOR(1536) type (not a String compatible cast).
    op.execute(
        """
        CREATE TABLE document_chunks (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL REFERENCES organizations(id),
            project_id UUID REFERENCES projects(id),
            document_id UUID NOT NULL REFERENCES project_documents(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            token_count INTEGER NOT NULL,
            embedding VECTOR(1536),
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        )
        """
    )

    # Create composite index for efficient document-scoped queries
    op.execute(
        'CREATE INDEX IF NOT EXISTS ix_document_chunks_doc_idx ON document_chunks (document_id, chunk_index)'
    )

    # Create organization and project indexes for tenancy queries
    op.execute(
        'CREATE INDEX IF NOT EXISTS ix_document_chunks_org ON document_chunks (organization_id)'
    )
    op.execute(
        'CREATE INDEX IF NOT EXISTS ix_document_chunks_project ON document_chunks (project_id)'
    )

    # Vector similarity index (ivfflat with cosine distance)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding ON document_chunks "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding")
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_doc_idx")
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_org")
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_project")
    op.execute("DROP TABLE IF EXISTS document_chunks")
    # Note: we don't drop the vector extension as other tables might use it
