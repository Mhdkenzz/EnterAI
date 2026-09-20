"""document_chunks.embedding is a fixed VECTOR(1536) pgvector column
(0009_rag_chunks). pgvector's distance operators require equal-dimension
operands and hard-error on mismatch, so mixing dimensions in that one column
isn't silent corruption -- it's an outage the moment a mismatched row gets
scanned. The invariant to enforce is "every row shares one dimension at a
time," which this migration makes checkable: it adds provenance (which
embedding provider/model produced each row, and at what dimension) so a
future dimension change has a way to detect old/new rows coexisting, instead
of only finding out when a query breaks.

Backfill for existing rows is provably correct, not a guess: every
pre-migration row was necessarily inserted through the fixed VECTOR(1536)
column, which pgvector would already have rejected at insert time had the
dimension differed -- so 1536 is the only value the column could ever have
accepted before this migration existed.
"""
from alembic import op
import sqlalchemy as sa

revision = "0016_document_chunk_provenance"
down_revision = "0015_provider_call_observability"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing_cols = {c["name"] for c in sa.inspect(bind).get_columns("document_chunks")}
    if "embedding_provider" not in existing_cols:
        op.add_column("document_chunks", sa.Column("embedding_provider", sa.String(40), nullable=True))
    if "embedding_model" not in existing_cols:
        op.add_column("document_chunks", sa.Column("embedding_model", sa.String(120), nullable=True))
    if "embedding_dim" not in existing_cols:
        op.add_column("document_chunks", sa.Column("embedding_dim", sa.Integer(), nullable=True))
        op.execute(
            "UPDATE document_chunks SET embedding_dim = 1536, embedding_provider = 'unknown', "
            "embedding_model = 'unknown' WHERE embedding_dim IS NULL"
        )


def downgrade():
    for name in ("embedding_dim", "embedding_model", "embedding_provider"):
        op.drop_column("document_chunks", name)
