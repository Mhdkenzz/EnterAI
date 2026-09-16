"""add project document ingestion"""
from alembic import op
from sqlalchemy import Column, DateTime, ForeignKey, String, Text

revision = "0002_project_documents"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "project_documents",
        Column("id", String(), primary_key=True),
        Column("organization_id", String(), ForeignKey("organizations.id"), nullable=False),
        Column("project_id", String(), ForeignKey("projects.id"), nullable=True),
        Column("uploaded_by", String(), ForeignKey("users.id"), nullable=False),
        Column("file_name", String(255), nullable=False),
        Column("path", String(500), nullable=False),
        Column("content_type", String(100), nullable=True),
        Column("extracted_text", Text(), nullable=False),
        Column("created_at", DateTime(), nullable=False),
    )
    op.create_index("ix_project_documents_organization_id", "project_documents", ["organization_id"])
    op.create_index("ix_project_documents_project_id", "project_documents", ["project_id"])

def downgrade():
    op.drop_index("ix_project_documents_project_id", table_name="project_documents")
    op.drop_index("ix_project_documents_organization_id", table_name="project_documents")
    op.drop_table("project_documents")
